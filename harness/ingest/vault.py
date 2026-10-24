"""Local SQLite token vault. Real values never leave the ASUS box; prompts and envelopes carry tokens only."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("HARNESS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TOKEN_RE = re.compile(r"\[\[([A-Z]+)_([0-9a-f]{8})\]\]")


class Vault:
    """Deterministic tokens: the same value always maps to the same token (HMAC with a per-vault key),
    so downstream agents can still correlate, e.g. "same bank account as last month"."""

    def __init__(self, path: Path | None = None) -> None:
        path = path or DATA_DIR / "vault.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        if new:
            os.chmod(path, 0o600)
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);"
            "CREATE TABLE IF NOT EXISTS vault(token TEXT PRIMARY KEY, kind TEXT, value TEXT, first_seen REAL);")
        row = self._db.execute("SELECT v FROM meta WHERE k='hmac_key'").fetchone()
        if row is None:
            key = secrets.token_hex(32)
            self._db.execute("INSERT INTO meta VALUES('hmac_key', ?)", (key,))
            self._db.commit()
        else:
            key = row[0]
        self._key = bytes.fromhex(key)

    def tokenize(self, kind: str, value: str) -> str:
        if TOKEN_RE.fullmatch(value):
            return value
        digest = hmac.new(self._key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:8]
        token = f"[[{kind}_{digest}]]"
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO vault VALUES(?,?,?,?)", (token, kind, value, time.time()))
            self._db.commit()
        return token

    def lookup(self, token: str) -> str | None:
        row = self._db.execute("SELECT value FROM vault WHERE token=?", (token,)).fetchone()
        return row[0] if row else None

    def detokenize(self, text: str) -> str:
        """Local use only (e.g. the review UI on ASUS). Never call before sending anything off-box."""
        return TOKEN_RE.sub(lambda m: self.lookup(m.group(0)) or m.group(0), text)
