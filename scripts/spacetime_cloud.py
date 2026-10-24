"""Provision and seed the explicitly authorized at350 FreightLine cloud database.

This is deliberately separate from the loopback-only LocalAdmin helper. It never
publishes, deletes, clears data, changes the CLI login, or reads the publisher's
token. Admin reducers use the existing verified CLI session. Service credentials
come only from Maincloud and stay in an ignored owner-only directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx

from common.policy import check
from scripts.spacetime_admin import SERVICES
from scripts.spacetime_demo import seed

ROOT = Path(__file__).resolve().parents[1]
SERVER = "https://maincloud.spacetimedb.com"
DATABASE = "at350-freightops"
PUBLISHER = "c2005175965c7ab35b8367adc2064a7bc96cacb0b0b796ddf81377fc02b50f1f"
FIXTURE_HUMAN = "human:operator"
ACTORS = (*SERVICES, FIXTURE_HUMAN)
ADMIN_REDUCERS = {"grant_role", "set_period"}


def identity_hex(value: object) -> str:
    """Metadata uses SATS identity objects; the identity endpoint uses strings."""
    if isinstance(value, dict):
        value = value.get("__identity__")
    if not isinstance(value, str):
        raise TypeError("Expected a Spacetime identity")
    result = value.lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ValueError("Identity must contain 64 hexadecimal digits")
    return result


def save_private(path: Path, content: str) -> None:
    """Atomically replace private state without following a destination symlink."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Cloud credential paths must not be symbolic links")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = output.name
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class CloudAdmin:
    """A narrowly scoped adapter compatible with the existing labeled seed."""

    def __init__(self, server: str = SERVER, database: str = DATABASE, *,
                 transport: httpx.BaseTransport | None = None):
        if server != SERVER or database != DATABASE:
            raise ValueError(f"This helper is restricted to {SERVER}/{DATABASE}")
        self.server = SERVER
        self.database = DATABASE
        self.directory = ROOT / "data" / "w2" / "maincloud" / DATABASE
        self.path = self.directory / "identities.json"
        self.identities: dict[str, dict[str, str]] = {}
        self._verified_actors: set[str] = set()
        self._target_verified = False
        if self.path.is_symlink() or self.directory.is_symlink():
            raise ValueError("Cloud credential paths must not be symbolic links")
        if self.path.exists():
            self.path.chmod(0o600)
            self.directory.chmod(0o700)
            stored = json.loads(self.path.read_text())
            if (stored.get("server"), stored.get("database"), stored.get("publisher")) != (
                SERVER, DATABASE, PUBLISHER,
            ):
                raise ValueError("Stored credentials belong to a different server/database/publisher")
            entries = stored.get("identities")
            if not isinstance(entries, dict) or any(actor not in ACTORS for actor in entries):
                raise ValueError("Credential file contains an unsupported actor or administrator")
            for actor, entry in entries.items():
                self.identities[actor] = self._validate_identity(entry)
            if len({entry["identity"] for entry in self.identities.values()}) != len(self.identities):
                raise ValueError("Each cloud service must have its own identity")
        self.client = httpx.Client(base_url=SERVER, timeout=60, follow_redirects=False, transport=transport)

    @staticmethod
    def _validate_identity(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            raise TypeError("Malformed service credential record")
        public = identity_hex(value.get("identity"))
        token = value.get("token")
        if public == PUBLISHER:
            raise ValueError("Publisher credentials must never be stored with service credentials")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,16384}", token):
            raise ValueError("Malformed service token")
        return {"identity": public, "token": token}

    @staticmethod
    def _policy(reducer: str, actor: str, payload: dict) -> None:
        decision = check(reducer, actor, payload)
        if decision.result != "allow":
            raise ValueError(f"Policy {decision.result}: {decision.reason}")

    @staticmethod
    def _verify_cli_publisher() -> None:
        # No --token, config-file reads, login changes, or credential arguments.
        result = subprocess.run(
            ["spacetime", "login", "show"], capture_output=True, text=True, check=False, timeout=30,
        )
        match = re.search(r"You are logged in as ([0-9a-fA-F]{64})\b", result.stdout)
        if result.returncode or not match or match.group(1).lower() != PUBLISHER:
            raise RuntimeError("CLI login does not match the authorized at350 publisher; no mutation was sent")

    def verify_target(self) -> None:
        if self._target_verified:
            return
        self._verify_cli_publisher()
        response = self.client.get(f"/v1/database/{DATABASE}")
        if not response.is_success:
            raise RuntimeError(f"Cannot inspect the authorized cloud database (HTTP {response.status_code})")
        if identity_hex(response.json().get("owner_identity")) != PUBLISHER:
            raise RuntimeError("Cloud database owner differs from the authorized publisher; no mutation was sent")
        self._target_verified = True

    def _save_identities(self) -> None:
        save_private(self.path, json.dumps({
            "server": SERVER, "database": DATABASE, "publisher": PUBLISHER,
            "identities": self.identities,
        }, indent=2) + "\n")

    def identity(self, actor: str) -> dict[str, str]:
        if actor not in ACTORS:
            raise ValueError("Only the named W2 service identities and fixture human may be provisioned")
        self.verify_target()
        created = actor not in self.identities
        if created:
            self._policy("provision_identity", "admin", {"actor": actor, "server": SERVER, "database": DATABASE})
            response = self.client.post("/v1/identity")
            if not response.is_success:
                raise RuntimeError(f"Cloud identity creation failed (HTTP {response.status_code}); token omitted")
            entry = self._validate_identity(response.json())
            if any(other["identity"] == entry["identity"] for other in self.identities.values()):
                raise RuntimeError("Maincloud returned an identity already assigned to another service")
        else:
            entry = self.identities[actor]
        if actor not in self._verified_actors:
            verified = self.client.get(
                f"/v1/identity/{entry['identity']}/verify",
                headers={"Authorization": f"Bearer {entry['token']}"},
            )
            if verified.status_code != 204:
                raise RuntimeError(f"Stored identity for {actor} is not valid on Maincloud; credentials omitted")
            self._verified_actors.add(actor)
        if created:
            self.identities[actor] = entry
            self._save_identities()
        return entry

    def call(self, actor: str, reducer: str, payload: dict) -> httpx.Response | None:
        self._policy(reducer, actor, payload)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", reducer):
            raise ValueError("Invalid reducer name")
        self.verify_target()
        body = {"write_id": str(uuid.uuid4()), **payload}
        if actor == "admin":
            if reducer not in ADMIN_REDUCERS:
                raise ValueError("This cloud adapter permits only service grants and fixture periods as admin")
            if reducer == "grant_role":
                self._validate_grant(body)
            # Recheck before every privileged CLI call in case the user switched
            # login sessions while a longer seed operation was running.
            self._verify_cli_publisher()
            result = subprocess.run([
                "spacetime", "call", "--no-config", "--server", SERVER, "--yes",
                DATABASE, reducer, json.dumps(json.dumps(body, separators=(",", ":"))),
            ], capture_output=True, text=True, check=False, timeout=60)
            if result.returncode:
                raise RuntimeError(f"Cloud admin reducer {reducer} failed; CLI output withheld")
            return None
        token = self.identity(actor)["token"]
        response = self.client.post(
            f"/v1/database/{DATABASE}/call/{reducer}",
            headers={"Authorization": f"Bearer {token}"},
            json=[json.dumps(body, separators=(",", ":"))],
        )
        if not response.is_success:
            raise RuntimeError(f"Cloud reducer {reducer} rejected (HTTP {response.status_code}); response omitted")
        return response

    @staticmethod
    def _validate_grant(payload: dict) -> None:
        public = identity_hex(payload.get("identity"))
        role, actor = payload.get("role"), payload.get("actor")
        if public == PUBLISHER or payload.get("active") is not True:
            raise ValueError("Cloud setup may not modify the publisher or revoke existing principals")
        if role in SERVICES and actor == role:
            return
        if role == "human" and isinstance(actor, str) and re.fullmatch(r"human:[A-Za-z0-9_.-]{1,80}", actor):
            return
        raise ValueError("Only named service roles or a named human can be granted")

    def grant(self, public: str, role: str, actor: str) -> None:
        public = identity_hex(public)
        payload = {"identity": public, "role": role, "actor": actor, "active": True}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]
        self.call("admin", "grant_role", {"write_id": f"cloud-grant:{digest}", **payload})

    def rows(self, table: str) -> list[dict]:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", table):
            raise ValueError("Invalid table")
        self.verify_target()
        response = self.client.post(f"/v1/database/{DATABASE}/sql", content=f'SELECT * FROM "{table}"')
        if not response.is_success:
            raise RuntimeError(f"Cloud table read failed (HTTP {response.status_code})")
        result = response.json()[0]
        names = [element["name"].get("some") if isinstance(element["name"], dict) else element["name"]
                 for element in result["schema"]["elements"]]
        return [dict(zip(names, row, strict=True)) for row in result["rows"]]

    def provision(self) -> None:
        self.verify_target()
        principals = {row["actor"]: row for row in self.rows("principal") if row["active"]}
        # Lost tokens cannot be recovered. Do not create replacements for live
        # actors or silently change their role assignment.
        for actor in ACTORS:
            existing = principals.get(actor)
            if existing and existing["identity"] != self.identities.get(actor, {}).get("identity"):
                raise RuntimeError(f"Cloud actor {actor} already has another active identity; existing grant preserved")
        for actor in ACTORS:
            self.grant(self.identity(actor)["identity"], "human" if actor == FIXTURE_HUMAN else actor, actor)
        lines = ["# Maincloud service credentials. Never commit or expose through NEXT_PUBLIC settings.",
                 "SPACETIME_DISABLED=0", f"SPACETIME_URL={SERVER}", f"SPACETIME_DB={DATABASE}"]
        for actor in SERVICES:
            lines.append(f"SPACETIME_TOKEN_{actor.upper()}={self.identity(actor)['token']}")
        save_private(self.directory / "services.env", "\n".join(lines) + "\n")
        print(f"Provisioned cloud service identities for {DATABASE}; private settings: {self.directory / 'services.env'}")

    def demo(self) -> None:
        self.verify_target()
        if any(actor not in self.identities for actor in ACTORS):
            raise RuntimeError("Provision cloud service identities before seeding the W2 fixtures")
        if any(not str(row["case_id"]).startswith("CASE-W2-") for row in self.rows("case")):
            raise RuntimeError("Cloud database contains non-fixture cases; demo seed will not change live agent settings")
        if any(row["period"] == "2026-10" and row["status"] != "open" for row in self.rows("accounting_period")):
            raise RuntimeError("October is locked; demo seed will not reopen an existing accounting period")
        if any(row["agent"] == "carrier_ap" and row["active"] and row["source_ref"] != "REVIEW-W2-AP"
               for row in self.rows("instruction_version")):
            raise RuntimeError("Carrier AP has live instruction updates; demo seed will not supersede them")
        seed(self)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision", help="Create Maincloud-only service identities and grant their roles")
    commands.add_parser("demo", help="Seed labeled W2 fixtures in the authorized cloud database")
    grant = commands.add_parser("grant-human", help="Grant the identity displayed by the cloud-connected dashboard")
    grant.add_argument("identity")
    grant.add_argument("--name", required=True)
    args = parser.parse_args()
    admin = CloudAdmin()
    try:
        if args.command == "provision":
            admin.provision()
        elif args.command == "demo":
            admin.demo()
        else:
            admin.grant(args.identity, "human", f"human:{args.name.removeprefix('human:')}")
            print(f"Human role granted on {DATABASE}; the publisher credential was not shared.")
    finally:
        admin.client.close()


if __name__ == "__main__":
    main()
