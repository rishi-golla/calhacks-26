"""HTTP-boundary tests: no server, real HTTPX request encoding/response parsing."""

import asyncio
import json
import uuid

import httpx
import pytest

from common import policy
from common import spacetime_client as spacetime


def sql_response(columns, rows):
    return [{"schema": {"elements": [{"name": {"some": name}} for name in columns]}, "rows": rows}]


def row_response(row):
    return httpx.Response(200, json=sql_response(list(row), [list(row.values())]))


def approved_record(**changes):
    return {
        "id": "APP-1", "case_id": "CASE-1", "invoice_id": "INV-1",
        "action": "propose_payment", "target_id": "PAY-1",
        "amount": 7000, "decision": "approved", "code_ok": True, "consumed_at": "",
        "expires_at": "2999-01-01T00:00:00Z", "approver": "human-identity",
        "requester_identity": "controller-identity", "approver_name": "human:alan", "requested_by": "controller",
        **changes,
    }


def client_with(handler, **kwargs):
    return spacetime.SpacetimeClient(disabled=False, transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    # Tests must be isolated from local development service credentials/settings.
    import os
    for name in os.environ:
        if name.startswith("SPACETIME_"):
            monkeypatch.delenv(name)


async def test_business_write_uses_policy_actor_and_single_json_string(monkeypatch):
    requests = []
    decisions = []

    def check(action, actor, payload):
        decisions.append((action, actor, payload))
        return policy.Decision("allow")

    def handler(request):
        requests.append(request)
        assert decisions, "policy must run before the HTTP mutation"
        return httpx.Response(200)

    monkeypatch.setattr(policy, "check", check)
    original = {"case_id": "CASE-001", "amount": 123.45, "write_id": "known-retry-id"}
    client = client_with(handler, actor="controller", token="controller-secret", timeout=2.5,
                         url="http://localhost:3000/prefix/", database="team/freight ops")
    receipt = await client.mutate("place_hold", original)
    request = requests[0]
    assert request.url.raw_path == b"/prefix/v1/database/team%2Ffreight%20ops/call/place_hold"
    assert request.headers["authorization"] == "Bearer controller-secret"
    assert request.extensions["timeout"]["read"] == 2.5
    arguments = json.loads(request.content)
    assert len(arguments) == 1 and isinstance(arguments[0], str)
    assert json.loads(arguments[0]) == original
    assert decisions == [("place_hold", "controller", original)]
    assert receipt == spacetime.ReducerReceipt("place_hold", 200, "known-retry-id")


async def test_auto_write_id_is_reported_without_modifying_callers_payload():
    requests = []

    def handler(request):
        requests.append(json.loads(json.loads(request.content)[0]))
        return httpx.Response(204)

    payload = {"case_id": "CASE-001"}
    receipt = await client_with(handler, actor="ingest").mutate("open_case", payload)
    assert str(uuid.UUID(receipt.write_id)) == receipt.write_id
    assert requests[0]["write_id"] == receipt.write_id
    assert payload == {"case_id": "CASE-001"}


async def test_per_actor_credentials_and_runtime_environment(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("SPACETIME_ACTOR", "carrier_ap")
    monkeypatch.setenv("SPACETIME_TOKEN", "shared-secret")
    monkeypatch.setenv("SPACETIME_TOKEN_CARRIER_AP", "ap-secret")
    monkeypatch.setenv("SPACETIME_URL", "http://first.local:3000")
    transport = httpx.MockTransport(handler)
    first = spacetime.SpacetimeClient(transport=transport)
    monkeypatch.setenv("SPACETIME_URL", "http://second.local:3000")
    second = spacetime.SpacetimeClient(actor="risk", transport=transport)
    third = spacetime.SpacetimeClient(actor="risk", token="explicit-secret", transport=transport)
    await first.mutate("agent_heartbeat", {})
    await second.mutate("agent_heartbeat", {})
    await third.mutate("agent_heartbeat", {})
    assert [str(request.url.host) for request in requests] == ["first.local", "second.local", "second.local"]
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer ap-secret", "Bearer shared-secret", "Bearer explicit-secret",
    ]


async def test_disabled_default_prevents_network_and_legacy_returns_false():
    def handler(request):
        pytest.fail("disabled persistence must not make HTTP requests")

    client = spacetime.SpacetimeClient(actor="controller", transport=httpx.MockTransport(handler))
    with pytest.raises(spacetime.SpacetimeDisabled):
        await client.mutate("place_hold", {})
    with pytest.raises(spacetime.SpacetimeDisabled):
        await client.query_rows("SELECT * FROM hold")
    assert await spacetime.call_reducer("place_hold", "sensitive-argument") is False


@pytest.mark.parametrize(("name", "actor", "payload", "result"), [
    ("propose_payment", "carrier_ap", {"amount": 10, "on_hold": True}, "deny"),
    ("propose_payment", "carrier_ap", {"amount": 5001, "human_approval_id": "fake"}, "needs_approval"),
    ("release_hold", "controller", {"amount": 5001}, "needs_approval"),
    ("post_journal_entry", "controller", {"lines": [{"debit": 20}, {"credit": 19}]}, "deny"),
    ("decide_approval", "human", {"requested_by": "human"}, "deny"),
])
async def test_policy_denials_and_approval_requirements_never_make_requests(name, actor, payload, result):
    def handler(request):
        pytest.fail("policy denial must stop dispatch")

    client = client_with(handler, actor=actor)
    with pytest.raises(spacetime.SpacetimePolicyError) as error:
        await client.mutate(name, payload)
    assert error.value.decision.result == result


async def test_approval_reference_is_still_checked_by_server():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/sql"):
            if b'"hold"' in request.content:
                return row_response({"id": "HOLD-1", "case_id": "CASE-1", "invoice_id": "INV-1",
                                     "amount": 7000, "released_amount": 0})
            return row_response(approved_record(action="release_hold", target_id="HOLD-1"))
        return httpx.Response(400, text="approval does not exist; secret details")

    client = client_with(handler, actor="controller")
    with pytest.raises(spacetime.SpacetimeHTTPError) as error:
        await client.mutate("release_hold", {"id": "HOLD-1", "amount": 7000, "human_approval_id": "APP-1"})
    assert len(requests) == 3
    assert error.value.kind == "rejected"
    assert "secret" not in str(error.value)


async def test_high_payment_with_stored_approval_preserves_original_amount_and_payload():
    requests = []
    payload = {"id": "PAY-1", "case_id": "CASE-1", "invoice_id": "INV-1",
               "amount": 7000, "approval_id": "APP-1", "write_id": "pay-1"}

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/sql"):
            if b'"invoice"' in request.content:
                return row_response({"id": "INV-1", "case_id": "CASE-1", "amount": 7000})
            return row_response(approved_record())
        assert json.loads(json.loads(request.content)[0]) == payload
        return httpx.Response(200)

    receipt = await client_with(handler, actor="carrier_ap").mutate("propose_payment", payload)
    assert receipt.write_id == "pay-1"
    assert len(requests) == 3


@pytest.mark.parametrize("changes", [
    {"decision": "pending"}, {"code_ok": False}, {"consumed_at": "2026-10-24T00:00:00Z"},
    {"expires_at": "2000-01-01T00:00:00Z"}, {"action": "release_hold"}, {"target_id": "PAY-OTHER"},
    {"case_id": "CASE-OTHER"}, {"amount": 7000.01}, {"approver": "controller-identity"},
    {"approver_name": "controller"}, {"expires_at": "bad"}, {"invoice_id": "INV-OTHER"},
])
async def test_approval_must_match_unconsumed_unexpired_separated_authorization(changes):
    def handler(request):
        assert request.url.path.endswith("/sql"), "invalid authorization must prevent the mutation"
        if b'"invoice"' in request.content:
            return row_response({"id": "INV-1", "case_id": "CASE-1", "amount": 7000})
        return row_response(approved_record(**changes))

    with pytest.raises(spacetime.SpacetimePolicyError) as error:
        await client_with(handler, actor="carrier_ap").mutate("propose_payment", {
            "id": "PAY-1", "case_id": "CASE-1", "invoice_id": "INV-1", "amount": 7000, "approval_id": "APP-1",
        })
    assert error.value.decision.result == "needs_approval"


@pytest.mark.parametrize(("invoice_amount", "approval_amount", "allowed"), [
    (9400, None, False), (9400, 9400, False), (9400, 4700, True), (5000, None, True),
])
async def test_partial_payment_approval_uses_stored_invoice_threshold(invoice_amount, approval_amount, allowed):
    mutations = []

    def handler(request):
        if request.url.path.endswith("/sql"):
            if b'"invoice"' in request.content:
                return row_response({"id": "INV-1", "case_id": "CASE-1", "amount": invoice_amount})
            return row_response(approved_record(amount=approval_amount))
        mutations.append(json.loads(json.loads(request.content)[0]))
        return httpx.Response(200)

    payload = {"id": "PAY-1", "case_id": "CASE-1", "invoice_id": "INV-1", "amount": 4700,
               "write_id": "partial-payment"}
    if approval_amount is not None:
        payload["approval_id"] = "APP-1"
    client = client_with(handler, actor="carrier_ap")
    if allowed:
        await client.mutate("propose_payment", payload)
        assert mutations == [payload]
    else:
        with pytest.raises(spacetime.SpacetimePolicyError) as error:
            await client.mutate("propose_payment", payload)
        assert error.value.decision.result == "needs_approval"
        assert mutations == []


async def test_release_without_amount_checks_actual_remaining_hold_and_approval_alias():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/sql"):
            if b'"hold"' in request.content:
                return row_response({"id": "HOLD-1", "case_id": "CASE-1", "invoice_id": "INV-1",
                                     "amount": 9000, "released_amount": 2000})
            return row_response(approved_record(action="release_hold", target_id="HOLD-1"))
        body = json.loads(json.loads(request.content)[0])
        assert "amount" not in body, "preflight must not change the stable mutation payload"
        return httpx.Response(200)

    await client_with(handler, actor="controller").mutate("release_hold", {"id": "HOLD-1", "approval_id": "APP-1"})
    assert len(requests) == 3


@pytest.mark.parametrize(("original", "approval_amount", "allowed"), [
    (9400, None, False), (9400, 9400, False), (9400, 4700, True), (5000, None, True),
])
async def test_partial_release_approval_uses_original_hold_threshold(original, approval_amount, allowed):
    mutations = []

    def handler(request):
        if request.url.path.endswith("/sql"):
            if b'"hold"' in request.content:
                return row_response({"id": "HOLD-1", "case_id": "CASE-1", "invoice_id": "INV-1",
                                     "amount": original, "released_amount": original - 4700})
            return row_response(approved_record(action="release_hold", target_id="HOLD-1",
                                                amount=approval_amount))
        mutations.append(json.loads(json.loads(request.content)[0]))
        return httpx.Response(200)

    payload = {"id": "HOLD-1", "amount": 4700, "write_id": "partial-release"}
    if approval_amount is not None:
        payload["approval_id"] = "APP-1"
    client = client_with(handler, actor="controller")
    if allowed:
        await client.mutate("release_hold", payload)
        assert mutations == [payload]
    else:
        with pytest.raises(spacetime.SpacetimePolicyError) as error:
            await client.mutate("release_hold", payload)
        assert error.value.decision.result == "needs_approval"
        assert mutations == []


@pytest.mark.parametrize(("requester", "allowed"), [("carrier_ap", True), ("controller", False)])
async def test_post_journal_reads_real_request_lines_and_requester(requester, allowed):
    requests = []
    payload = {"id": "JE-1", "request_id": "JR-1", "write_id": "post-1"}

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/sql"):
            return row_response({"id": "JR-1", "requested_by": requester,
                                 "lines": [["expense", 100, 0], ["payable", 0, 100]]})
        assert json.loads(json.loads(request.content)[0]) == payload
        return httpx.Response(200)

    client = client_with(handler, actor="controller")
    if allowed:
        await client.mutate("post_journal_entry", payload)
        assert len(requests) == 2
    else:
        with pytest.raises(spacetime.SpacetimePolicyError):
            await client.mutate("post_journal_entry", payload)
        assert len(requests) == 1


async def test_scoped_human_approval_receipt_is_not_claim_of_success(monkeypatch):
    monkeypatch.setenv("SPACETIME_TOKEN_HUMAN_ALAN", "human-secret")

    def handler(request):
        assert request.headers["authorization"] == "Bearer human-secret"
        if request.url.path.endswith("/sql"):
            return row_response(approved_record(decision="pending", code_ok=False, last_error="Invalid code"))
        return httpx.Response(200)

    client = client_with(handler, actor="human:alan")
    receipt = await client.mutate("decide_approval", {"id": "APP-1", "decision": "approved", "code": "wrong"})
    assert receipt.status_code == 200
    assert not hasattr(receipt, "approved")
    row = (await client.query_rows("SELECT * FROM approval"))[0]
    assert row["decision"] == "pending"


@pytest.mark.parametrize(("status", "kind", "uncertain"), [
    (400, "rejected", False), (401, "authorization", False), (403, "authorization", False),
    (404, "not_found", False), (409, "conflict", False), (429, "rate_limited", False),
    (500, "server", True), (503, "server", True), (307, "rejected", False),
    (530, "rejected", False),   # a reducer that threw: the write rolled back, nothing committed
])
async def test_http_failures_classified_without_retry_redirect_or_body_leak(status, kind, uncertain):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="SECRET-BODY", headers={"Location": "http://untrusted.test"})

    with pytest.raises(spacetime.SpacetimeHTTPError) as error:
        await client_with(handler, actor="ingest").mutate("open_case", {"write_id": "stable"})
    assert len(requests) == 1
    assert error.value.status_code == status
    assert error.value.kind == kind
    assert error.value.may_have_committed is uncertain
    assert error.value.write_id == "stable"
    assert "SECRET-BODY" not in str(error.value)


@pytest.mark.parametrize(("error_type", "timeout", "uncertain"), [
    (httpx.ConnectError, False, False), (httpx.ConnectTimeout, True, False),
    (httpx.ReadTimeout, True, True), (httpx.WriteError, False, True),
])
async def test_transport_failures_preserve_reconciliation_id(error_type, timeout, uncertain):
    requests = []

    def handler(request):
        requests.append(request)
        raise error_type("SECRET network diagnostics", request=request)

    with pytest.raises(spacetime.SpacetimeTransportError) as error:
        await client_with(handler, actor="ingest").mutate("open_case", {})
    assert len(requests) == 1
    assert error.value.timeout is timeout
    assert error.value.may_have_committed is uncertain
    assert error.value.write_id == json.loads(json.loads(requests[0].content)[0])["write_id"]
    assert "SECRET" not in str(error.value)


async def test_legacy_preserves_arguments_and_does_not_log_secrets(monkeypatch, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 2:
            raise httpx.ReadTimeout("SECRET transport", request=request)
        return httpx.Response(200 if len(requests) == 1 else 400, text="SECRET server")

    client = client_with(handler, actor="ingest", token="SECRET token")
    monkeypatch.setattr(spacetime, "SpacetimeClient", lambda: client)
    args = (json.dumps({"title": "SECRET payload", "write_id": "stable"}),)
    assert await spacetime.call_reducer("open_case", *args) is True
    assert await spacetime.call_reducer("open_case", *args) is False
    assert await spacetime.call_reducer("open_case", *args) is False
    assert json.loads(requests[0].content) == list(args)
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("kwargs", [
    {"url": "ftp://localhost"}, {"url": "http://user:secret@localhost"},
    {"url": "http://localhost?token=secret"}, {"url": "http://localhost/#fragment"},
    {"url": "http://localhost:invalid"}, {"url": "http://localhost\n"},
    {"database": ""}, {"database": ".."}, {"token": "secret\r\nHeader: injected"},
    {"timeout": 0}, {"timeout": float("inf")}, {"actor": "../controller"},
])
def test_invalid_configuration_fails_before_network(kwargs):
    with pytest.raises(spacetime.SpacetimeConfigurationError):
        spacetime.SpacetimeClient(**kwargs)


async def test_mutations_require_actor_valid_policy_inputs_and_json():
    def handler(request):
        pytest.fail("invalid mutations must not reach the network")

    with pytest.raises(spacetime.SpacetimeConfigurationError):
        await client_with(handler).mutate("open_case", {})
    client = client_with(handler, actor="carrier_ap")
    for name, payload in [
        ("../open_case", {}), ("propose_payment", {}), ("open_case", {"write_id": ""}),
        ("open_case", {"value": float("nan")}), ("open_case", {"value": object()}),
        ("post_journal_entry", {"lines": [1]}),
    ]:
        with pytest.raises(spacetime.SpacetimeConfigurationError):
            await client.mutate(name, payload)


async def test_query_posts_plaintext_and_preserves_values_without_actor():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=sql_response(["id", "timestamp", "optional"], [
            ["18446744073709551615", [123456789], {"some": "value"}],
        ]))

    result = await client_with(handler, token="").query(" SELECT * FROM audit_event; ")
    assert requests[0].content == b"SELECT * FROM audit_event;"
    assert requests[0].headers["content-type"].startswith("text/plain")
    assert "authorization" not in requests[0].headers
    assert result.columns == ("id", "timestamp", "optional")
    assert result.rows == ({"id": "18446744073709551615", "timestamp": [123456789],
                            "optional": {"some": "value"}},)


@pytest.mark.parametrize("sql", [
    "DELETE FROM hold", "UPDATE hold SET status = 'released'", "SELECT * FROM hold; DELETE FROM hold",
    "SELECT * FROM hold -- comment", "SELECT * FROM hold /* comment */", "SELECT 'unterminated",
])
async def test_sql_read_api_rejects_mutation_and_multiple_statements(sql):
    def handler(request):
        pytest.fail("invalid SQL must not reach the network")

    with pytest.raises(spacetime.SpacetimeConfigurationError):
        await client_with(handler).query_rows(sql)


async def test_agent_config_escapes_filter_and_preserves_disabled_setting():
    agent = "risk'; DELETE FROM hold; --"

    def handler(request):
        assert request.content.decode() == (
            "SELECT * FROM agent_config WHERE agent = 'risk''; DELETE FROM hold; --'"
        )
        return httpx.Response(200, json=sql_response(["agent", "model", "enabled"], [[agent, "pinned", False]]))

    assert await client_with(handler).get_agent_config(agent) == spacetime.AgentConfig(agent, "pinned", False)


@pytest.mark.parametrize("document", [
    {}, [], [{"schema": {"elements": []}, "rows": [[1]]}],
    sql_response(["id", "id"], [[1, 2]]), [{"schema": {"elements": [{"name": {"none": []}}]}, "rows": []}],
])
async def test_malformed_sql_response_raises_protocol_error(document):
    client = client_with(lambda request: httpx.Response(200, json=document))
    with pytest.raises(spacetime.SpacetimeProtocolError):
        await client.query_rows("SELECT * FROM hold")


async def test_non_json_sql_response_raises_safe_protocol_error():
    client = client_with(lambda request: httpx.Response(200, text="SECRET invalid document"))
    with pytest.raises(spacetime.SpacetimeProtocolError) as error:
        await client.query_rows("SELECT * FROM hold")
    assert "SECRET" not in str(error.value)


async def test_memory_filters_scopes_and_orders_specific_recent_versions():
    columns = ["id", "scope", "version", "text"]
    rows = [
        ["global", "{}", 99, "Global instruction"],
        ["other", '{"carrier_id":"C-002"}', 10, "Wrong carrier"],
        ["older", '{"carrier_id":"C-001"}', 1, "Earlier memory"],
        ["specific", '{"carrier_id":"C-001","skill":"evaluate_invoice"}', 2, "Relevant detail"],
        ["recent", '{"carrier_id":"C-001"}', 3, "Newer memory"],
    ]

    def handler(request):
        assert request.content == b"SELECT * FROM agent_memory WHERE agent = 'carrier_ap' AND active = true"
        return httpx.Response(200, json=sql_response(columns, rows))

    client = client_with(handler)
    matching = await client.get_agent_memory("carrier_ap", scope={"carrier_id": "C-001",
                                                                "skill": "evaluate_invoice"}, limit=3)
    assert [row["id"] for row in matching] == ["specific", "recent", "older"]
    assert matching[0]["scope"] == {"carrier_id": "C-001", "skill": "evaluate_invoice"}
    assert [row["id"] for row in await client.get_agent_memory("carrier_ap")] == ["global"]


async def test_missing_config_and_instruction_versions_are_explicit_none():
    client = client_with(lambda request: httpx.Response(200, json=sql_response(["agent"], [])))
    assert await client.get_agent_config("risk") is None
    assert await client.get_instruction_version("risk") is None


async def test_instruction_version_refuses_ambiguous_active_rows():
    client = client_with(lambda request: httpx.Response(200, json=sql_response(
        ["agent", "version"], [["risk", 1], ["risk", 2]],
    )))
    with pytest.raises(spacetime.SpacetimeProtocolError):
        await client.get_instruction_version("risk")


async def test_instruction_history_sorts_and_preserves_revert_anchors():
    client = client_with(lambda request: httpx.Response(200, json=sql_response(
        ["agent", "version", "diff", "reverts_version"],
        [["risk", 3, "first patch", 1], ["risk", 1, "first patch", 0], ["risk", 2, "second patch", 0]],
    )))
    history = await client.get_instruction_history("risk")
    assert [row["version"] for row in history] == [1, 2, 3]
    assert history[-1]["reverts_version"] == 1
    assert history[-1]["diff"] == "first patch"


async def test_polling_emits_initial_and_changed_snapshots_ignores_order_and_stops():
    stop = asyncio.Event()
    snapshots = iter([[[1], [2]], [[2], [1]], [[1], [3]]])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=sql_response(["id"], next(snapshots)))

    received = []
    async for rows in client_with(handler).poll_rows("SELECT * FROM hold", interval_s=0.001, stop=stop):
        received.append(rows)
        if len(received) == 2:
            stop.set()
    assert len(requests) == 3
    assert received == [[{"id": 1}, {"id": 2}], [{"id": 1}, {"id": 3}]]


async def test_polling_failure_propagates_instead_of_returning_empty_state():
    client = client_with(lambda request: httpx.Response(503))
    with pytest.raises(spacetime.SpacetimeHTTPError) as error:
        await anext(client.poll_rows("SELECT * FROM hold"))
    assert error.value.may_have_committed is False
