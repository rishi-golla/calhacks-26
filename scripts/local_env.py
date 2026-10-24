"""Load local development env files without executing shell expressions or printing secrets."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for index, line in enumerate(path.read_text().splitlines(), 1):
        try:
            parts = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError(f"Invalid env syntax in {path.name} line {index}") from None
        if parts and parts[0] == "export":
            parts = parts[1:]
        if not parts:
            continue
        if len(parts) != 1 or "=" not in parts[0]:
            raise ValueError(f"Expected one assignment in {path.name} line {index}")
        name, value = parts[0].split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid variable name in {path.name} line {index}")
        values[name] = value
    return values


def load_live_env() -> None:
    """Use the project key file plus the locally provisioned service credentials."""
    os.environ.update(read_env(ROOT / ".env"))
    database = os.environ.get("SPACETIME_DB", "")
    if not re.fullmatch(r"[a-z0-9-]+", database):
        raise ValueError("Configure a local SPACETIME_DB name")
    os.environ.update(read_env(ROOT / "data/w2" / database / "services.env"))
    os.environ.update(read_env(ROOT / "data/live/runtime.env"))
