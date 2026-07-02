"""
PC Health Monitor for J.A.R.V.I.S.
Background service that continuously monitors system health and reports issues.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("J.A.R.V.I.S.Health")


class HealthMonitor:
    """Monitors PC health: CPU, RAM, disk, battery, top processes."""

    def __init__(self, interval: int = 60, history_size: int = 10):
        self.interval = interval  # seconds between checks
        self.history_size = history_size
        self._snapshots: deque = deque(maxlen=history_size)
        self._alerts: list = []
        self._high_cpu_count = 0  # consecutive high CPU readings
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """Start the background health monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("[HEALTH] Monitor started (interval=%ds)", self.interval)

    def stop(self):
        """Stop the background monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("[HEALTH] Monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop - runs every `interval` seconds."""
        while self._running:
            try:
                snapshot = self._take_snapshot()
                self._snapshots.append(snapshot)
                self._evaluate_alerts(snapshot)
            except Exception as e:
                logger.warning("[HEALTH] Snapshot failed: %s", e)
            await asyncio.sleep(self.interval)

    def _take_snapshot(self) -> dict:
        """Collect current system metrics."""
        import psutil

        cpu_percent = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        battery = psutil.sensors_battery()

        # Disk usage for all mounted partitions
        disks = []
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "drive": part.device,
                    "total_gb": round(usage.total / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                pass

        # Top 5 CPU-consuming processes
        top_procs = []
        try:
            procs = sorted(
                psutil.process_iter(["name", "cpu_percent", "memory_percent"]),
                key=lambda p: p.info.get("cpu_percent", 0) or 0,
                reverse=True,
            )[:5]
            for p in procs:
                try:
                    info = p.info
                    top_procs.append({
                        "name": info.get("name", "?"),
                        "cpu": round(info.get("cpu_percent", 0) or 0, 1),
                        "ram": round(info.get("memory_percent", 0) or 0, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

        return {
            "timestamp": time.time(),
            "cpu_percent": cpu_percent,
            "ram_percent": ram.percent,
            "ram_used_gb": round(ram.used / (1024**3), 1),
            "ram_total_gb": round(ram.total / (1024**3), 1),
            "battery_percent": battery.percent if battery else None,
            "battery_plugged": battery.power_plugged if battery else None,
            "disks": disks,
            "top_processes": top_procs,
        }

    def _evaluate_alerts(self, snapshot: dict):
        """Check for concerning conditions and generate alerts."""
        self._alerts.clear()

        # CPU alert: >90% for 3+ consecutive checks
        if snapshot["cpu_percent"] > 90:
            self._high_cpu_count += 1
        else:
            self._high_cpu_count = 0

        if self._high_cpu_count >= 3:
            self._alerts.append(
                f"HIGH CPU: {snapshot['cpu_percent']}% for {self._high_cpu_count} consecutive checks"
            )

        # RAM alert: >90%
        if snapshot["ram_percent"] > 90:
            self._alerts.append(
                f"HIGH RAM: {snapshot['ram_percent']}% used ({snapshot['ram_used_gb']}/{snapshot['ram_total_gb']} GB)"
            )

        # Disk alert: any drive <10% free
        for disk in snapshot["disks"]:
            if disk["percent"] > 90:
                self._alerts.append(
                    f"LOW DISK: {disk['drive']} is {disk['percent']}% full ({disk['free_gb']} GB free)"
                )

        # Battery alert: <15% and not charging
        if (snapshot["battery_percent"] is not None
                and snapshot["battery_percent"] < 15
                and not snapshot["battery_plugged"]):
            self._alerts.append(
                f"LOW BATTERY: {snapshot['battery_percent']}% — plug in soon"
            )

    def get_health_status(self) -> dict:
        """Return the latest health snapshot."""
        if not self._snapshots:
            return {"status": "no_data", "message": "Health monitor has not collected data yet."}
        latest = self._snapshots[-1]
        return {
            "status": "ok" if not self._alerts else "warning",
            "cpu_percent": latest["cpu_percent"],
            "ram_percent": latest["ram_percent"],
            "ram_used_gb": latest["ram_used_gb"],
            "ram_total_gb": latest["ram_total_gb"],
            "battery_percent": latest["battery_percent"],
            "battery_plugged": latest["battery_plugged"],
            "disks": latest["disks"],
            "alerts": list(self._alerts),
        }

    def get_health_alerts(self) -> list:
        """Return current active alerts."""
        return list(self._alerts)

    def get_process_hogs(self, top: int = 5) -> list:
        """Return top CPU/RAM consuming processes."""
        if not self._snapshots:
            return []
        return self._snapshots[-1].get("top_processes", [])[:top]


# Singleton instance
_health_monitor: Optional[HealthMonitor] = None


def get_health_monitor() -> HealthMonitor:
    """Get or create the singleton health monitor."""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = HealthMonitor()
    return _health_monitor
