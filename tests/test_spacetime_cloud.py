"""Cloud setup boundaries are tested without contacting Maincloud or invoking CLI mutations."""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
from datetime import date, datetime

import httpx
import pytest

from common.config import AGENTS
from common.policy import Decision
from scripts import spacetime_cloud as cloud
from scripts.local_env import read_env
from scripts.spacetime_admin import SERVICES


def sql_rows(rows):
    columns = list(rows[0]) if rows else []
    return [{"schema": {"elements": [{"name": {"some": column}} for column in columns]},
             "rows": [[row[column] for column in columns] for row in rows]}]


class FakeCloud:
    def __init__(self, monkeypatch, tmp_path):
        self.requests = []
        self.commands = []
        self.created = []
        self.tables = {"principal": [], "case": [], "accounting_period": [], "instruction_version": [],
                       # The real cloud predates `documents`; ensure_agent_defaults adds it.
                       "agent_config": [{"agent": name, "model": "auto", "enabled": True}
                                        for name in AGENTS if name != "documents"]}
        self.fail_defaults = False
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
        elif command[-2] == "ensure_agent_defaults":
            if self.fail_defaults:
                return subprocess.CompletedProcess(command, 1, "", "unknown reducer")
            have = {row["agent"] for row in self.tables["agent_config"]}
            self.tables["agent_config"] += [{"agent": name, "model": "auto", "enabled": True}
                                            for name in AGENTS if name not in have]
        elif command[-2] == "settle_cases":
            self.tables["case"] = [{**row, "status": "resolved"} if row["status"] == "open" else row
                                   for row in self.tables["case"]]
        elif command[-2] == "set_period":
            self.tables["accounting_period"] = [row for row in self.tables["accounting_period"]
                                                if row["period"] != payload["period"]]
            self.tables["accounting_period"].append({"period": payload["period"], "status": payload["status"]})
        return subprocess.CompletedProcess(command, 0, "", "")

    def reducer_commands(self, reducer):
        return [command for command in self.commands if command[1] == "call" and command[-2] == reducer]

    def reducer_payloads(self, reducer):
        return [json.loads(json.loads(command[-1])) for command in self.reducer_commands(reducer)]

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
        assert len(fake.reducer_commands("grant_role")) == len(cloud.ACTORS)
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
    first_grants = fake.reducer_payloads("grant_role")
    fake.commands.clear()
    second = fake.adapter()
    try:
        second.provision()
        second_grants = fake.reducer_payloads("grant_role")
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


# --- agent defaults, accounting periods and per-service handoff files ---------------------------

class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 24, 9, 30, tzinfo=tz)


def provisioned(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud, "datetime", FixedDatetime)
    admin = fake.adapter()
    admin.provision()
    return fake, admin


def test_provision_ensures_agent_defaults_once_after_every_grant(monkeypatch, tmp_path, capsys):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        admin.provision()
        reducers = [command[-2] for command in fake.commands if command[1] == "call"]
        assert reducers.count("ensure_agent_defaults") == 1
        assert reducers.index("ensure_agent_defaults") > max(
            index for index, name in enumerate(reducers) if name == "grant_role")
        (payload,) = fake.reducer_payloads("ensure_agent_defaults")
        assert set(payload) == {"write_id"}
        assert {row["agent"] for row in fake.tables["agent_config"]} == set(AGENTS)
        assert "documents" in capsys.readouterr().out
    finally:
        admin.client.close()


def test_repeat_provision_uses_a_fresh_agent_defaults_write_id(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    first = fake.adapter()
    first.provision()
    first.client.close()
    second = fake.adapter()
    try:
        second.provision()
        first_call, second_call = fake.reducer_payloads("ensure_agent_defaults")
        assert first_call["write_id"] != second_call["write_id"]
    finally:
        second.client.close()


def test_ensure_agent_defaults_reports_only_the_missing_agents(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        assert cloud.ensure_agent_defaults(admin) == ["documents"]
        assert cloud.ensure_agent_defaults(admin) == []
    finally:
        admin.client.close()


def test_agent_defaults_failure_asks_for_republish_after_files_are_written(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.fail_defaults = True
    admin = fake.adapter()
    try:
        with pytest.raises(RuntimeError, match="republish"):
            admin.provision()
        assert (admin.directory / "services.env").exists()
        assert {path.name for path in (admin.directory / "handoff").iterdir()} == {
            f"{actor}.env" for actor in SERVICES}
        assert not fake.reducer_commands("set_period")
    finally:
        admin.client.close()


def test_accounting_periods_span_current_month_plus_eighteen():
    window = cloud.accounting_periods(date(2026, 10, 24))
    assert len(window) == 19
    assert (window[0], window[-1]) == ("2026-10", "2028-04")
    assert cloud.accounting_periods(date(2026, 12, 5))[::18] == ["2026-12", "2028-06"]
    assert cloud.accounting_periods(date(2026, 12, 5))[1] == "2027-01"


def test_open_accounting_periods_opens_the_whole_window_with_stable_write_ids(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    admin = fake.adapter()
    try:
        opened, locked = cloud.open_accounting_periods(admin, today=date(2026, 10, 24))
        assert (opened[0], opened[-1], len(opened), locked) == ("2026-10", "2028-04", 19, [])
        payloads = fake.reducer_payloads("set_period")
        assert [payload["write_id"] for payload in payloads] == [f"cloud-period:{p}:open" for p in opened]
        assert {payload["status"] for payload in payloads} == {"open"}
        assert [payload["period"] for payload in payloads] == opened
    finally:
        admin.client.close()


def test_existing_open_and_locked_periods_are_never_called_again(monkeypatch, tmp_path):
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.tables["accounting_period"] = [{"period": "2026-10", "status": "open"},
                                        {"period": "2026-12", "status": "locked"}]
    admin = fake.adapter()
    try:
        opened, locked = cloud.open_accounting_periods(admin, today=date(2026, 10, 24))
        called = {payload["period"] for payload in fake.reducer_payloads("set_period")}
        assert "2026-10" not in called
        assert "2026-12" not in called
        assert "2026-10" not in opened
        assert "2026-12" not in opened
        assert len(called) == 17
        assert locked == ["2026-12"]
        assert {row["period"]: row["status"] for row in fake.tables["accounting_period"]}["2026-12"] == "locked"
    finally:
        admin.client.close()


def test_provision_reports_locked_periods_and_second_run_opens_nothing(monkeypatch, tmp_path, capsys):
    fake = FakeCloud(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud, "datetime", FixedDatetime)
    fake.tables["accounting_period"] = [{"period": "2026-11", "status": "locked"}]
    first = fake.adapter()
    first.provision()
    first.client.close()
    assert "Left locked (never reopened by provisioning): 2026-11" in capsys.readouterr().out
    assert len(fake.reducer_commands("set_period")) == 18
    fake.commands.clear()
    second = fake.adapter()
    try:
        second.provision()
        assert not fake.reducer_commands("set_period")
        assert "none missing" in capsys.readouterr().out
    finally:
        second.client.close()


def test_ensure_agent_defaults_is_admin_only_and_policy_checked_first(monkeypatch, tmp_path):
    assert "ensure_agent_defaults" in cloud.ADMIN_REDUCERS
    fake = FakeCloud(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud, "check", lambda *args: Decision("deny", reason="fixture policy denial"))
    admin = fake.adapter()
    try:
        with pytest.raises(ValueError, match="Policy deny"):
            admin.call("admin", "ensure_agent_defaults", {"write_id": "x"})
        assert not fake.requests
        assert not fake.commands
    finally:
        admin.client.close()


def test_handoff_files_are_private_and_one_per_service(monkeypatch, tmp_path):
    _, admin = provisioned(monkeypatch, tmp_path)
    try:
        directory = admin.directory / "handoff"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert sorted(path.name for path in directory.iterdir()) == sorted(f"{actor}.env" for actor in SERVICES)
        assert len(SERVICES) == 13
        for path in directory.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        admin.client.close()


def test_handoff_files_hold_only_their_own_token_and_machine_switches(monkeypatch, tmp_path):
    _, admin = provisioned(monkeypatch, tmp_path)
    try:
        stored = json.loads(admin.path.read_text())["identities"]
        files = {actor: read_env(admin.directory / "handoff" / f"{actor}.env") for actor in SERVICES}
        for actor, values in files.items():
            assert values["SPACETIME_TIMEOUT_SECONDS"] == "15"
            assert values["SPACETIME_DISABLED"] == "0"
            assert values["SPACETIME_URL"] == cloud.SERVER
            assert values["SPACETIME_DB"] == cloud.DATABASE
            assert values["SPACETIME_ACTOR"] == actor
            tokens = {key: value for key, value in values.items() if key.startswith("SPACETIME_TOKEN_")}
            assert tokens == {f"SPACETIME_TOKEN_{actor.upper()}": stored[actor]["token"]}
            assert "SOURCE_DATA_PATH" not in values
            assert (values.get("SOURCE_DATA_BACKEND") == "spacetime") == (actor in AGENTS or actor == "router")
            assert (values.get("FIRST_HOP_TRANSPORT") == "spacetime") == (actor == "router")
            assert (values.get("DISPATCH_POLL_SECONDS") == "1") == (actor == "router")
        assert "voice_desk" in AGENTS
        assert "validator" in AGENTS
        for actor in ("ingest", "harness", "evaluator"):
            assert "SOURCE_DATA_BACKEND" not in files[actor]
    finally:
        admin.client.close()


def test_services_env_gains_timeout_and_still_omits_admin_and_human(monkeypatch, tmp_path):
    _, admin = provisioned(monkeypatch, tmp_path)
    try:
        values = read_env(admin.directory / "services.env")
        assert values["SPACETIME_TIMEOUT_SECONDS"] == "15"
        assert not any("ADMIN" in key for key in values)
        assert "human:operator" not in (admin.directory / "services.env").read_text()
        assert {key for key in values if key.startswith("SPACETIME_TOKEN_")} == {
            f"SPACETIME_TOKEN_{actor.upper()}" for actor in SERVICES}
        # The Mac's live launchers load only this combined file, so it carries every switch.
        assert values["SOURCE_DATA_BACKEND"] == "spacetime"
        assert values["FIRST_HOP_TRANSPORT"] == "spacetime"
        assert values["DISPATCH_POLL_SECONDS"] == "1"
        assert "SOURCE_DATA_PATH" not in values
        assert "SPACETIME_ACTOR" not in values
    finally:
        admin.client.close()


def test_handoff_command_rewrites_files_without_network_or_cli(monkeypatch, tmp_path):
    fake, first = provisioned(monkeypatch, tmp_path)
    first.client.close()
    shutil.rmtree(first.directory / "handoff")
    (first.directory / "services.env").unlink()
    fake.requests.clear()
    fake.commands.clear()
    admin = fake.adapter()
    try:
        admin.write_env_files()
        assert not fake.requests
        assert not fake.commands
        assert len(list((admin.directory / "handoff").iterdir())) == len(SERVICES)
        assert (admin.directory / "services.env").exists()
    finally:
        admin.client.close()


def test_handoff_without_stored_identities_names_the_missing_actors(monkeypatch, tmp_path):
    fake, first = provisioned(monkeypatch, tmp_path)
    first.client.close()
    stored = json.loads(first.path.read_text())
    del stored["identities"]["router"], stored["identities"]["ingest"]
    cloud.save_private(first.path, json.dumps(stored))
    admin = fake.adapter()
    try:
        with pytest.raises(RuntimeError, match="missing: .*ingest.*router"):
            admin.write_env_files()
    finally:
        admin.client.close()


def test_settle_cases_is_a_cli_only_admin_call_with_a_fresh_write_id(monkeypatch, tmp_path):
    assert "settle_cases" in cloud.ADMIN_REDUCERS
    fake = FakeCloud(monkeypatch, tmp_path)
    fake.tables["case"] = [{"case_id": "CASE-X", "status": "open"}, {"case_id": "CASE-H", "status": "human_owned"}]
    admin = fake.adapter()
    try:
        assert cloud.settle_cases(admin) == ({"human_owned": 1, "open": 1}, {"human_owned": 1, "resolved": 1})
        cloud.settle_cases(admin)
        first, second = fake.reducer_payloads("settle_cases")
        assert set(first) == {"write_id"} and first["write_id"] != second["write_id"]
        assert not [request for request in fake.requests if "/call/" in request.url.path]
    finally:
        admin.client.close()
