"""SOURCE_DATA_BACKEND=spacetime: source records read over HTTP SQL, mapped like the file dataset."""
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from common import source_repository
from common.envelope import TaskEnvelope
from common.server import SERVICE_ACTORS, _readiness
from common.source_repository import SourceRepository, SourceUnavailable
from common.spacetime_client import SpacetimeClient, SpacetimeError

CASE = {"load_id": "L-1", "synthetic": True, "carrier": {"id": "C-1"}, "shipper": {"id": "S-1"},
        "invoice_amount": 940.5}
COMPANY = {"id": "C-1", "name": "Carrier", "verified": True, "source": "fmcsa", "phone": "+15555550100"}
CONTACTS = [{"id": "CT-0", "company_id": "C-1", "verified": False, "source": "web"},
            {"id": "CT-1", "company_id": "C-1", "verified": True, "source": "fmcsa", "phone": "+15555550101"}]
META = {"schema": 1, "generated_at": "2026-10-24T00:00:00Z", "source_kind": "w4_ingest_export"}
COLUMNS = ["id", "kind", "case_id", "record_json", "digest", "updated_at"]


def row(record_id, value, *, raw=None):
    kind = record_id.split(":", 1)[0]
    case_id = record_id.split(":", 1)[1] if kind == "case" else ""
    return [record_id, kind, case_id, raw if raw is not None else json.dumps(value), "digest", "2026-10-24T00:00:00Z"]


def sql_response(rows):
    return httpx.Response(200, json=[{"schema": {"elements": [{"name": {"some": name}} for name in COLUMNS]},
                                      "rows": rows}])


class Database:
    """A source_record table behind httpx.MockTransport that records every SQL statement."""

    def __init__(self, **records):
        self.records = dict(records)
        self.queries: list[str] = []
        self.failure = None

    def handler(self, request):
        assert request.url.path.endswith("/sql")
        self.queries.append(request.content.decode())
        if self.failure is not None:
            return self.failure(request)
        return self.respond(request)

    def respond(self, request):
        sql = request.content.decode()
        prefix = "SELECT * FROM source_record WHERE id = '"
        assert sql.startswith(prefix) and sql.endswith("'")
        record_id = sql[len(prefix):-1].replace("''", "'")
        return sql_response([row(record_id, self.records[record_id])] if record_id in self.records else [])

    def client(self, *, database="freightops-test", **kwargs):
        return SpacetimeClient(disabled=False, url="http://spacetime.test", database=database,
                               transport=httpx.MockTransport(self.handler), **kwargs)

    def reads(self, record_id):
        return self.queries.count(f"SELECT * FROM source_record WHERE id = '{record_id}'")


def default_records():
    return {"case:CASE-1": CASE, "company:C-1": COMPANY, "contacts:C-1": CONTACTS, "dataset:meta": META}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("SPACETIME_", "SOURCE_DATA_", "A2A_TOKEN_", "AGENT_PROVIDER_", "AGENT_MODEL_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SOURCE_DATA_BACKEND", "spacetime")
    clock = [1000.0]
    monkeypatch.setattr(source_repository, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    source_repository.clear_source_cache()
    yield clock
    source_repository.clear_source_cache()


@pytest.fixture
def db(monkeypatch):
    """Route argument-free SourceRepository() construction to the mock table, as callers do."""
    database = Database(**default_records())
    actors = []

    def factory(*, actor=None):
        actors.append(actor)
        return database.client(actor=actor)

    monkeypatch.setattr(source_repository, "SpacetimeClient", factory)
    database.actors = actors
    return database


def env(case_id="CASE-1", **subject):
    return TaskEnvelope(task_type="process_carrier_invoice", case_id=case_id, subject=subject)


# Backend selection


@pytest.mark.parametrize("backend", [None, "file", " FILE "])
async def test_unset_or_file_backend_reads_source_data_path_without_spacetime(monkeypatch, tmp_path, backend):
    def refuse(**_):
        raise AssertionError("file backend must not construct a SpacetimeDB client")

    monkeypatch.setattr(source_repository, "SpacetimeClient", refuse)
    if backend is None:
        monkeypatch.delenv("SOURCE_DATA_BACKEND")
    else:
        monkeypatch.setenv("SOURCE_DATA_BACKEND", backend)
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"cases": {"CASE-1": {"load_id": "FROM-FILE"}}, "companies": {"C-1": COMPANY}}))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    assert (await SourceRepository().load(env()))["load_id"] == "FROM-FILE"
    assert (await SourceRepository().get_company("C-1"))["name"] == "Carrier"
    monkeypatch.delenv("SOURCE_DATA_PATH")
    with pytest.raises(SourceUnavailable, match="live execution requires SOURCE_DATA_PATH"):
        await SourceRepository().load(env())


async def test_fixture_mode_without_backend_still_falls_back_to_subject(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.delenv("SOURCE_DATA_BACKEND")
    loaded = await SourceRepository().load(env(load_id="FROM-SUBJECT"))
    assert loaded["load_id"] == "FROM-SUBJECT" and loaded["simulated"] is True


async def test_spacetime_backend_ignores_source_data_path(db, monkeypatch, tmp_path):
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"cases": {"CASE-1": {"load_id": "FROM-FILE"}}}))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    assert (await SourceRepository().load(env()))["load_id"] == "L-1"
    assert db.reads("case:CASE-1") == 1


async def test_injected_data_wins_over_the_spacetime_backend(db):
    loaded = await SourceRepository({"cases": {"CASE-1": {"load_id": "INJECTED"}}}).load(env())
    assert loaded["load_id"] == "INJECTED"
    assert db.queries == [] and db.actors == []


async def test_unknown_backend_is_refused(monkeypatch, db):
    monkeypatch.setenv("SOURCE_DATA_BACKEND", "spacetimedb")
    with pytest.raises(SourceUnavailable, match="SOURCE_DATA_BACKEND must be file or spacetime"):
        await SourceRepository().load(env())
    assert db.queries == []


async def test_actor_is_passed_to_the_client_as_policy_context(db):
    await SourceRepository().load(env())
    await SourceRepository(actor="carrier_ap").get_company("C-1")
    assert db.actors == [None, "carrier_ap"]


# Read paths


async def test_load_reads_the_case_record_and_applies_file_post_processing(db):
    loaded = await SourceRepository().load(env())
    assert db.queries == ["SELECT * FROM source_record WHERE id = 'case:CASE-1'",
                          "SELECT * FROM source_record WHERE id = 'dataset:meta'"]
    assert loaded["case_id"] == "CASE-1"
    assert loaded["carrier_id"] == "C-1" and loaded["shipper_id"] == "S-1"
    assert loaded["invoice_amount"] == 940.5
    assert loaded["simulated"] is True and loaded["source_kind"] == "synthetic"


async def test_load_matches_the_file_backend_result_exactly(db, monkeypatch, tmp_path):
    from_spacetime = await SourceRepository().load(env())
    path = tmp_path / "source.json"
    path.write_text(json.dumps({**META, "cases": {"CASE-1": CASE}}))
    monkeypatch.setenv("SOURCE_DATA_BACKEND", "file")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    assert await SourceRepository().load(env()) == from_spacetime


async def test_load_rejects_a_record_for_another_case(db):
    db.records["case:CASE-1"] = {**CASE, "case_id": "CASE-2"}
    with pytest.raises(SourceUnavailable, match="source case does not match the requested case"):
        await SourceRepository().load(env())


async def test_load_rejects_a_non_object_case_record(db):
    db.records["case:CASE-1"] = ["not", "an", "object"]
    with pytest.raises(SourceUnavailable, match="case source must be an object"):
        await SourceRepository().load(env())


async def test_case_ids_are_escaped_as_sql_strings(db):
    db.records["case:O'BRIEN-1"] = {"load_id": "QUOTED"}
    assert (await SourceRepository().load(env("O'BRIEN-1")))["load_id"] == "QUOTED"
    assert db.queries[0] == "SELECT * FROM source_record WHERE id = 'case:O''BRIEN-1'"


async def test_dataset_meta_supplies_dataset_level_provenance(db):
    db.records["case:CASE-1"] = {"load_id": "L-1"}
    assert (await SourceRepository().load(env()))["source_kind"] == "configured_source"
    source_repository.clear_source_cache()
    db.records["dataset:meta"] = {**META, "synthetic": True}
    assert (await SourceRepository().load(env()))["source_kind"] == "synthetic"


async def test_missing_dataset_meta_leaves_record_level_provenance(db):
    del db.records["dataset:meta"]
    assert (await SourceRepository().load(env()))["simulated"] is True
    db.records["case:CASE-2"] = {"load_id": "L-2"}
    assert (await SourceRepository().load(env("CASE-2")))["source_kind"] == "configured_source"


async def test_dataset_meta_cannot_inject_other_records(db):
    db.records["dataset:meta"] = {**META, "cases": {"CASE-9": CASE},
                                  "companies": {"C-1": {**COMPANY, "phone": "+19999999999"}}}
    with pytest.raises(SourceUnavailable, match="case is absent"):
        await SourceRepository().load(env("CASE-9"))
    assert (await SourceRepository().get_company("C-1"))["phone"] == "+15555550100"


async def test_non_object_dataset_meta_is_refused(db):
    db.records["dataset:meta"] = ["meta"]
    with pytest.raises(SourceUnavailable, match="source dataset must be a JSON object"):
        await SourceRepository().load(env())


async def test_get_company_reads_the_company_record(db):
    company = await SourceRepository().get_company("C-1")
    assert db.queries[0] == "SELECT * FROM source_record WHERE id = 'company:C-1'"
    assert company["id"] == "C-1" and company["phone"] == "+15555550100"
    assert company["source_kind"] == "configured_source"


async def test_get_company_refuses_missing_or_mismatched_records(db):
    with pytest.raises(SourceUnavailable, match="counterparty is absent from the verified source directory"):
        await SourceRepository().get_company("C-404")
    db.records["company:C-2"] = {**COMPANY, "id": "C-1"}
    with pytest.raises(SourceUnavailable, match="counterparty is absent from the verified source directory"):
        await SourceRepository().get_company("C-2")


async def test_get_contact_reads_the_contact_list_and_picks_the_verified_contact(db):
    contact = await SourceRepository().get_contact("C-1")
    assert db.queries[0] == "SELECT * FROM source_record WHERE id = 'contacts:C-1'"
    assert contact["id"] == "CT-1" and contact["company_id"] == "C-1"
    assert db.reads("company:C-1") == 0


async def test_get_contact_falls_back_to_the_verified_company(db):
    del db.records["contacts:C-1"]
    contact = await SourceRepository().get_contact("C-1")
    assert contact["id"] == "C-1" and contact["phone"] == "+15555550100"
    assert db.reads("contacts:C-1") == 1 and db.reads("company:C-1") == 1


async def test_get_contact_without_any_verified_route_is_unavailable(db):
    db.records["contacts:C-1"] = [CONTACTS[0]]
    db.records["company:C-1"] = {**COMPANY, "verified": False}
    with pytest.raises(SourceUnavailable, match="no independently verified contact is available"):
        await SourceRepository().get_contact("C-1")


# Missing rows and errors


@pytest.mark.parametrize("mode", ["live", "fixture"])
async def test_missing_case_row_is_absent_in_both_modes(db, monkeypatch, mode):
    monkeypatch.setenv("FREIGHTOPS_MODE", mode)
    with pytest.raises(SourceUnavailable, match="^case is absent from the configured source dataset$"):
        await SourceRepository().load(env("CASE-404", load_id="FROM-SUBJECT"))
    assert db.reads("dataset:meta") == 0


@pytest.mark.parametrize(("failure", "message"), [
    (lambda request: httpx.Response(500, text="secret server detail"), "SpacetimeDB HTTP 500 \\(server\\)"),
    (lambda request: httpx.Response(404), "SpacetimeDB HTTP 404 \\(not_found\\)"),
    (lambda request: httpx.Response(401), "SpacetimeDB HTTP 401 \\(authorization\\)"),
    (lambda request: httpx.Response(200, text="not json"), "SpacetimeDB SQL response is not JSON"),
    (lambda request: httpx.Response(200, json={"rows": []}), "SpacetimeDB returned an invalid SQL result"),
])
async def test_http_and_protocol_errors_map_to_source_unavailable(db, failure, message):
    db.failure = failure
    with pytest.raises(SourceUnavailable, match=f"^source dataset could not be read: {message}$") as caught:
        await SourceRepository().load(env())
    assert isinstance(caught.value.__cause__, SpacetimeError)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(("error", "message"), [
    (httpx.ConnectError("refused"), "SpacetimeDB transport failed"),
    (httpx.ReadTimeout("slow"), "SpacetimeDB request timed out"),
])
async def test_transport_errors_map_to_source_unavailable(db, error, message):
    def fail(request):
        raise error

    db.failure = fail
    for read in (lambda repo: repo.load(env()), lambda repo: repo.get_company("C-1"),
                 lambda repo: repo.get_contact("C-1")):
        with pytest.raises(SourceUnavailable, match=f"^source dataset could not be read: {message}$"):
            await read(SourceRepository())


async def test_a_meta_read_failure_after_the_case_read_is_unavailable(db):
    def fail_meta(request):
        if "dataset:meta" in request.content.decode():
            return httpx.Response(503)
        return db.respond(request)

    db.failure = fail_meta
    with pytest.raises(SourceUnavailable, match="SpacetimeDB HTTP 503"):
        await SourceRepository().load(env())


async def test_disabled_spacetime_maps_to_source_unavailable(monkeypatch):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    with pytest.raises(SourceUnavailable, match="^source dataset could not be read: SpacetimeDB is disabled$"):
        await SourceRepository().load(env())


async def test_client_configuration_errors_map_to_source_unavailable(monkeypatch):
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("SPACETIME_URL", "ftp://nowhere")
    with pytest.raises(SourceUnavailable, match="SPACETIME_URL must be an HTTP"):
        await SourceRepository().load(env())
    monkeypatch.delenv("SPACETIME_URL")
    with pytest.raises(SourceUnavailable, match="actor must be a service name"):
        await SourceRepository(actor="Not An Actor").load(env())


async def test_nul_bytes_in_identifiers_never_reach_sql(db):
    with pytest.raises(SourceUnavailable, match="SQL filter must be a string without NUL bytes"):
        await SourceRepository().get_company("C-1\x00")
    assert db.queries == []


@pytest.mark.parametrize("rows", [
    [row("case:CASE-1", CASE), row("case:CASE-1", CASE)],
    [row("case:CASE-2", CASE)],
    [["case:CASE-1", "case", "CASE-1", {"nested": "object"}, "digest", "now"]],
])
async def test_malformed_rows_are_refused(db, rows):
    db.failure = lambda request: sql_response(rows)
    with pytest.raises(SourceUnavailable, match="SpacetimeDB returned an invalid source record"):
        await SourceRepository().load(env())


async def test_unparseable_record_json_is_refused(db):
    db.failure = lambda request: sql_response([row("case:CASE-1", None, raw="{not json")])
    with pytest.raises(SourceUnavailable, match="source record could not be parsed"):
        await SourceRepository().load(env())


# Cache


async def test_records_are_cached_for_five_seconds_and_meta_for_thirty(db, environment):
    clock = environment
    await SourceRepository().load(env())
    clock[0] += 4.9
    await SourceRepository().load(env())
    assert db.reads("case:CASE-1") == 1 and db.reads("dataset:meta") == 1
    clock[0] += 0.2
    await SourceRepository().load(env())
    assert db.reads("case:CASE-1") == 2 and db.reads("dataset:meta") == 1
    clock[0] += 25
    await SourceRepository().load(env())
    assert db.reads("case:CASE-1") == 3 and db.reads("dataset:meta") == 2


async def test_a_changed_record_is_seen_after_the_ttl(db, environment):
    clock = environment
    await SourceRepository().load(env())
    db.records["case:CASE-1"] = {**CASE, "invoice_amount": 1000}
    assert (await SourceRepository().load(env()))["invoice_amount"] == 940.5
    clock[0] += 5
    assert (await SourceRepository().load(env()))["invoice_amount"] == 1000


async def test_cache_is_shared_across_read_paths_and_repositories(db):
    await SourceRepository().get_company("C-1")
    del db.records["contacts:C-1"]
    await SourceRepository(actor="carrier_ap").get_contact("C-1")
    assert db.reads("company:C-1") == 1


async def test_absent_rows_and_failures_are_not_cached(db):
    with pytest.raises(SourceUnavailable, match="case is absent"):
        await SourceRepository().load(env("CASE-2"))
    db.records["case:CASE-2"] = {"load_id": "ARRIVED"}
    assert (await SourceRepository().load(env("CASE-2")))["load_id"] == "ARRIVED"
    db.failure = lambda request: httpx.Response(503)
    with pytest.raises(SourceUnavailable):
        await SourceRepository().get_company("C-1")
    db.failure = None
    assert (await SourceRepository().get_company("C-1"))["id"] == "C-1"


async def test_callers_cannot_mutate_cached_records(db):
    first = await SourceRepository().load(env())
    first["carrier"]["id"] = "TAMPERED"
    first["documents"] = ["forged"]
    second = await SourceRepository().load(env())
    assert second["carrier"]["id"] == "C-1" and "documents" not in second
    assert db.reads("case:CASE-1") == 1


async def test_cache_is_scoped_to_the_database(monkeypatch):
    first, second = Database(**default_records()), Database(**default_records())
    second.records["case:CASE-1"] = {**CASE, "load_id": "OTHER-DB"}
    assert (await SourceRepository(client=first.client()).load(env()))["load_id"] == "L-1"
    other = second.client(database="freightops-other")
    assert (await SourceRepository(client=other).load(env()))["load_id"] == "OTHER-DB"


async def test_expired_entries_are_pruned(db, environment, monkeypatch):
    clock = environment
    monkeypatch.setattr(source_repository, "_CACHE_LIMIT", 8)
    for index in range(12):
        db.records[f"company:C-{index}"] = {"id": f"C-{index}"}
        await SourceRepository().get_company(f"C-{index}")
    assert len(source_repository._cache) == 13
    clock[0] += 31
    await SourceRepository().get_company("C-1")
    assert set(source_repository._cache) == {("http://spacetime.test", "freightops-test", record_id)
                                             for record_id in ("company:C-1", "dataset:meta")}


# Readiness


def ready_executor():
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    return SimpleNamespace(name="risk", store=SimpleNamespace(engine=engine))


@pytest.fixture
def live_ready(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("SPACETIME_TOKEN_RISK", "risk-token")
    monkeypatch.setenv("SPACETIME_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("SPACETIME_DB", "freightops")
    for actor in SERVICE_ACTORS:
        monkeypatch.setenv(f"A2A_TOKEN_{actor.upper()}", f"token-{actor}")


def test_readiness_accepts_the_spacetime_backend_without_a_source_file(live_ready):
    _readiness(ready_executor())


def test_readiness_accepts_a_readable_source_file(live_ready, monkeypatch, tmp_path):
    path = tmp_path / "source.json"
    path.write_text("{}")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    for backend in ("file", ""):
        monkeypatch.setenv("SOURCE_DATA_BACKEND", backend)
        _readiness(ready_executor())


@pytest.mark.parametrize("path", [None, "missing.json"])
def test_readiness_rejects_neither_source_configured(live_ready, monkeypatch, tmp_path, path):
    monkeypatch.delenv("SOURCE_DATA_BACKEND")
    if path:
        monkeypatch.setenv("SOURCE_DATA_PATH", str(tmp_path / path))
    with pytest.raises(ValueError, match="live mode requires an accessible source dataset"):
        _readiness(ready_executor())
    monkeypatch.setenv("SOURCE_DATA_BACKEND", "file")
    with pytest.raises(ValueError, match="live mode requires an accessible source dataset"):
        _readiness(ready_executor())


def test_readiness_still_requires_spacetime_for_the_spacetime_backend(live_ready, monkeypatch):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    with pytest.raises(ValueError, match="live mode requires SpacetimeDB"):
        _readiness(ready_executor())


def test_readiness_rejects_an_unknown_backend(live_ready, monkeypatch, tmp_path):
    path = tmp_path / "source.json"
    path.write_text("{}")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    monkeypatch.setenv("SOURCE_DATA_BACKEND", "postgres")
    with pytest.raises(ValueError, match="SOURCE_DATA_BACKEND must be file or spacetime"):
        _readiness(ready_executor())
