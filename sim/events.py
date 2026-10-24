import json
import os
from datetime import UTC, datetime
from pathlib import Path


def iso_z(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def append_event(event: dict) -> None:
    path = Path(os.getenv("SIM_DATA_DIR", "data")) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = (json.dumps(event, default=str, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, record)
    finally:
        os.close(fd)
