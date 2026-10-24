"""Agents call SpacetimeDB reducers over HTTP (PRD 7.4). W2 confirms the exact 2.x path."""
import logging
import os

import httpx

log = logging.getLogger(__name__)
URL = os.getenv("SPACETIME_URL", "http://localhost:3000")
DB = os.getenv("SPACETIME_DB", "freightops")
TOKEN = os.getenv("SPACETIME_TOKEN", "")


async def call_reducer(name: str, *args) -> bool:
    if os.getenv("SPACETIME_DISABLED", "1") == "1":
        log.debug("spacetime disabled; would call %s%s", name, args)
        return False
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(f"{URL}/v1/database/{DB}/call/{name}", json=list(args), headers=headers)
        if r.status_code >= 400:
            log.warning("reducer %s failed: %s %s", name, r.status_code, r.text[:200])
            return False
        return True
