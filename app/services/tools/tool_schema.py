"""
Universal Tool Schema for J.A.R.V.I.S.
=======================================
Every tool (local PC tool or remote device tool) follows ONE schema.
This makes tools discoverable by the LLM automatically and enables
the Device Manager to route tool calls to the correct device.

Backward compatible: existing SYSTEM_TOOLS dict entries can be wrapped
into ToolDefinition via from_legacy_dict().
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("J.A.R.V.I.S")


# ---------------------------------------------------------------------------
# Parameter definition
# ---------------------------------------------------------------------------
@dataclass
class ParameterDef:
    """Definition of a single tool parameter."""
    name: str
    type: str = "string"          # "string", "integer", "float", "boolean"
    description: str = ""
    required: bool = True
    default: Any = None


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
@dataclass
class RetryPolicy:
    """How the Action Manager should retry this tool on failure."""
    max_retries: int = 0          # 0 = no retry
    backoff_seconds: float = 1.0  # initial backoff; doubled each attempt


# ---------------------------------------------------------------------------
# Tool definition — the universal schema
# ---------------------------------------------------------------------------
@dataclass
class ToolDefinition:
    """
    Universal description of a tool.
    Both local (SYSTEM_TOOLS) and remote (device) tools use this schema.
    The LLM reads name + description + parameters to decide which tool to call.
    The Action Manager reads timeout + retry_policy + dangerous for execution.
    """
    # Identity
    name: str
    description: str

    # Parameters & return
    parameters: List[ParameterDef] = field(default_factory=list)
    returns: str = "str"          # human description of what this returns

    # Permission & safety
    permission: str = "NONE"      # "NONE", "CAMERA", "PHONE", "STORAGE", etc.
    dangerous: bool = False
    requires_confirmation: bool = False

    # Execution
    timeout: int = 30             # seconds before tool call is aborted
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    version: str = "v1"           # e.g. "camera:v2"
    category: str = "local"       # "local" (server) or "remote" (device)
    capability: str = ""          # e.g. "camera", "gps", "bluetooth", "pc"

    # LLM examples (shown in system prompt)
    examples: List[str] = field(default_factory=list)

    # For local tools only: the actual callable and positional param names
    func: Optional[Callable] = field(default=None, repr=False)
    legacy_params: List[str] = field(default_factory=list, repr=False)

    # Cache TTL in seconds (0 = no caching)
    cache_ttl: int = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def is_remote(self) -> bool:
        return self.category == "remote"

    @property
    def is_local(self) -> bool:
        return self.category == "local"

    @property
    def is_custom(self) -> bool:
        return self.category == "custom"

    def to_llm_description(self) -> str:
        """One-line description for the LLM system prompt."""
        param_str = ", ".join(
            f'"{p.name}"' for p in self.parameters if p.required
        )
        return f'  - [ACTION:{self.name}({param_str})] \u2014 {self.description}'

    def to_dict(self) -> dict:
        """Serialize to a plain dict (for API responses / WebSocket)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                }
                for p in self.parameters
            ],
            "returns": self.returns,
            "permission": self.permission,
            "dangerous": self.dangerous,
            "requires_confirmation": self.requires_confirmation,
            "timeout": self.timeout,
            "version": self.version,
            "category": self.category,
            "capability": self.capability,
            "examples": self.examples,
            "cache_ttl": self.cache_ttl,
        }

    # ------------------------------------------------------------------
    # Build from legacy SYSTEM_TOOLS dict entry
    # ------------------------------------------------------------------
    @classmethod
    def from_legacy_dict(cls, name: str, entry: dict,
                         requires_confirmation_set: Optional[set] = None) -> "ToolDefinition":
        """
        Convert a legacy SYSTEM_TOOLS entry like:
            {"func": callable, "description": "...", "params": ["a", "b"]}
        into a ToolDefinition.

        Args:
            name: Tool name.
            entry: Legacy dict with "func", "description", "params".
            requires_confirmation_set: Set of tool names needing user confirmation.
                Pass this from tool_executor to avoid circular imports.
        """
        param_names = entry.get("params", [])
        parameters = [ParameterDef(name=p) for p in param_names]

        # Determine if this tool is dangerous (check known set)
        _dangerous_tools = {
            "empty_recycle_bin", "shutdown_pc", "delete_item",
            "lock_pc", "run_command",
        }
        _confirm_set = requires_confirmation_set or set()

        return cls(
            name=name,
            description=entry.get("description", ""),
            parameters=parameters,
            func=entry.get("func"),
            legacy_params=param_names,
            dangerous=name in _dangerous_tools,
            requires_confirmation=name in _confirm_set,
            category="local",
            capability="pc",
            # Sensible defaults for cache
            cache_ttl=_default_cache_ttl(name),
        )


# ---------------------------------------------------------------------------
# Default cache TTLs for common tools
# ---------------------------------------------------------------------------
def _default_cache_ttl(tool_name: str) -> int:
    """Return default cache TTL (seconds) for a tool. 0 = no cache.
    Only cache data that genuinely doesn't change between calls.
    Dynamic data (RAM, CPU, network speed) must NOT be cached."""
    _TTL_MAP = {
        # Static — rarely changes
        "list_installed_apps": 600,   # 10 min
        "wifi_info": 120,             # 2 min
        # Semi-dynamic — changes occasionally
        "list_desktop": 15,           # 15 sec
        "list_folder": 15,            # 15 sec
        "weather": 300,               # 5 min
        # Dynamic — NEVER cache (changes every second)
        # system_info, pc_health, network_speed -> 0 (default)
    }
    return _TTL_MAP.get(tool_name, 0)


# ---------------------------------------------------------------------------
# Tool Registry — holds all ToolDefinitions
# ---------------------------------------------------------------------------
class ToolRegistry:
    """
    Central registry of all tools (local + remote).
    Populated at startup from SYSTEM_TOOLS and remote tool definitions.
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool. Overwrites any previous tool with the same name."""
        self._tools[tool.name] = tool
        logger.info("[TOOL-SCHEMA] Registered: %s (%s)", tool.name, tool.category)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def all_tools(self) -> Dict[str, ToolDefinition]:
        return dict(self._tools)

    def local_tools(self) -> Dict[str, ToolDefinition]:
        return {n: t for n, t in self._tools.items() if t.category == "local"}

    def remote_tools(self) -> Dict[str, ToolDefinition]:
        return {n: t for n, t in self._tools.items() if t.category == "remote"}

    def custom_tools(self) -> Dict[str, ToolDefinition]:
        return {n: t for n, t in self._tools.items() if t.category == "custom"}

    def tools_for_capability(self, capability: str) -> List[ToolDefinition]:
        return [t for t in self._tools.values() if t.capability == capability]

    def to_llm_prompt(self) -> str:
        """Generate tool descriptions for the LLM system prompt."""
        lines = []
        for name, tool in sorted(self._tools.items()):
            lines.append(tool.to_llm_description())
        return "\n".join(lines)

    def to_api_list(self) -> list:
        """Serialize all tools for the /api/tools endpoint."""
        return [t.to_dict() for t in self._tools.values()]

    def load_from_system_tools(self, requires_confirmation_set: Optional[set] = None) -> None:
        """Load all tools from the legacy SYSTEM_TOOLS dict."""
        from app.services.tools.system_tools import SYSTEM_TOOLS
        for name, entry in SYSTEM_TOOLS.items():
            tool_def = ToolDefinition.from_legacy_dict(name, entry, requires_confirmation_set)
            self.register(tool_def)
        logger.info("[TOOL-SCHEMA] Loaded %d local tools from SYSTEM_TOOLS", len(SYSTEM_TOOLS))

    def load_custom_tools(self, user_id: str) -> int:
        """
        Load custom (user-created) actions into the registry as ToolDefinitions.
        Each custom action gets category="custom" so the LLM can discover them.

        Returns:
            Number of custom tools loaded.
        """
        from app.services.tools.custom_action_manager import custom_action_manager

        actions = custom_action_manager.get_all_actions(user_id)
        count = 0
        for action in actions:
            name = action.get("name", "")
            if not name:
                continue

            # Build ParameterDef list from action's params
            param_names = action.get("params", [])
            parameters = [ParameterDef(name=p) for p in param_names]

            tool_def = ToolDefinition(
                name=name,
                description=action.get("description", ""),
                parameters=parameters,
                category="custom",
                capability="pc",
                dangerous=action.get("dangerous", False),
                requires_confirmation=action.get("requires_confirmation", False),
                cache_ttl=0,  # Custom actions are never cached
            )
            self.register(tool_def)
            count += 1

        logger.info("[TOOL-SCHEMA] Loaded %d custom tools for user '%s'", count, user_id)
        return count

    def register_custom_action(self, action_def: dict) -> None:
        """
        Register a single custom action as a ToolDefinition.
        Called at runtime when a new action is created.
        """
        name = action_def.get("name", "")
        if not name:
            return

        param_names = action_def.get("params", [])
        parameters = [ParameterDef(name=p) for p in param_names]

        tool_def = ToolDefinition(
            name=name,
            description=action_def.get("description", ""),
            parameters=parameters,
            category="custom",
            capability="pc",
            dangerous=action_def.get("dangerous", False),
            requires_confirmation=action_def.get("requires_confirmation", False),
            cache_ttl=0,
        )
        self.register(tool_def)


# Global registry instance
tool_registry = ToolRegistry()
