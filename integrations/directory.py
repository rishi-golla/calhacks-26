"""Counterparty directory: real carriers/shippers discovered from public records, cached in data/world.json."""
import json
import os
import pathlib


def path() -> pathlib.Path:
    return pathlib.Path(os.getenv("WORLD_FILE", "data/world.json"))


def load() -> dict:
    p = path()
    return json.loads(p.read_text()) if p.exists() else {"carriers": [], "shippers": []}


def save(world: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(world, indent=1))
    tmp.replace(p)


def get(company_id: str) -> dict | None:
    w = load()
    return next((c for c in w["carriers"] + w["shippers"] if c["id"] == company_id), None)


def by_usdot(usdot: str) -> dict | None:
    return next((c for c in load()["carriers"] if c.get("usdot") == str(usdot)), None)
