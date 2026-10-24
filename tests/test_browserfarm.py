from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from browserfarm.app import app

PlaywrightError = pytest.importorskip("playwright.async_api").Error


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://not-allowlisted.invalid/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        content = (
            b"<!doctype html><html><head><title>Browser farm fixture</title></head>"
            b"<body>ready</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def local_page_url() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def client(local_page_url):
    port = urlsplit(local_page_url).port
    with patch.dict(os.environ, {"BROWSERFARM_ALLOWED_HOSTS": f"127.0.0.1:{port}"}):
        test_client = TestClient(app)
        try:
            test_client.__enter__()
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        try:
            yield test_client
        finally:
            test_client.__exit__(None, None, None)


def test_post_navigates_and_sessions_show_url_and_title(client, local_page_url):
    response = client.post("/sessions/navigate", json={"url": local_page_url})

    assert response.status_code == 200
    assert response.json()["url"] == local_page_url
    assert response.json()["title"] == "Browser farm fixture"

    sessions = client.get("/sessions").json()
    session = next(item for item in sessions if item["node_id"] == "navigate")
    assert session["url"] == local_page_url
    assert session["title"] == "Browser farm fixture"
    assert session["updated_at"]


def test_frame_returns_jpeg_bytes(client, local_page_url):
    response = client.post("/sessions/frame", json={"url": local_page_url})
    assert response.status_code == 200

    frame = client.get("/frame/frame.jpg")
    assert frame.status_code == 200
    assert frame.headers["cache-control"] == "no-store"
    assert frame.content.startswith(b"\xff\xd8")


def test_stream_returns_multipart_and_a_frame(client, local_page_url):
    response = client.post("/sessions/stream", json={"url": local_page_url})
    assert response.status_code == 200

    with client.stream("GET", "/stream/stream") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith(
            "multipart/x-mixed-replace; boundary=frame"
        )
        first_chunk = next(stream.iter_raw())
        assert first_chunk.startswith(b"--frame\r\nContent-Type: image/jpeg")
        assert b"\xff\xd8" in first_chunk


@pytest.mark.parametrize(
    ("node_id", "url"),
    [
        ("disallowed", "https://example.com/"),
        ("file-url", "file:///etc/passwd"),
        ("bad!", "http://127.0.0.1/"),
    ],
)
def test_rejects_disallowed_urls_and_node_ids(client, node_id, url):
    response = client.post(f"/sessions/{node_id}", json={"url": url})

    assert response.status_code == 400


def test_rejects_localhost_on_a_different_port(client, local_page_url):
    port = urlsplit(local_page_url).port
    different_port = port % 65535 + 1

    response = client.post(
        "/sessions/wrong-port",
        json={"url": f"http://127.0.0.1:{different_port}/"},
    )

    assert response.status_code == 400


def test_blocks_redirect_to_disallowed_host(client, local_page_url):
    response = client.post(
        "/sessions/redirect",
        json={"url": f"{local_page_url}redirect"},
    )

    assert response.status_code == 200
    session = next(
        item for item in client.get("/sessions").json() if item["node_id"] == "redirect"
    )
    assert urlsplit(session["url"]).hostname != "not-allowlisted.invalid"


def test_delete_removes_session(client, local_page_url):
    assert client.post("/sessions/delete-me", json={"url": local_page_url}).status_code == 200

    deleted = client.delete("/sessions/delete-me")

    assert deleted.status_code == 204
    assert all(item["node_id"] != "delete-me" for item in client.get("/sessions").json())
