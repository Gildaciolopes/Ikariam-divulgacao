"""Incremental structured diagnostics for post-run analysis."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_WRITE_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_KEY_MARKERS = ("password", "passwd", "secret", "token", "cookie", "authorization", "credential")


def _sanitize(value: Any, *, key: str = "") -> Any:
    key_lower = key.casefold()
    if any(marker in key_lower for marker in _SECRET_KEY_MARKERS):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _EMAIL_RE.sub("[conta]", value)[:4000]
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    return _EMAIL_RE.sub("[conta]", str(value))[:1000]


class DetailedAuditLog:
    """Append-only JSONL writer shared by all accounts in one bot run."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id

    def write(self, event: str, *, account_id: str | None = None, **data: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "epoch": round(time.time(), 3),
            "run_id": self.run_id,
            "event": str(event),
            "account_id": str(account_id) if account_id else None,
            "data": _sanitize(data),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with _WRITE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(line + "\n")
