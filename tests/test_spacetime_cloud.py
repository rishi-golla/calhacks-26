"""Cloud setup boundaries are tested without contacting Maincloud or invoking CLI mutations."""
from __future__ import annotations

import json
import stat
import subprocess

import httpx
import pytest

from common.policy import Decision
from scripts import spacetime_cloud as cloud


def sql_rows(rows):
    columns = list(rows[0]) if rows else []
    return [{"schema": {"elements": [{"name": {"some": column}} for column in columns]},
             "rows": [[row[column] for column in columns] for row in rows]}]


class FakeCloud:
    def __init__(self, monkeypatch, tmp_path):
        self.requests = []
        self.commands = []
        self.created = []
        self.tables = {"principal": [], "case": [], "accounting_period": [], "instruction_version": []}
        self.owner = cloud.PUBLISHER
        self.cli_identity = cloud.PUBLISHER
        self.verify_status = 204
        self.reducer_status = 200
        monkeypatch.setattr(cloud, "ROOT", tmp_path)
        monkeypatch.setattr(cloud.subprocess, "run", self.run_cli)
        self.transport = httpx.MockTransport(self.handle)

    def run_cli(self, command, **kwargs):
        self.commands.append(command)
        assert "--token" not in command
        assert kwargs["capture_output"] and kwargs["text"]
        if command == ["spacetime", "login", "show"]:
            return subprocess.CompletedProcess(command, 0, f"You are logged in as {self.cli_identity}\n", "")
        assert command[:2] == ["spacetime", "call"]
        assert command[command.index("--server") + 1] == cloud.SERVER
        assert command[-3] == cloud.DATABASE
        assert command[-2] in cloud.ADMIN_REDUCERS
        payload = json.loads(json.loads(command[-1]))
        if command[-2] == "grant_role":
            self.tables["principal"] = [row for row in self.tables["principal"]
                                        if row["identity"] != payload["identity"]]
            self.tables["principal"].append({key: payload[key] for key in ("identity", "actor", "role", "active")})
        return subprocess.CompletedProcess(command, 0, "", "")

    def handle(self, request):
        self.requests.append(request)
        assert str(request.url).startswith(cloud.SERVER + "/")
        if request.method == "GET" and request.url.path == f"/v1/database/{cloud.DATABASE}":
            return httpx.Response(200, json={"owner_identity": {"__identity__": f"0x{self.owner}"}})
        if request.method == "POST" and request.url.path == "/v1/identity":
            index = len(self.created) + 1
            record = {"identity": f"{index:064x}", "token": f"cloud.private-token-{index}"}
            self.created.append(record)
            return httpx.Response(200, json=record)
        if request.url.path.endswith("/verify"):
            assert request.headers["authorization"].startswith("Bearer cloud.")
            return httpx.Response(self.verify_status)
        if request.url.path.endswith("/sql"):
            table = request.content.decode().split('"')[1]
            return httpx.Response(200, json=sql_rows(self.tables[table]))
        if "/call/" in request.url.path:
            assert "authorization" in request.headers
            assert request.url.path.split("/")[-1] not in cloud.ADMIN_REDUCERS
            return httpx.Response(self.reducer_status, text="response may contain sensitive details")
        pytest.fail(f"Unexpected request: {request.method} {request.url.path}")

    def adapter(self):
        return cloud.CloudAdmin(transport=self.transport)


@pytest.mark.parametrize(("server", "database"), [
    ("http://maincloud.spacetimedb.com", cloud.DATABASE),
    ("https://maincloud.spacetimedb.com.evil.test", cloud.DATABASE),
    ("http://127.0.0.1:3000", cloud.DATABASE),
    (cloud.SERVER, "freightops"),
])
def test_adapter_is_bound_to_exact_https_host_and_database(server, database):
    with pytest.raises(ValueError, match="restricted"):
        cloud.CloudAdmin(server, database)


def test_wrong_cli_identity_prevents_all_network_and_mutations(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.cli_identity = "f" * 64
    with fake.adapter().client as client:
        assert not client.is_closed
    admin = fake.adapter()
    try:
        with pytest.raises(RuntimeError, match="CLI login"):
            admin.provision()
        assert not fake.requests
        assert all(command == ["spacetime", "login", "show"] for command in fake.commands)
    finally:
        admin.client.close()


def test_wrong_database_owner_prevents_identity_creation_and_grants(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.owner = "a" * 64
    admin = fake.adapter()
    try:
        with pytest.raises(RuntimeError, match="database owner"):
            admin.provision()
        assert [request.method for request in fake.requests] == ["GET"]
        assert not fake.created
        assert all(command[1] != "call" for command in fake.commands)
    finally:
        admin.client.close()


def test_provision_uses_fresh_cloud_tokens_private_files_and_cli_only_admin(monkeypatch, tmp_path, capsys):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        admin.provision()
        assert len(fake.created) == len(cloud.ACTORS)
        assert len([command for command in fake.commands if command[1] == "call"]) == len(cloud.ACTORS)
        stored = json.loads(admin.path.read_text())
        assert stored["server"] == cloud.SERVER
        assert stored["database"] == cloud.DATABASE
        assert stored["publisher"] == cloud.PUBLISHER
        assert "admin" not in stored["identities"]
        assert stat.S_IMODE(admin.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(admin.directory.stat().st_mode) == 0o700
        env = (admin.directory / "services.env").read_text()
        assert "SPACETIME_TOKEN_CARRIER_AP=" in env
        assert "SPACETIME_TOKEN_ADMIN" not in env
        assert "human:operator" not in env
        output = capsys.readouterr()
        assert "private-token" not in output.out + output.err
        assert not any("private-token" in " ".join(command) for command in fake.commands)
    finally:
        admin.client.close()


def test_repeat_provision_reuses_verified_cloud_tokens_and_stable_grant_ids(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    first = fake.adapter()
    first.provision()
    first.client.close()
    first_grants = [json.loads(json.loads(command[-1])) for command in fake.commands if command[1] == "call"]
    fake.commands.clear()
    second = fake.adapter()
    try:
        second.provision()
        second_grants = [json.loads(json.loads(command[-1])) for command in fake.commands if command[1] == "call"]
        assert len(fake.created) == len(cloud.ACTORS)
        assert first_grants == second_grants
    finally:
        second.client.close()


def test_foreign_or_admin_credentials_are_not_reused(monkeypatch, tmp_path):
    FakeCloud(monkeypatch, tmp_path)
    directory = tmp_path / "data/w2/maincloud" / cloud.DATABASE
    path = directory / "identities.json"
    for changes in [
        {"server": "http://127.0.0.1:3000"},
        {"identities": {"admin": {"identity": cloud.PUBLISHER, "token": "publisher.secret"}}},
        {"identities": {"carrier_ap": {"identity": cloud.PUBLISHER, "token": "publisher.secret"}}},
    ]:
        cloud.save_private(path, json.dumps({"server": cloud.SERVER, "database": cloud.DATABASE,
                                             "publisher": cloud.PUBLISHER, "identities": {}, **changes}))
        with pytest.raises(ValueError):
            cloud.CloudAdmin()


def test_existing_live_actor_assignment_is_preserved(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.tables["principal"] = [{"identity": "a" * 64, "actor": "carrier_ap", "role": "carrier_ap", "active": True}]
    admin = fake.adapter()
    try:
        with pytest.raises(RuntimeError, match="existing grant preserved"):
            admin.provision()
        assert not fake.created
        assert not any(command[1] == "call" for command in fake.commands)
    finally:
        admin.client.close()


def test_invalid_cloud_service_token_cannot_be_granted(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.verify_status = 401
    admin = fake.adapter()
    try:
        with pytest.raises(RuntimeError, match="not valid on Maincloud"):
            admin.provision()
        assert not admin.path.exists()
        assert not any(command[1] == "call" for command in fake.commands)
    finally:
        admin.client.close()


def test_policy_denial_precedes_network_and_cli_mutation(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud, "check", lambda *args: Decision("deny", reason="fixture policy denial"))
    admin = fake.adapter()
    try:
        with pytest.raises(ValueError, match="Policy deny"):
            admin.call("ingest", "open_case", {"case_id": "CASE-W2-1"})
        assert not fake.requests
        assert not fake.commands
    finally:
        admin.client.close()


def test_service_call_uses_one_json_argument_and_only_its_cloud_token(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        payload = {"write_id": "cloud-fixture-test", "case_id": "CASE-W2-1"}
        admin.call("ingest", "open_case", payload)
        request = next(request for request in fake.requests if "/call/" in request.url.path)
        assert json.loads(json.loads(request.content)[0]) == payload
        assert request.headers["authorization"] == "Bearer cloud.private-token-1"
        assert all(command[1] != "call" for command in fake.commands)
        fake.reducer_status = 400
        with pytest.raises(RuntimeError) as error:
            admin.call("ingest", "open_case", payload)
        assert "sensitive details" not in str(error.value)
    finally:
        admin.client.close()


def test_admin_adapter_cannot_escalate_roles_or_extract_publisher_credentials(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        with pytest.raises(ValueError):
            admin.identity("admin")
        for public, role, actor in [("a" * 64, "admin", "admin:other"),
                                    (cloud.PUBLISHER, "human", "human:publisher"),
                                    ("a" * 64, "controller", "risk")]:
            with pytest.raises(ValueError):
                admin.grant(public, role, actor)
        with pytest.raises(ValueError):
            admin.call("admin", "post_journal_entry", {"id": "J-1"})
        assert all(command[1] != "call" for command in fake.commands)
    finally:
        admin.client.close()


@pytest.mark.parametrize(("table", "rows", "message"), [
    ("case", [{"case_id": "CASE-LIVE-1"}], "non-fixture"),
    ("accounting_period", [{"period": "2026-10", "status": "locked"}], "locked"),
    ("instruction_version", [{"agent": "carrier_ap", "active": True, "source_ref": "REVIEW-LIVE"}], "live instruction"),
])
def test_demo_preserves_live_settings(monkeypatch, tmp_path, table, rows, message):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        admin.provision()
        fake.tables[table] = rows
        monkeypatch.setattr(cloud, "seed", lambda _admin: pytest.fail("seed must not run"))
        with pytest.raises(RuntimeError, match=message):
            admin.demo()
    finally:
        admin.client.close()


def test_demo_reuses_existing_seed_only_after_provisioning(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    called = []
    monkeypatch.setattr(cloud, "seed", lambda adapter: called.append(adapter))
    try:
        with pytest.raises(RuntimeError, match="Provision"):
            admin.demo()
        admin.provision()
        admin.demo()
        assert called == [admin]
    finally:
        admin.client.close()
