#!/usr/bin/env python3
"""
Persistent alert storage for inverter-control
"""
import json
import os
import threading
from datetime import UTC, datetime
from typing import List, Optional
from uuid import uuid4

from .config import ALERT_STORAGE_PATH


class PersistentAlert:
    """Represents a single alert with full metadata for persistence"""

    def __init__(
        self,
        id: str,
        title: str,
        body: str,
        level: str,
        source: str,
        timestamp: str,
        acknowledged: bool = False,
        acknowledged_at: Optional[str] = None,
    ):
        self.id = id
        self.title = title
        self.body = body
        self.level = level  # "info", "warning", "alarm"
        self.source = source
        self.timestamp = timestamp  # RFC 3339 timestamp when alert triggered
        self.acknowledged = acknowledged  # Whether inverter-desktop has acknowledged it
        self.acknowledged_at = acknowledged_at  # When acknowledged (RFC 3339)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "level": self.level,
            "source": self.source,
            "timestamp": self.timestamp,
            "acknowledged": self.acknowledged,
            "acknowledged_at": self.acknowledged_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PersistentAlert":
        """Create from dictionary (loaded from JSON)"""
        return cls(
            id=data["id"],
            title=data["title"],
            body=data["body"],
            level=data["level"],
            source=data["source"],
            timestamp=data["timestamp"],
            acknowledged=data.get("acknowledged", False),
            acknowledged_at=data.get("acknowledged_at"),
        )


class AlertStorage:
    """Manages persistent storage and retrieval of alerts"""

    def __init__(self, storage_path: str = ALERT_STORAGE_PATH):
        self.storage_path = storage_path
        self.alerts: List[PersistentAlert] = []
        self._lock = threading.RLock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load existing alerts from disk on initialization"""
        with self._lock:
            if not os.path.exists(self.storage_path):
                self.alerts = []
                return

            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.alerts = [
                        PersistentAlert.from_dict(item) for item in data
                    ]
            except (json.JSONDecodeError, IOError) as e:
                # If file is corrupt, start with empty list and log error
                # In a real application, we would log this
                print(f"Warning: Could not load alert storage: {e}")
                self.alerts = []

    def _save_to_disk(self) -> None:
        """Save alerts to disk"""
        with self._lock:
            try:
                # Ensure directory exists
                os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
                with open(self.storage_path, "w", encoding="utf-8") as f:
                    json.dump(
                        [alert.to_dict() for alert in self.alerts],
                        f,
                        indent=2,
                    )
            except IOError as e:
                # In a real application, we would log this
                print(f"Warning: Could not save alert storage: {e}")

    def add_alert(
        self,
        title: str,
        body: str,
        level: str,
        source: str = "inverter-control",
    ) -> PersistentAlert:
        """Add a new alert and persist it"""
        alert = PersistentAlert(
            id=str(uuid4()),
            title=title,
            body=body,
            level=level,
            source=source,
            timestamp=datetime.now(UTC).isoformat(),
        )

        with self._lock:
            self.alerts.append(alert)
            self._save_to_disk()

        return alert

    def get_unacknowledged_alerts(self) -> List[PersistentAlert]:
        """Get all alerts that have not been acknowledged"""
        with self._lock:
            return [alert for alert in self.alerts if not alert.acknowledged]

    def acknowledge_alert(self, alert_id: str) -> bool:
        """Mark an alert as acknowledged"""
        with self._lock:
            for alert in self.alerts:
                if alert.id == alert_id:
                    alert.acknowledged = True
                    alert.acknowledged_at = datetime.now(UTC).isoformat()
                    self._save_to_disk()
                    return True
            return False

    def get_alert_history(self, limit: Optional[int] = None) -> List[PersistentAlert]:
        """Get alert history, newest first"""
        with self._lock:
            # Sort by timestamp descending (newest first)
            sorted_alerts = sorted(
                self.alerts, key=lambda a: a.timestamp, reverse=True
            )
            if limit is not None:
                return sorted_alerts[:limit]
            return sorted_alerts


# Global instance for easy access
_alert_storage: Optional[AlertStorage] = None


def get_alert_storage() -> AlertStorage:
    """Get or create the global alert storage instance"""
    global _alert_storage
    if _alert_storage is None:
        _alert_storage = AlertStorage()
    return _alert_storage