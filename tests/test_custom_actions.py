"""Integration test for the Custom Action Engine."""
import sys
sys.path.insert(0, ".")

print("=" * 50)
print("Custom Action Engine — Integration Test")
print("=" * 50)

# 1. Import all modules
from app.services.tools.custom_action_manager import custom_action_manager
from app.services.tools.action_builder import action_builder
from app.services.tools.tool_schema import tool_registry, ToolDefinition
print("[PASS] All imports successful")

# 2. Create a custom action
ok, msg = custom_action_manager.create_action("test_user", {
    "name": "test_open_notepad",
    "description": "Open Notepad",
    "execution": {"type": "shell", "command": "notepad", "platform": "windows"},
    "trigger_phrases": ["open notepad"],
})
assert ok, f"Create failed: {msg}"
print(f"[PASS] Create action: {msg}")

# 3. Look it up
action = custom_action_manager.get_action("test_open_notepad")
assert action is not None
assert action["name"] == "test_open_notepad"
print("[PASS] Get action works")

# 4. is_custom_tool
assert custom_action_manager.is_custom_tool("test_open_notepad")
assert not custom_action_manager.is_custom_tool("nonexistent_tool")
print("[PASS] is_custom_tool works")

# 5. get_all_actions
actions = custom_action_manager.get_all_actions("test_user")
assert len(actions) == 1
print(f"[PASS] get_all_actions: {len(actions)} action(s)")

# 6. Persistence — check file exists
from config import CUSTOM_ACTIONS_DIR
import json
filepath = CUSTOM_ACTIONS_DIR / "test_user.json"
assert filepath.exists(), "JSON file not created"
with open(filepath, "r") as f:
    data = json.load(f)
assert len(data["actions"]) == 1
print("[PASS] Persistence: JSON file written correctly")

# 7. Register in tool registry
tool_registry.register_custom_action(action)
assert tool_registry.has("test_open_notepad")
tool_def = tool_registry.get("test_open_notepad")
assert tool_def.category == "custom"
print("[PASS] Tool registry: custom tool registered with category='custom'")

# 8. Validate action safety
is_safe, reason = action_builder.validate_action(action)
assert is_safe, f"Safe action flagged as dangerous: {reason}"
print(f"[PASS] Validate safe action: {reason}")

# 9. Validate dangerous action is blocked
dangerous_action = {
    "name": "bad_tool",
    "execution": {"command": "rm -rf /"},
}
is_safe2, reason2 = action_builder.validate_action(dangerous_action)
assert not is_safe2, "Dangerous action should be blocked"
print(f"[PASS] Validate dangerous action blocked: {reason2}")

# 10. Generate tool name
name = action_builder.generate_tool_name("Open Chrome New Window")
assert name == "open_chrome_new_window"
print(f"[PASS] Generate tool name: '{name}'")

# 11. Rate limit check (should pass — under limit)
assert custom_action_manager._check_rate_limit("test_user")
print("[PASS] Rate limit check passes (under limit)")

# 12. Delete action
ok2, msg2 = custom_action_manager.delete_action("test_user", "test_open_notepad")
assert ok2, f"Delete failed: {msg2}"
assert not custom_action_manager.is_custom_tool("test_open_notepad")
tool_registry.unregister("test_open_notepad")
assert not tool_registry.has("test_open_notepad")
print(f"[PASS] Delete action: {msg2}")

# 13. Verify file is empty after delete
with open(filepath, "r") as f:
    data2 = json.load(f)
assert len(data2["actions"]) == 0
print("[PASS] Persistence: JSON file empty after delete")

# Cleanup
import os
os.remove(filepath)
print("[PASS] Cleanup: test file removed")

print("=" * 50)
print("ALL 13 TESTS PASSED")
print("=" * 50)
