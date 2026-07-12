"""
Tool executor for J.A.R.V.I.S.
Parses [ACTION:tool_name(params)] tags from LLM output, executes the corresponding
tool function, and returns the result. Works with the streaming pipeline.

Integrates with:
  - ToolCache: avoids redundant calls for cacheable tools
  - EventBus: emits tool_started/completed/failed/cached events
  - ToolRegistry: universal tool schema for LLM discovery
  - DeviceManager: routes remote tool calls to connected devices
"""

import re
import json
import logging
import time
from typing import Optional, Tuple

from app.services.tools.system_tools import SYSTEM_TOOLS

logger = logging.getLogger("J.A.R.V.I.S")

# Lazy imports to avoid circular dependencies at module load time.
# These are initialized on first use.
_tool_cache = None
_event_bus = None
_tool_registry = None
_device_manager = None
_custom_action_manager = None


def _get_cache():
    global _tool_cache
    if _tool_cache is None:
        from app.services.tools.tool_cache import tool_cache
        _tool_cache = tool_cache
    return _tool_cache


def _get_event_bus():
    global _event_bus
    if _event_bus is None:
        from app.services.event_bus import event_bus
        _event_bus = event_bus
    return _event_bus


def _get_registry():
    global _tool_registry
    if _tool_registry is None:
        from app.services.tools.tool_schema import tool_registry
        _tool_registry = tool_registry
    return _tool_registry


def _get_device_manager():
    global _device_manager
    if _device_manager is None:
        from app.services.device_manager import device_manager
        _device_manager = device_manager
    return _device_manager


def _get_custom_action_manager():
    global _custom_action_manager
    if _custom_action_manager is None:
        from app.services.tools.custom_action_manager import custom_action_manager
        _custom_action_manager = custom_action_manager
    return _custom_action_manager

# Tools that require explicit user confirmation before execution.
# When triggered, the system asks the user to confirm instead of executing immediately.
REQUIRES_CONFIRMATION = {"empty_recycle_bin", "lock_pc", "delete_item", "shutdown_pc"}

# Tracks pending destructive actions awaiting user confirmation.
# Key: tool_name, Value: {"params": list, "prompt": str}
_pending_confirmations: dict = {}


def _find_action_tags(text: str) -> list:
    """
    Find all [ACTION:tool(params)] tags in text using a state-machine parser
    instead of regex, so we correctly handle ) inside quoted strings.
    Returns list of (start_pos, end_pos, tool_name, raw_params_str).
    """
    results = []
    marker = "[ACTION:"
    i = 0
    while i < len(text):
        idx = text.lower().find(marker.lower(), i)
        if idx == -1:
            break

        # Found [ACTION: — now extract tool name
        name_start = idx + len(marker)
        paren_pos = text.find("(", name_start)
        if paren_pos == -1:
            i = name_start
            continue

        tool_name = text[name_start:paren_pos].strip()
        if not tool_name.isidentifier():
            i = name_start
            continue

        # Walk forward from opening ( to find matching ) respecting quotes
        depth = 0
        in_quote = None
        j = paren_pos
        found_close = -1
        while j < len(text):
            ch = text[j]
            if in_quote:
                if ch == '\\' and j + 1 < len(text):
                    j += 2  # skip escaped char
                    continue
                if ch == in_quote:
                    in_quote = None
            else:
                if ch in ('"', "'"):
                    in_quote = ch
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        found_close = j
                        break
            j += 1

        if found_close == -1:
            i = name_start
            continue

        # Check for closing ]
        if found_close + 1 < len(text) and text[found_close + 1] == ']':
            raw_params = text[paren_pos + 1:found_close]
            end_pos = found_close + 2  # past the ]
            results.append((idx, end_pos, tool_name, raw_params))
            i = end_pos
        else:
            i = name_start

    return results


def parse_action(text: str) -> Optional[Tuple[str, list]]:
    """
    Parse an [ACTION:tool(params)] tag from text.
    Returns (tool_name, [param_values]) or None if no action found.
    """
    tags = _find_action_tags(text)
    if not tags:
        return None

    _, _, tool_name, raw_params = tags[0]
    tool_name = tool_name.lower()
    raw_params = raw_params.strip()

    if not raw_params:
        return (tool_name, [])

    # Parse params: handle quoted strings and numbers
    params = []
    for part in _split_params(raw_params):
        part = part.strip()
        if not part:
            continue
        # Remove quotes
        if (part.startswith('"') and part.endswith('"')) or \
           (part.startswith("'") and part.endswith("'")):
            params.append(part[1:-1])
        else:
            # Try to parse as number
            try:
                if '.' in part:
                    params.append(float(part))
                else:
                    params.append(int(part))
            except ValueError:
                params.append(part)  # Keep as raw string

    return (tool_name, params)


def _split_params(s: str) -> list:
    """Split comma-separated params while respecting quoted strings."""
    parts = []
    current = ""
    in_quote = None
    for ch in s:
        if ch in ('"', "'") and in_quote is None:
            in_quote = ch
            current += ch
        elif ch == in_quote:
            in_quote = None
            current += ch
        elif ch == ',' and in_quote is None:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def execute_action(tool_name: str, params: list) -> str:
    """
    Execute a tool by name with the given parameters.

    Flow:
      1. Check tool cache (if tool has cache_ttl > 0)
      2. Check SYSTEM_TOOLS (local) -> execute directly
      3. Check remote tools (device) -> route via DeviceManager
      4. Emit events: tool_started, tool_completed/failed/cached
      5. Cache result if cacheable
    """
    bus = _get_event_bus()
    cache = _get_cache()

    # --- Check cache first ---
    # Look up the tool definition to get cache_ttl
    registry = _get_registry()
    tool_def = registry.get(tool_name)
    cache_ttl = tool_def.cache_ttl if tool_def else 0

    if cache_ttl > 0:
        cached = cache.get(tool_name, params)
        if cached is not None:
            logger.info("[TOOL-EXEC] Cache hit: %s", tool_name)
            bus.emit("tool_cached", {"tool": tool_name})
            return cached

    # --- Check local tools ---
    if tool_name in SYSTEM_TOOLS:
        return _execute_local(tool_name, params, cache, cache_ttl, bus)

    # --- Check custom actions (user-created tools) ---
    cam = _get_custom_action_manager()
    if cam.is_custom_tool(tool_name):
        return _execute_custom(tool_name, params, bus)

    # --- Check remote tools (device routing) ---
    if tool_def and tool_def.is_remote:
        return _execute_remote(tool_name, params, tool_def, bus)

    # --- Unknown tool ---
    logger.warning("[TOOL-EXEC] Unknown tool: %s", tool_name)
    return f"Unknown tool: {tool_name}"


def _execute_local(tool_name: str, params: list, cache, cache_ttl: int, bus) -> str:
    """Execute a local (server-side) tool with events and caching."""
    tool = SYSTEM_TOOLS[tool_name]
    func = tool["func"]
    expected_params = tool["params"]

    bus.emit("tool_started", {"tool": tool_name, "category": "local"})
    t0 = time.perf_counter()

    try:
        # Build kwargs from positional params
        kwargs = {}
        for i, param_name in enumerate(expected_params):
            if i < len(params):
                kwargs[param_name] = params[i]

        # Check if this is a confirmation for a pending destructive action
        if tool_name in REQUIRES_CONFIRMATION:
            # Check if user already confirmed (params contain "_confirmed")
            if params and params[-1] == "_confirmed":
                # Remove the confirmation marker before executing
                kwargs.pop(expected_params[-1], None) if len(params) > len(expected_params) else None
                _pending_confirmations.pop(tool_name, None)
                logger.info("[TOOL-EXEC] Confirmed and executing: %s(%s)", tool_name, kwargs)
                result = func(**kwargs)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                logger.info("[TOOL-EXEC] Result: %s", str(result)[:200])
                bus.emit("tool_completed", {"tool": tool_name, "elapsed_ms": elapsed_ms})
                return result
            else:
                # Store pending confirmation
                _pending_confirmations[tool_name] = {"params": params, "kwargs": kwargs}
                logger.info("[TOOL-EXEC] Confirmation required for: %s", tool_name)
                return (f"CONFIRMATION_REQUIRED: This action ({tool_name}) requires your explicit confirmation "
                        f"for safety. Please say 'Yes, confirm {tool_name}' to proceed.")

        logger.info("[TOOL-EXEC] Executing: %s(%s)", tool_name, kwargs)
        result = func(**kwargs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("[TOOL-EXEC] Result (%d ms): %s", elapsed_ms, str(result)[:200])

        # Cache the result if cacheable
        if cache_ttl > 0 and result and not result.startswith("Tool error"):
            cache.set(tool_name, params, result, cache_ttl)

        bus.emit("tool_completed", {"tool": tool_name, "elapsed_ms": elapsed_ms})
        return result
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error("[TOOL-EXEC] Error executing %s (%d ms): %s", tool_name, elapsed_ms, e)
        bus.emit("tool_failed", {"tool": tool_name, "error": str(e), "elapsed_ms": elapsed_ms})
        return f"Tool error: {e}"


def _execute_remote(tool_name: str, params: list, tool_def, bus) -> str:
    """Execute a remote tool by routing to a connected device."""
    dm = _get_device_manager()
    capability = tool_def.capability

    device = dm.get_best_device(capability)
    if not device:
        logger.warning("[TOOL-EXEC] No device with capability '%s' connected", capability)
        bus.emit("tool_failed", {
            "tool": tool_name,
            "error": f"No connected device has capability: {capability}",
        })
        return f"No connected device has the '{capability}' capability."

    bus.emit("tool_started", {
        "tool": tool_name,
        "category": "remote",
        "device_id": device.device_id,
    })

    # For now, remote tools return a placeholder.
    # Full WebSocket routing will be added in Step 10.
    logger.info("[TOOL-EXEC] Remote tool %s -> device %s (not yet wired)", tool_name, device.device_id)
    bus.emit("tool_completed", {"tool": tool_name, "device_id": device.device_id})
    return f"[Remote tool '{tool_name}' routed to {device.device_type} device '{device.device_id}'. Full execution pending WebSocket integration.]"


def _execute_custom(tool_name: str, params: list, bus) -> str:
    """Execute a custom (user-created) action with events."""
    cam = _get_custom_action_manager()

    bus.emit("tool_started", {"tool": tool_name, "category": "custom"})
    t0 = time.perf_counter()

    try:
        logger.info("[TOOL-EXEC] Executing custom action: %s(%s)", tool_name, params)
        result = cam.execute_action(tool_name, params)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("[TOOL-EXEC] Custom action result (%d ms): %s", elapsed_ms, str(result)[:200])

        bus.emit("tool_completed", {"tool": tool_name, "elapsed_ms": elapsed_ms})
        return result
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error("[TOOL-EXEC] Custom action error %s (%d ms): %s", tool_name, elapsed_ms, e)
        bus.emit("tool_failed", {"tool": tool_name, "error": str(e), "elapsed_ms": elapsed_ms})
        return f"Custom action error: {e}"


def process_text_for_actions(text: str) -> Tuple[str, list]:
    """
    Scan text for [ACTION:...] tags, execute each one, and return
    (cleaned_text, [action_results]).
    The action tags are removed from the text.
    """
    results = []
    cleaned = text

    for start, end, tool_name, raw_params in _find_action_tags(text):
        full_tag = text[start:end]
        parsed = parse_action(full_tag)
        if parsed:
            t_name, params = parsed
            result = execute_action(t_name, params)
            results.append({
                "tool": t_name,
                "params": params,
                "result": result,
            })
            # Remove the action tag from displayed text
            cleaned = cleaned.replace(full_tag, "")

    # Clean up extra whitespace from removed tags
    cleaned = re.sub(r'\n\s*\n\s*\n', '\n\n', cleaned).strip()

    return cleaned, results


def get_tools_description() -> str:
    """
    Generate a description of all available tools for the system prompt.
    This tells the LLM what tools it can call and how.
    """
    lines = [
        "=== AVAILABLE TOOLS (PC Automation) ===",
        "",
        "You can execute actions on the user's PC by including action tags in your response.",
        "Format: [ACTION:tool_name(\"param1\", \"param2\")]",
        "The tag will be executed by the system and removed from the displayed response.",
        "Place the tag on its own line. You can include multiple action tags if needed.",
        "",
        "CRITICAL RULES:",
        "- The system will automatically show tool results to the user AFTER your response.",
        "- Keep your text BEFORE the action tag VERY BRIEF (1-5 words max).",
        "- GOOD: 'Checking. [ACTION:git_status(\"project\")]'",
        "- BAD: 'Let me check the git status of your project for you...' (too long, results shown anyway)",
        "- BAD: 'I am finding the information...' (unnecessary preamble)",
        "- Never show the raw [ACTION:...] syntax to the user.",
        "- Do NOT try to report tool results yourself — the system handles that automatically.",
        "",
        "Available tools:",
    ]

    for name, tool in SYSTEM_TOOLS.items():
        param_str = ", ".join(f'"{p}"' for p in tool["params"]) if tool["params"] else ""
        lines.append(f'  - [ACTION:{name}({param_str})] \u2014 {tool["description"]}')
    
    # Append custom (user-created) actions
    try:
        cam = _get_custom_action_manager()
        from config import CURRENT_USER_ID
        custom_actions = cam.get_all_actions(CURRENT_USER_ID)
        if custom_actions:
            lines.append("")
            lines.append("Custom tools (user-created):")
            for action in custom_actions:
                aname = action.get("name", "")
                adesc = action.get("description", "")
                aparams = action.get("params", [])
                param_str = ", ".join(f'"{p}"' for p in aparams) if aparams else ""
                lines.append(f'  - [ACTION:{aname}({param_str})] \u2014 {adesc}')
    except Exception:
        pass  # Custom actions are optional in the prompt
    
    lines.extend([
        "",
        "Examples (note how brief the text is):",
        '  User: "Open Chrome"',
        '  Response: "Opening. [ACTION:open_app(\"Chrome\")]"',
        "",
        '  User: "Play Bohemian Rhapsody on YouTube"',
        '  Response: "Playing. [ACTION:play_youtube(\"Bohemian Rhapsody\")]"',
        "",
        '  User: "How much RAM am I using?"',
        '  Response: "[ACTION:system_info()]"',
        "",
        '  User: "Check git status of my Airbnb project"',
        '  Response: "[ACTION:git_status(\"Airbnb Clone\")]"',
        "",
        '  User: "What\'s on my desktop?"',
        '  Response: "[ACTION:list_desktop()]"',
        "",
        '  User: "Mute the volume"',
        '  Response: "Done. [ACTION:volume_control(\"mute\")]"',
        "",
        '  User: "Open the resume file"',
        '  Response: "Opening. [ACTION:open_file(\"resume\")]"',
        "",
        '  User: "Open a new Chrome window"',
        '  Response: "Opening. [ACTION:open_new_window(\"chrome\")]"',
        "",
        '  User: "Close LinkedIn"',
        '  Response: "Closing. [ACTION:close_app(\"linkedin\")]"',
        "",
        "=== RUNTIME TOOL CREATION ===",
        "",
        "If the user asks you to DO something (open, create, launch, automate) and NO existing tool above can do it,",
        "you can CREATE a new tool at runtime. Use this format:",
        '  [CREATE_TOOL:user request description]',
        "",
        "The system will automatically:",
        "  1. Generate the command to accomplish the task",
        "  2. Save it as a permanent tool for future use",
        "  3. Execute it immediately",
        "",
        "Examples:",
        '  User: "Create a system restore point"',
        '  Response: "[CREATE_TOOL:Create a system restore point]"',
        "",
        '  User: "Set power plan to high performance"',
        '  Response: "[CREATE_TOOL:Set power plan to high performance]"',
        "",
        "IMPORTANT: Only use [CREATE_TOOL:...] for ACTION requests, not for questions or information.",
        "If an existing tool can handle the request, use [ACTION:...] instead.",
    ])

    return "\n".join(lines)
