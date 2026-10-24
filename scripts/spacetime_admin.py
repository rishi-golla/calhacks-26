"""Local W2 setup: persistent identities, safe publish, service grants, human OTPs.

Credentials are stored only in ignored data/w2/<database>/ with owner-only modes.
No global Spacetime CLI login/config is changed. Never pass tokens to the dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from common.policy import check
from common.spacetime_client import SpacetimeProtocolError, canonical_sql_column

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "carrier_ap", "shipper_ar", "cfo", "risk", "controller", "verification",
    "voice_desk", "validator", "documents", "ingest", "harness", "evaluator", "router",
)


def save_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(content)


class LocalAdmin:
    def __init__(self, server: str = "http://127.0.0.1:3000", database: str = "freightops"):
        if urlparse(server).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("This provisioning helper is for local development servers only")
        if not database or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in database):
            raise ValueError("Use a lowercase SpacetimeDB database name")
        self.server = server.rstrip("/")
        self.database = database
        self.directory = ROOT / "data" / "w2" / database
        self.path = self.directory / "identities.json"
        self.identities = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.client = httpx.Client(base_url=self.server, timeout=60)

    def identity(self, actor: str) -> dict:
        if actor not in self.identities:
            response = self.client.post("/v1/identity")
            response.raise_for_status()
            self.identities[actor] = response.json()
            save_private(self.path, json.dumps(self.identities, indent=2) + "\n")
        return self.identities[actor]

    def call(self, actor: str, reducer: str, payload: dict, *, expected_error: bool = False):
        decision = check(reducer, actor, payload)
        if decision.result != "allow":
            raise ValueError(f"Policy {decision.result}: {decision.reason}")
        return self.raw_call(actor, reducer, payload, expected_error=expected_error)

    def raw_call(self, actor: str, reducer: str, payload: dict, *, expected_error: bool = False):
        """Raw transport also used to test reducer controls independently of Python preflight."""
        body = {"write_id": str(uuid.uuid4()), **payload}
        response = self.client.post(
            f"/v1/database/{quote(self.database, safe='')}/call/{quote(reducer, safe='')}",
            headers={"Authorization": f"Bearer {self.identity(actor)['token']}"},
            json=[json.dumps(body, separators=(",", ":"))],
        )
        if expected_error:
            if response.is_success:
                raise AssertionError(f"{reducer} unexpectedly succeeded")
        elif not response.is_success:
            raise RuntimeError(f"{reducer} rejected ({response.status_code}): {response.text[:600]}")
        return response

    def rows(self, table: str, *, actor: str | None = None) -> list[dict]:
        if not table.replace("_", "").isalnum():
            raise ValueError("Invalid table")
        headers = {"Authorization": f"Bearer {self.identity(actor)['token']}"} if actor else {}
        response = self.client.post(
            f"/v1/database/{self.database}/sql", content=f'SELECT * FROM "{table}"', headers=headers
        )
        response.raise_for_status()
        result = response.json()[0]
        names = [canonical_sql_column(e["name"].get("some") if isinstance(e["name"], dict) else e["name"])
                 for e in result["schema"]["elements"]]
        if len(set(names)) != len(names):
            raise SpacetimeProtocolError("SpacetimeDB returned ambiguous SQL column names")
        return [dict(zip(names, row, strict=True)) for row in result["rows"]]

    def publish(self) -> None:
        admin = self.identity("admin")
        config = self.directory / "cli.toml"
        # This is a dedicated CLI config, never the user's global identity.
        result = subprocess.run(
            ["spacetime", "--config-path", str(config), "login", "--token", admin["token"]],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise RuntimeError("Could not configure the isolated local CLI identity")
        if config.exists():
            config.chmod(0o600)
        subprocess.run([
            "spacetime", "--config-path", str(config), "publish", "--no-config",
            "--server", self.server, "--module-path", str(ROOT / "spacetime/module/spacetimedb"),
            "--delete-data=never", "--yes", self.database,
        ], check=True)

    def grant(self, identity: str, role: str, actor: str) -> None:
        self.call("admin", "grant_role", {
            "identity": identity, "role": role, "actor": actor, "active": True,
        })

    def provision(self) -> None:
        for actor in SERVICES:
            self.grant(self.identity(actor)["identity"], actor, actor)
        # A distinct human identity keeps separation of duties testable.
        self.grant(self.identity("human:operator")["identity"], "human", "human:operator")
        lines = ["# Local development credentials. Never commit or expose in NEXT_PUBLIC variables.",
                 "SPACETIME_DISABLED=0", f"SPACETIME_URL={self.server}", f"SPACETIME_DB={self.database}"]
        for actor in SERVICES:
            lines.append(f"SPACETIME_TOKEN_{actor.upper()}={self.identity(actor)['token']}")
        save_private(self.directory / "services.env", "\n".join(lines) + "\n")
        print(f"Provisioned service identities. Source {self.directory / 'services.env'} before starting agents.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("SPACETIME_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--database", default=os.getenv("SPACETIME_DB", "freightops"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("publish", help="Build/publish without deleting data, then provision local services")
    commands.add_parser("provision", help="Grant service identities on an already published module")
    grant = commands.add_parser("grant-human", help="Grant the identity shown in the browser dashboard")
    grant.add_argument("identity")
    grant.add_argument("--name", required=True)
    code = commands.add_parser("issue-code", help="Issue a five-minute single-use approval code")
    code.add_argument("approval_id")
    args = parser.parse_args()
    admin = LocalAdmin(args.server, args.database)
    try:
        if args.command == "publish":
            admin.publish()
            admin.provision()
        elif args.command == "provision":
            admin.provision()
        elif args.command == "grant-human":
            admin.grant(args.identity, "human", f"human:{args.name.removeprefix('human:')}")
            print("Human role granted. The dashboard identity can now submit human actions.")
        elif args.command == "issue-code":
            value = f"{secrets.randbelow(1_000_000):06d}"
            admin.call("admin", "issue_approval_code", {
                "approval_id": args.approval_id, "code": value, "expires_in_seconds": 300,
            })
            print(f"Approval {args.approval_id}: {value} (five minutes; give only to the assigned approver)")
    finally:
        admin.client.close()


if __name__ == "__main__":
    main()
