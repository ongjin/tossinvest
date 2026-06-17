from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class AuditLog:
    """Append-only JSONL record of every write-tool decision. Trust/debug/blog evidence."""

    def __init__(
        self,
        path: "str | Path",
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._path = Path(path)
        self._now = now

    def record(self, event: dict) -> None:
        entry = {"ts": self._now().isoformat(), **event}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
