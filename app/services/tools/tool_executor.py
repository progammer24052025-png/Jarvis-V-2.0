"""
Tool executor for J.A.R.V.I.S.
Parses [ACTION:tool_name(params)] tags from LLM output, executes the corresponding
tool function, and returns the result. Works with the streaming pipeline.
"""

import re
import json
import logging
from typing import Optional, Tuple

from app.services.tools.system_tools import SYSTEM_TOOLS

logger = logging.getLogger("J.A.R.V.I.S")


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
    """Execute a tool by name with the given parameters."""
    if tool_name not in SYSTEM_TOOLS:
        logger.warning("[TOOL-EXEC] Unknown tool: %s", tool_name)
        return f"Unknown tool: {tool_name}"

    tool = SYSTEM_TOOLS[tool_name]
    func = tool["func"]
    expected_params = tool["params"]

    try:
        # Build kwargs from positional params
        kwargs = {}
        for i, param_name in enumerate(expected_params):
            if i < len(params):
                kwargs[param_name] = params[i]

        logger.info("[TOOL-EXEC] Executing: %s(%s)", tool_name, kwargs)
        result = func(**kwargs)
        logger.info("[TOOL-EXEC] Result: %s", str(result)[:200])
        return result
    except Exception as e:
        logger.error("[TOOL-EXEC] Error executing %s: %s", tool_name, e)
        return f"Tool error: {e}"


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
        "IMPORTANT:",
        "- Always include the action tag when you want to perform an action.",
        "- The action is executed IMMEDIATELY — tell the user you are doing it.",
        "- You MUST still respond normally alongside the action tag.",
        "- Never show the raw [ACTION:...] syntax to the user in your spoken text.",
        "",
        "Available tools:",
    ]

    for name, tool in SYSTEM_TOOLS.items():
        param_str = ", ".join(f'"{p}"' for p in tool["params"]) if tool["params"] else ""
        lines.append(f'  - [ACTION:{name}({param_str})] — {tool["description"]}')

    lines.extend([
        "",
        "Examples:",
        '  User: "Open Chrome"',
        '  Response: "Right away. [ACTION:open_app(\"Chrome\")]"',
        "",
        '  User: "Play Bohemian Rhapsody on YouTube"',
        '  Response: "On it. [ACTION:play_youtube(\"Bohemian Rhapsody\")]"',
        "",
        '  User: "How much RAM am I using?"',
        '  Response: "[ACTION:system_info()] Let me check that for you."',
        "",
        '  User: "Search for Python tutorials"',
        '  Response: "Searching. [ACTION:search_web(\"Python tutorials\")]"',
        "",
        '  User: "Mute the volume"',
        '  Response: "Done. [ACTION:volume_control(\"mute\")]"',
        "",
        '  User: "Open the resume file on my desktop"',
        '  Response: "Opening that. [ACTION:open_file(\"resume\")]"',
        "",
        '  User: "Read my notes.txt file"',
        '  Response: "[ACTION:read_file(\"notes.txt\")] Here is what it says..."',
        "",
        '  User: "Write a to-do list to a file"',
        '  Response: "Saved to your Desktop. [ACTION:write_file(\"todo.txt\", \"1. Buy groceries\\n2. Call dentist\")]"',
    ])

    return "\n".join(lines)
