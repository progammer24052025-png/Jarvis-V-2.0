"""
Action Builder for J.A.R.V.I.S.
================================
LLM-driven runtime tool creation. When no existing tool matches a user request,
this module calls the LLM to generate a shell/PowerShell command that accomplishes
the task, validates it for safety, and returns a custom action definition.

Flow:
  1. User says "Open Chrome new window"
  2. System detects no existing tool matches
  3. ActionBuilder.build_action_from_request() is called
  4. LLM generates: {name, description, command, dangerous}
  5. validate_action() checks for dangerous patterns
  6. Returns the action definition for CustomActionManager to persist

Safety:
  - validate_action() blocks known-dangerous commands (rm -rf, format, etc.)
  - Dangerous actions are flagged for user confirmation
  - Rate-limited by CustomActionManager (5/minute)
"""

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from config import GROQ_API_KEYS, GROQ_MODEL

logger = logging.getLogger("J.A.R.V.I.S")


# ---------------------------------------------------------------------------
# Safety: patterns that are always dangerous
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERNS = [
    # Destructive file operations
    r"rm\s+(-rf?|--recursive)",
    r"del\s+/[fqs]",
    r"rmdir\s+/[sS]",
    r"format\s+[a-zA-Z]:",
    r"rd\s+/[sS]",
    # Registry edits
    r"reg\s+(add|delete|export)",
    r"New-ItemProperty.*-Path.*HK[LM]\\",
    # System-level destructive commands
    r"shutdown\s+(/s|/r|/h)",
    r"taskkill\s+/F\s+/IM\s+(explorer|csrss|wininit|smss)\.exe",
    # Network exfiltration patterns
    r"curl\s+.*\s+-d\s+.*(/etc/passwd|/etc/shadow)",
    r"Invoke-WebRequest.*-Body.*\$env:",
    # Credential access
    r"cat\s+/etc/shadow",
    r"type\s+.*SAM",
    r"Get-Process.*\|\s*Export-Clixml.*password",
    # Disable security
    r"Set-MpPreference\s+-DisableRealtimeMonitoring",
    r"netsh\s+advfirewall\s+set\s+.*state\s+off",
    r"Set-NetFirewallProfile\s+-Enabled\s+False",
]

# Compile all patterns once
_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]


class ActionBuilder:
    """Builds custom actions from natural language requests using the LLM."""

    def __init__(self):
        self._llm = None  # Lazy init

    def _get_llm(self):
        """Lazy-initialize the LLM for action building."""
        if self._llm is None:
            try:
                from langchain_groq import ChatGroq
                self._llm = ChatGroq(
                    api_key=GROQ_API_KEYS[0],
                    model_name="llama-3.1-8b-instant",  # Fast model for tool generation
                    temperature=0.1,  # Low temperature for deterministic output
                )
            except Exception as e:
                logger.error("[ACTION-BUILDER] Failed to init LLM: %s", e)
                raise
        return self._llm

    def build_action_from_request(
        self,
        user_message: str,
        existing_tools: List[str],
    ) -> Optional[dict]:
        """
        Analyze a user request and generate a custom action definition.

        Args:
            user_message: What the user said (e.g. "Open Chrome new window").
            existing_tools: List of existing tool names (so LLM doesn't duplicate).

        Returns:
            dict with keys: name, description, execution, params, dangerous,
            trigger_phrases — or None if the LLM can't generate a valid action.
        """
        try:
            llm = self._get_llm()
        except Exception:
            return None

        tools_list = ", ".join(existing_tools[:50])  # Cap at 50 to save tokens

        system_prompt = """You are a Windows automation expert. Your job is to generate shell or PowerShell commands that accomplish specific tasks on Windows.

Given a user's request, generate a custom action definition as a JSON object.

RULES:
- The command must work on Windows 10/11.
- Use "start" for opening apps/URLs, "powershell" for automation.
- Keep the command simple and safe. No destructive operations.
- The action name must be a valid Python identifier (snake_case, no spaces, no hyphens).
- If the task cannot be accomplished with a shell command, return null.

EXISTING TOOLS (do not duplicate these):
""" + tools_list + """

RESPONSE FORMAT (strict JSON, no markdown):
{
    "name": "action_name_snake_case",
    "description": "Brief description of what this does",
    "trigger_phrases": ["phrase1", "phrase2"],
    "execution": {
        "type": "shell",
        "command": "the actual command to run",
        "platform": "windows"
    },
    "params": [],
    "dangerous": false
}

If the task is impossible via shell/PowerShell, respond with exactly: null
"""

        user_prompt = f'User request: "{user_message}"'

        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            response = llm.invoke(messages)
            raw = response.content.strip()

            # Strip markdown code fences if the LLM wraps in ```json ... ```
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)

            if raw.lower() == "null" or raw == "{}":
                logger.info("[ACTION-BUILDER] LLM says task is impossible: %s", user_message[:80])
                return None

            action_def = json.loads(raw)

            # Validate required fields
            if not action_def.get("name") or not action_def.get("execution", {}).get("command"):
                logger.warning("[ACTION-BUILDER] LLM returned incomplete action: %s", raw[:200])
                return None

            return action_def

        except json.JSONDecodeError as e:
            logger.warning("[ACTION-BUILDER] LLM returned invalid JSON: %s | Error: %s", raw[:200] if 'raw' in dir() else "", e)
            return None
        except Exception as e:
            logger.error("[ACTION-BUILDER] Failed to build action: %s", e)
            return None

    def validate_action(self, action_def: dict) -> Tuple[bool, str]:
        """
        Validate a custom action for safety.

        Returns:
            (is_safe: bool, reason: str)
            If not safe, reason explains why.
        """
        command = action_def.get("execution", {}).get("command", "")
        if not command:
            return False, "No command defined."

        # Check against dangerous patterns
        for pattern in _DANGEROUS_RE:
            if pattern.search(command):
                return False, f"Command matches dangerous pattern: {pattern.pattern}"

        # Check for obviously malicious commands
        name = action_def.get("name", "")
        if len(name) > 64:
            return False, "Action name too long (max 64 chars)."

        if not name.isidentifier():
            return False, f"Action name '{name}' is not a valid identifier."

        # Flag as dangerous if it involves elevated operations
        dangerous_keywords = [
            "runas", "Start-Process.*-Verb.*RunAs",
            "net user", "net localgroup",
            "icacls", "takeown",
        ]
        for kw in dangerous_keywords:
            if re.search(kw, command, re.IGNORECASE):
                action_def["dangerous"] = True
                action_def["requires_confirmation"] = True
                logger.info("[ACTION-BUILDER] Action '%s' flagged as dangerous (keyword: %s)", name, kw)
                break

        return True, "Safe."

    def generate_tool_name(self, description: str) -> str:
        """
        Generate a clean snake_case tool name from a description.
        E.g. "Open Chrome New Window" -> "open_chrome_new_window"
        """
        # Remove non-alphanumeric chars (keep spaces for splitting)
        clean = re.sub(r"[^a-zA-Z0-9\s]", "", description)
        # Split on spaces, lowercase, join with underscore
        parts = clean.lower().split()
        # Cap at 5 words to keep names reasonable
        name = "_".join(parts[:5])
        # Ensure it starts with a letter
        if name and not name[0].isalpha():
            name = "tool_" + name
        return name or "custom_action"


# Global singleton instance
action_builder = ActionBuilder()
