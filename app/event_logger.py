"""EventLogger — per-run NDJSON event stream + human-readable log."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time()*1000)%1000:03d}"


class EventLogger:
    """Writes events.ndjson (structured) and log.txt (readable) for one run."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ndjson_path = run_dir / "events.ndjson"
        self.log_path = run_dir / "log.txt"

    def emit(self, event_type: str, data: dict | None = None, **kw) -> dict:
        event = {"ts": _now(), "id": uuid.uuid4().hex[:8], "type": event_type, **(data or {}), **kw}
        with self.ndjson_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        readable = f"[{event['ts']}] {event_type} " + " ".join(
            f"{k}={v}" for k, v in event.items() if k not in ("ts", "type", "id")
        )
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(readable + "\n")
        return event

    def events(self) -> list[dict]:
        if not self.ndjson_path.exists():
            return []
        out = []
        for line in self.ndjson_path.read_text("utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


def read_events(run_dir: Path) -> list[dict]:
    return EventLogger(run_dir).events()
