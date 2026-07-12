"""
Action Manager for J.A.R.V.I.S.
=================================
Queues, tracks, retries, and cancels multi-step workflows.

When the user says "Take a picture and send it to Mom", JARVIS creates:
  Task #1042
    Action 1: camera.capture()
    Action 2: contacts.search("Mom")
    Action 3: messages.send()

The Action Manager executes each step, handles failures, and emits
events for progress tracking. This replaces ad-hoc tool execution
with structured, observable workflows.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.services.event_bus import event_bus

logger = logging.getLogger("J.A.R.V.I.S")


class ActionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Action — a single step in a task
# ---------------------------------------------------------------------------
@dataclass
class Action:
    action_id: str
    task_id: str
    tool_name: str
    params: dict = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "params": self.params,
            "status": self.status.value,
            "result": str(self.result)[:200] if self.result else None,
            "error": self.error,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


# ---------------------------------------------------------------------------
# Task — a multi-step workflow
# ---------------------------------------------------------------------------
@dataclass
class Task:
    task_id: str
    goal: str
    actions: List[Action] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    device_id: Optional[str] = None   # target device (if any)

    @property
    def progress(self) -> str:
        """Return progress as 'completed/total'."""
        total = len(self.actions)
        done = sum(1 for a in self.actions if a.status == ActionStatus.COMPLETED)
        return f"{done}/{total}"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value,
            "progress": self.progress,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "device_id": self.device_id,
            "actions": [a.to_dict() for a in self.actions],
        }


# ---------------------------------------------------------------------------
# Action Manager
# ---------------------------------------------------------------------------
class ActionManager:
    """
    Manages multi-step task execution with retry, cancellation, and events.
    Thread-safe.
    """

    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.RLock()
        self._executor: Optional[Callable] = None  # set to tool_executor.execute_action

    def set_executor(self, executor_func: Callable) -> None:
        """Wire up the tool executor function. Called at startup."""
        self._executor = executor_func

    # ------------------------------------------------------------------
    # Create task
    # ------------------------------------------------------------------
    def create_task(
        self,
        goal: str,
        steps: List[dict],
        device_id: Optional[str] = None,
    ) -> Task:
        """
        Create a new task with the given steps.

        Args:
            goal: Human-readable description of what the task achieves.
            steps: List of {"tool": "tool_name", "params": {...}, "max_retries": int}
            device_id: Optional target device.

        Returns:
            The created Task.
        """
        task_id = uuid.uuid4().hex[:8]
        actions = []
        for step in steps:
            action = Action(
                action_id=uuid.uuid4().hex[:8],
                task_id=task_id,
                tool_name=step["tool"],
                params=step.get("params", {}),
                max_retries=step.get("max_retries", 0),
            )
            actions.append(action)

        task = Task(
            task_id=task_id,
            goal=goal,
            actions=actions,
            device_id=device_id,
        )

        with self._lock:
            self._tasks[task_id] = task

        logger.info("[ACTION-MGR] Created task %s: %s (%d actions)",
                     task_id, goal, len(actions))
        event_bus.emit("workflow_started", {
            "task_id": task_id,
            "goal": goal,
            "action_count": len(actions),
        })
        return task

    # ------------------------------------------------------------------
    # Execute task (synchronous — runs in calling thread)
    # ------------------------------------------------------------------
    def execute_task(self, task_id: str) -> Task:
        """
        Execute all actions in a task sequentially.
        Handles retries for failed actions.
        Returns the task with updated statuses.
        """
        task = self._tasks.get(task_id)
        if not task:
            logger.error("[ACTION-MGR] Task %s not found", task_id)
            return None

        if not self._executor:
            logger.error("[ACTION-MGR] No executor set. Call set_executor() first.")
            task.status = TaskStatus.FAILED
            return task

        task.status = TaskStatus.RUNNING
        logger.info("[ACTION-MGR] Executing task %s: %s", task_id, task.goal)

        for action in task.actions:
            if task.status == TaskStatus.CANCELLED:
                action.status = ActionStatus.CANCELLED
                continue

            self._execute_action(action)

            if action.status == ActionStatus.FAILED:
                # Check if we should retry
                if action.retry_count <= action.max_retries:
                    logger.info("[ACTION-MGR] Retrying action %s (%d/%d)",
                                action.action_id, action.retry_count, action.max_retries)
                    self._execute_action(action)

                if action.status == ActionStatus.FAILED:
                    task.status = TaskStatus.FAILED
                    logger.warning("[ACTION-MGR] Task %s failed at action %s (%s)",
                                   task_id, action.action_id, action.tool_name)
                    event_bus.emit("workflow_failed", {
                        "task_id": task_id,
                        "goal": task.goal,
                        "failed_action": action.tool_name,
                        "error": action.error,
                    })
                    return task

        # All actions completed
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now()
        logger.info("[ACTION-MGR] Task %s completed: %s", task_id, task.goal)
        event_bus.emit("workflow_completed", {
            "task_id": task_id,
            "goal": task.goal,
            "progress": task.progress,
        })
        return task

    def _execute_action(self, action: Action) -> None:
        """Execute a single action using the wired executor."""
        action.status = ActionStatus.RUNNING
        action.started_at = datetime.now()

        event_bus.emit("action_started", {
            "action_id": action.action_id,
            "task_id": action.task_id,
            "tool": action.tool_name,
        })

        try:
            # Build params list from dict for legacy executor compatibility
            params_list = list(action.params.values()) if action.params else []
            result = self._executor(action.tool_name, params_list)

            if result and isinstance(result, str) and result.startswith("Tool error"):
                action.status = ActionStatus.FAILED
                action.error = result
                action.retry_count += 1
                event_bus.emit("action_failed", {
                    "action_id": action.action_id,
                    "tool": action.tool_name,
                    "error": result,
                })
            else:
                action.status = ActionStatus.COMPLETED
                action.result = result
                action.completed_at = datetime.now()
                event_bus.emit("action_completed", {
                    "action_id": action.action_id,
                    "tool": action.tool_name,
                })

        except Exception as e:
            action.status = ActionStatus.FAILED
            action.error = str(e)
            action.retry_count += 1
            logger.error("[ACTION-MGR] Action %s error: %s", action.action_id, e)
            event_bus.emit("action_failed", {
                "action_id": action.action_id,
                "tool": action.tool_name,
                "error": str(e),
            })

    # ------------------------------------------------------------------
    # Cancel task
    # ------------------------------------------------------------------
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending or running task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = TaskStatus.CANCELLED
            for action in task.actions:
                if action.status in (ActionStatus.PENDING, ActionStatus.RUNNING):
                    action.status = ActionStatus.CANCELLED
        logger.info("[ACTION-MGR] Cancelled task %s", task_id)
        event_bus.emit("workflow_failed", {
            "task_id": task_id,
            "reason": "cancelled",
        })
        return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_active_tasks(self) -> List[Task]:
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]

    def get_all_tasks(self, limit: int = 50) -> List[Task]:
        with self._lock:
            tasks = sorted(self._tasks.values(),
                           key=lambda t: t.created_at, reverse=True)
            return tasks[:limit]

    def to_api_list(self, limit: int = 50) -> list:
        """Serialize tasks for the /api/tasks endpoint."""
        return [t.to_dict() for t in self.get_all_tasks(limit)]


# Global action manager instance
action_manager = ActionManager()
