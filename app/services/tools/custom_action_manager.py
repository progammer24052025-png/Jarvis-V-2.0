"""
Custom Action Manager for J.A.R.V.I.S.
=======================================
Runtime Feature Creation engine. Manages user-created tools that persist
across restarts. Each user has their own JSON file of custom actions.

Architecture:
  - In-memory dict is the source of truth during runtime.
  - Every create/delete immediately persists to disk (JSON).
  - At startup, load_actions() hydrates memory from disk.
  - Per-user isolation: each user gets their own JSON file.

Thread safety:
  - All public methods acquire _lock before mutating state.
  - Reads (get_action, is_custom_tool) also lock for consistency.
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import CUSTOM_ACTIONS_DIR, CUSTOM_ACTIONS_ENABLED

logger = logging.getLogger("J.A.R.V.I.S")


class CustomActionManager:
    """
    Manages runtime-created tools (custom actions) for all users.

    Storage layout:
        database/custom_actions/
          default_user.json       <- one file per user
          another_user.json

    Per-action schema (stored in JSON):
        {
            "name": "open_chrome_new_window",
            "description": "Open Google Chrome in a new window",
            "trigger_phrases": ["open chrome new window"],
            "created_by": "default_user",
            "created_at": "2026-07-01T22:30:00",
            "last_used": "2026-07-01T22:30:00",
            "usage_count": 5,
            "execution": {
                "type": "shell",         # "shell" | "powershell" | "compose"
                "command": "start chrome --new-window",
                "platform": "windows"
            },
            "params": [],
            "dangerous": false,
            "requires_confirmation": false
        }
    """

    def __init__(self):
        self._lock = threading.RLock()
        # Key: user_id -> {action_name: action_dict}
        self._actions: Dict[str, Dict[str, dict]] = {}
        # Rate limit tracking: user_id -> list of creation timestamps
        self._creation_times: Dict[str, List[float]] = {}
        self._enabled = CUSTOM_ACTIONS_ENABLED

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _user_file(self, user_id: str) -> Path:
        """Return the JSON file path for a user's custom actions."""
        safe_id = user_id.replace("/", "_").replace("\\", "_").replace("..", "_")
        return CUSTOM_ACTIONS_DIR / f"{safe_id}.json"

    def load_actions(self, user_id: str) -> int:
        """
        Load all custom actions for a user from disk into memory.
        Returns the number of actions loaded.
        """
        if not self._enabled:
            return 0

        filepath = self._user_file(user_id)
        if not filepath.exists():
            logger.info("[CUSTOM-ACTIONS] No file for user '%s' — starting fresh", user_id)
            with self._lock:
                self._actions[user_id] = {}
            return 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            actions = {}
            for action_def in data.get("actions", []):
                name = action_def.get("name", "")
                if name:
                    actions[name] = action_def

            with self._lock:
                self._actions[user_id] = actions

            logger.info("[CUSTOM-ACTIONS] Loaded %d actions for user '%s'", len(actions), user_id)
            return len(actions)

        except Exception as e:
            logger.error("[CUSTOM-ACTIONS] Failed to load actions for '%s': %s", user_id, e)
            with self._lock:
                self._actions[user_id] = {}
            return 0

    def save_actions(self, user_id: str) -> bool:
        """Persist all custom actions for a user to disk."""
        filepath = self._user_file(user_id)
        with self._lock:
            actions = list(self._actions.get(user_id, {}).values())

        try:
            data = {
                "user_id": user_id,
                "updated_at": datetime.now().isoformat(),
                "actions": actions,
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error("[CUSTOM-ACTIONS] Failed to save actions for '%s': %s", user_id, e)
            return False

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------
    def create_action(self, user_id: str, action_def: dict) -> tuple:
        """
        Register a new custom action and persist it.

        Args:
            user_id: Owner of the action.
            action_def: Dict with keys: name, description, execution, params, etc.

        Returns:
            (success: bool, message: str)
        """
        if not self._enabled:
            return False, "Custom actions are disabled."

        name = action_def.get("name", "").strip()
        if not name:
            return False, "Action name is required."

        # Validate name is a valid Python identifier (for [ACTION:name(...)])
        if not name.isidentifier():
            return False, f"Invalid action name '{name}'. Must be a valid identifier (letters, digits, underscore)."

        # Rate limit check
        if not self._check_rate_limit(user_id):
            return False, "Rate limit exceeded. Max 5 new tools per minute."

        # Check for name collision with existing actions
        with self._lock:
            user_actions = self._actions.setdefault(user_id, {})
            if name in user_actions:
                return False, f"Action '{name}' already exists. Delete it first or use a different name."

        # Fill in metadata
        now = datetime.now().isoformat()
        action_def.setdefault("created_by", user_id)
        action_def.setdefault("created_at", now)
        action_def.setdefault("last_used", now)
        action_def.setdefault("usage_count", 0)
        action_def.setdefault("dangerous", False)
        action_def.setdefault("requires_confirmation", False)
        action_def.setdefault("trigger_phrases", [])
        action_def.setdefault("params", [])

        with self._lock:
            user_actions[name] = action_def
            self._record_creation(user_id)

        # Persist to disk
        self.save_actions(user_id)
        logger.info("[CUSTOM-ACTIONS] Created action '%s' for user '%s'", name, user_id)
        return True, f"Action '{name}' created successfully."

    def delete_action(self, user_id: str, action_name: str) -> tuple:
        """
        Remove a custom action permanently.

        Returns:
            (success: bool, message: str)
        """
        with self._lock:
            user_actions = self._actions.get(user_id, {})
            if action_name not in user_actions:
                return False, f"Action '{action_name}' not found."
            del user_actions[action_name]

        self.save_actions(user_id)
        logger.info("[CUSTOM-ACTIONS] Deleted action '%s' for user '%s'", action_name, user_id)
        return True, f"Action '{action_name}' deleted."

    def get_action(self, name: str) -> Optional[dict]:
        """Look up a custom action by name across all users (for tool executor)."""
        with self._lock:
            for user_id, user_actions in self._actions.items():
                if name in user_actions:
                    return user_actions[name]
        return None

    def get_action_for_user(self, user_id: str, name: str) -> Optional[dict]:
        """Look up a custom action by name for a specific user."""
        with self._lock:
            return self._actions.get(user_id, {}).get(name)

    def get_all_actions(self, user_id: str) -> List[dict]:
        """Return all custom actions for a user."""
        with self._lock:
            return list(self._actions.get(user_id, {}).values())

    def is_custom_tool(self, name: str) -> bool:
        """Check if a tool name is a registered custom action (any user)."""
        with self._lock:
            for user_actions in self._actions.values():
                if name in user_actions:
                    return True
        return False

    def increment_usage(self, name: str) -> None:
        """Track usage count and last_used timestamp for a custom action."""
        now = datetime.now().isoformat()
        with self._lock:
            for user_actions in self._actions.values():
                if name in user_actions:
                    user_actions[name]["usage_count"] = user_actions[name].get("usage_count", 0) + 1
                    user_actions[name]["last_used"] = now
                    break

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute_action(self, name: str, params: list) -> str:
        """
        Execute a custom action by name.

        Supports execution types:
          - "shell": Run as a shell command via subprocess.
          - "powershell": Run as a PowerShell command.
          - "compose": Chain existing tools (future).

        Returns:
            str: Result message.
        """
        action_def = self.get_action(name)
        if not action_def:
            return f"Unknown custom action: {name}"

        execution = action_def.get("execution", {})
        exec_type = execution.get("type", "shell")
        command = execution.get("command", "")

        if not command:
            return f"Action '{name}' has no command defined."

        # Increment usage counter
        self.increment_usage(name)

        try:
            if exec_type == "shell":
                return self._execute_shell(command, params)
            elif exec_type == "powershell":
                return self._execute_powershell(command, params)
            elif exec_type == "compose":
                return self._execute_compose(command, params)
            else:
                return f"Unknown execution type: {exec_type}"
        except Exception as e:
            logger.error("[CUSTOM-ACTIONS] Execution failed for '%s': %s", name, e)
            return f"Action '{name}' failed: {e}"

    def _execute_shell(self, command: str, params: list) -> str:
        """Execute a shell command."""
        # Substitute {0}, {1}, etc. with params if provided
        if params:
            for i, param in enumerate(params):
                command = command.replace(f"{{{i}}}", str(param))

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            return output if output else "Done."
        else:
            error = result.stderr.strip()
            return f"Command failed (exit {result.returncode}): {error}" if error else f"Command exited with code {result.returncode}."

    def _execute_powershell(self, command: str, params: list) -> str:
        """Execute a PowerShell command."""
        if params:
            for i, param in enumerate(params):
                command = command.replace(f"{{{i}}}", str(param))

        result = subprocess.run(
            ["powershell", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            return output if output else "Done."
        else:
            error = result.stderr.strip()
            return f"PowerShell failed: {error}" if error else f"PowerShell exited with code {result.returncode}."

    def _execute_compose(self, command: str, params: list) -> str:
        """
        Execute a composition of existing tools.
        Format: "tool1(param1), tool2(param2)"
        """
        # Lazy import to avoid circular dependency
        from app.services.tools.tool_executor import execute_action as _exec

        results = []
        steps = [s.strip() for s in command.split(",") if s.strip()]
        for step in steps:
            # Parse "tool_name(param)" format
            paren_idx = step.find("(")
            if paren_idx == -1:
                results.append(f"Skipped (invalid format): {step}")
                continue
            tool_name = step[:paren_idx].strip()
            close_paren = step.find(")", paren_idx)
            if close_paren == -1:
                results.append(f"Skipped (missing closing paren): {step}")
                continue
            raw_param = step[paren_idx + 1:close_paren].strip().strip("\"'")
            step_params = [raw_param] if raw_param else []
            result = _exec(tool_name, step_params)
            results.append(result)

        return " | ".join(results) if results else "Done."

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------
    def _check_rate_limit(self, user_id: str) -> bool:
        """Check if the user has exceeded the rate limit (5 creations/minute)."""
        from config import CUSTOM_ACTIONS_RATE_LIMIT
        now = time.time()
        with self._lock:
            times = self._creation_times.get(user_id, [])
            # Remove entries older than 60 seconds
            times = [t for t in times if now - t < 60]
            self._creation_times[user_id] = times
            return len(times) < CUSTOM_ACTIONS_RATE_LIMIT

    def _record_creation(self, user_id: str) -> None:
        """Record a creation timestamp for rate limiting."""
        with self._lock:
            self._creation_times.setdefault(user_id, []).append(time.time())


# Global singleton instance
custom_action_manager = CustomActionManager()
