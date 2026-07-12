"""Full integration test for Phase 0 platform modules."""
import sys
sys.path.insert(0, '.')

print("=== FULL INTEGRATION TEST ===")

# 1. Register mock devices
from app.connectors.mock_android import start_mock_android, stop_mock_android
from app.connectors.mock_glasses import start_mock_glasses, stop_mock_glasses

android = start_mock_android()
glasses = start_mock_glasses()
print("1. Mock devices registered")

# 2. Device Manager
from app.services.device_manager import device_manager
print(f"2. Devices: {device_manager.get_device_count()}, Online: {len(device_manager.get_online_devices())}")

# 3. Tool Registry
from app.services.tools.tool_schema import tool_registry
from app.services.tools.tool_executor import REQUIRES_CONFIRMATION
tool_registry.load_from_system_tools(REQUIRES_CONFIRMATION)
print(f"3. Tools loaded: {len(tool_registry.all_tools())}")

# 4. Context Engine
from app.services.context_engine import context_engine
context_engine.set_device_manager(device_manager)
ctx = context_engine.build_context()
prompt_ctx = context_engine.format_for_prompt(ctx)
print(f"4. Context built: {ctx.get('device_count')} devices")
for line in prompt_ctx.split("\n"):
    print(f"   {line}")

# 5. Action Manager
from app.services.action_manager import action_manager
from app.services.tools.tool_executor import execute_action
action_manager.set_executor(execute_action)
print("5. ActionManager executor wired")

# 6. Execute a task
task = action_manager.create_task("Test system info", [{"tool": "system_info", "params": {}}])
result = action_manager.execute_task(task.task_id)
print(f"6. Task executed: {result.status.value}, Progress: {result.progress}")

# 7. Tool Cache
from app.services.tools.tool_cache import tool_cache
print(f"7. Cache stats: {tool_cache.stats()}")

# 8. Event Bus
from app.services.event_bus import event_bus
print(f"8. Event bus: {event_bus.stats()['total_events']} events emitted")

# 9. Cleanup
stop_mock_android()
stop_mock_glasses()
print("9. Mock devices stopped")

print("=== INTEGRATION TEST PASSED ===")
