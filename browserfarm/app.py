from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger(__name__)

DEFAULT_ALLOWED_HOSTS = (
    "127.0.0.1,localhost,safer.fmcsa.dot.gov,www.sec.gov,api.nessieisreal.com"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
    "GoldenGateFreightDemo/1.0 (demo@goldengatefreight.example)"
)
NODE_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
BOUNDARY = b"--frame\r\n"
FRAME_END = b"\r\n"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BrowserSession:
    node_id: str
    page: Any
    cdp: Any
    url: str = ""
    updated_at: str = field(default_factory=_now)
    latest_frame: bytes | None = None
    viewers: set[asyncio.Queue[bytes | None]] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class BrowserFarm:
    def __init__(self) -> None:
        configured_hosts = os.getenv("BROWSERFARM_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
        self.allowed_hosts = {
            host.strip().lower().rstrip(".") for host in configured_hosts.split(",") if host.strip()
        }
        self.user_agent = os.getenv("BROWSERFARM_USER_AGENT") or DEFAULT_USER_AGENT
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.sessions: dict[str, BrowserSession] = {}
        self.sessions_lock = asyncio.Lock()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(headless=True)
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=self.user_agent,
            )
        except Exception:
            await self.playwright.stop()
            self.playwright = None
            raise

    async def close(self) -> None:
        async with self.sessions_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await self._close_session(session)
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    def validate_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError as exc:
            raise HTTPException(400, "invalid URL") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            raise HTTPException(400, "URL must use http or https and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise HTTPException(400, "URL credentials are not allowed")
        if host not in self.allowed_hosts:
            raise HTTPException(400, "host is not allowlisted")

    async def get_or_create_session(self, node_id: str) -> BrowserSession:
        async with self.sessions_lock:
            existing = self.sessions.get(node_id)
            if existing is not None and not existing.closed and not existing.page.is_closed():
                return existing
            if existing is not None:
                self.sessions.pop(node_id, None)
                await self._close_session(existing)
            if self.context is None:
                raise RuntimeError("browser farm is not started")

            page = await self.context.new_page()
            cdp = await self.context.new_cdp_session(page)
            session = BrowserSession(node_id=node_id, page=page, cdp=cdp)

            def on_frame(event: dict[str, Any]) -> None:
                asyncio.create_task(self._handle_screencast_frame(session, event))

            cdp.on("Page.screencastFrame", on_frame)
            try:
                await cdp.send("Page.enable")
                await cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 60,
                        "maxWidth": 1280,
                        "maxHeight": 800,
                    },
                )
            except Exception:
                await page.close()
                raise
            self.sessions[node_id] = session
            return session

    async def navigate(self, node_id: str, url: str) -> dict[str, str]:
        from playwright.async_api import Error as PlaywrightError

        session = await self.get_or_create_session(node_id)
        async with session.lock:
            if session.page.url == url:
                session.url = session.page.url
                title = await session.page.title()
                return {"node_id": node_id, "url": session.url, "title": title}

            error: str | None = None
            try:
                await session.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            except PlaywrightError as exc:
                error = str(exc)

            try:
                frame = await session.page.screenshot(type="jpeg", quality=60)
                await self._publish_frame(session, frame)
            except PlaywrightError as exc:
                if error is None:
                    error = str(exc)

            session.url = session.page.url if session.page.url != "about:blank" else url
            session.updated_at = _now()
            try:
                title = await session.page.title()
            except PlaywrightError as exc:
                title = ""
                if error is None:
                    error = str(exc)

            result = {"node_id": node_id, "url": session.url, "title": title}
            if error is not None:
                result["error"] = error
            return result

    async def list_sessions(self) -> list[dict[str, str]]:
        from playwright.async_api import Error as PlaywrightError

        async with self.sessions_lock:
            sessions = list(self.sessions.values())
        result = []
        for session in sessions:
            if session.closed or session.page.is_closed():
                continue
            page_url = session.page.url
            if page_url and page_url != "about:blank" and page_url != session.url:
                session.url = page_url
                session.updated_at = _now()
            try:
                title = await session.page.title()
            except PlaywrightError:
                title = ""
            result.append(
                {
                    "node_id": session.node_id,
                    "url": session.url,
                    "title": title,
                    "updated_at": session.updated_at,
                }
            )
        return result

    async def delete_session(self, node_id: str) -> None:
        async with self.sessions_lock:
            session = self.sessions.pop(node_id, None)
        if session is not None:
            await self._close_session(session)

    async def _handle_screencast_frame(
        self, session: BrowserSession, event: dict[str, Any]
    ) -> None:
        from playwright.async_api import Error as PlaywrightError

        try:
            await self._publish_frame(session, base64.b64decode(event["data"]))
        except Exception:
            log.exception("Failed to process browserfarm frame for %s", session.node_id)
        finally:
            session_id = event.get("sessionId")
            if session_id is not None:
                try:
                    await session.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except PlaywrightError:
                    log.debug("Could not acknowledge closed screencast for %s", session.node_id)

    async def _publish_frame(self, session: BrowserSession, frame: bytes) -> None:
        session.latest_frame = frame
        session.updated_at = _now()
        for queue in tuple(session.viewers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    async def _close_session(self, session: BrowserSession) -> None:
        from playwright.async_api import Error as PlaywrightError

        if session.closed:
            return
        session.closed = True
        for queue in tuple(session.viewers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        try:
            await session.cdp.send("Page.stopScreencast")
        except PlaywrightError:
            log.debug("Could not stop screencast for closed page %s", session.node_id)
        try:
            await session.page.close()
        except PlaywrightError:
            log.debug("Could not close page %s", session.node_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    farm = BrowserFarm()
    await farm.start()
    app.state.farm = farm
    try:
        yield
    finally:
        await farm.close()


app = FastAPI(title="browserfarm", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


def _validate_node_id(node_id: str) -> None:
    if NODE_ID_RE.fullmatch(node_id) is None:
        raise HTTPException(400, "invalid node_id")


def _farm(request: Request) -> BrowserFarm:
    return request.app.state.farm


@app.post("/sessions/{node_id}")
async def open_session(node_id: str, request: Request) -> dict[str, str]:
    _validate_node_id(node_id)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        raise HTTPException(400, "JSON body must include a URL string")
    url = body["url"]
    farm = _farm(request)
    farm.validate_url(url)
    return await farm.navigate(node_id, url)


@app.delete("/sessions/{node_id}", status_code=204)
async def delete_session(node_id: str, request: Request) -> Response:
    _validate_node_id(node_id)
    await _farm(request).delete_session(node_id)
    return Response(status_code=204)


@app.get("/sessions")
async def get_sessions(request: Request) -> list[dict[str, str]]:
    return await _farm(request).list_sessions()


@app.get("/frame/{node_id}.jpg")
async def get_frame(node_id: str, request: Request) -> Response:
    _validate_node_id(node_id)
    session = _farm(request).sessions.get(node_id)
    if session is None or session.latest_frame is None:
        raise HTTPException(404, "frame not available")
    return Response(
        content=session.latest_frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/stream/{node_id}")
async def get_stream(node_id: str, request: Request) -> StreamingResponse:
    _validate_node_id(node_id)
    session = _farm(request).sessions.get(node_id)
    if session is None or session.closed:
        raise HTTPException(404, "session not found")

    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
    session.viewers.add(queue)
    if session.latest_frame is not None:
        queue.put_nowait(session.latest_frame)

    async def stream() -> AsyncIterator[bytes]:
        last_frame: bytes | None = None
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=2.0)
                except TimeoutError:
                    frame = session.latest_frame or last_frame
                    if frame is None:
                        if session.closed:
                            break
                        continue
                if frame is None:
                    break
                last_frame = frame
                yield (
                    BOUNDARY
                    + b"Content-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n"
                    + frame
                    + FRAME_END
                )
        finally:
            session.viewers.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("BROWSERFARM_HOST", "127.0.0.1"),
        port=int(os.getenv("BROWSERFARM_PORT", "9300")),
    )
