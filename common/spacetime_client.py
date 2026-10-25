"""Python boundary for the W2 SpacetimeDB 2.x module.

Use ``SpacetimeClient(actor="controller").mutate("place_hold", payload)`` for
business writes. It checks the shared policy, sends the module's single JSON
string argument and raises a classified exception on failure. Callers decide
whether persistence is mandatory; this module never retries a mutation.

Environment is read when a client is constructed, not at import time:
``SPACETIME_URL``, ``SPACETIME_DB``, ``SPACETIME_TIMEOUT_SECONDS`` (default 5),
``SPACETIME_ACTOR``, and ``SPACETIME_TOKEN_<ACTOR>`` (uppercase, e.g.
``SPACETIME_TOKEN_CARRIER_AP``), falling back to ``SPACETIME_TOKEN``. Each token
must be registered to the actor's server-side role. ``SPACETIME_DISABLED``
defaults to ``1`` to preserve the existing mock stack; set it to ``0`` for live
calls. Explicit constructor options override the environment.

HTTP contract: https://spacetimedb.com/docs/http/database/
The generated TypeScript SDK owns live subscriptions. ``poll_rows`` provides
a read-only HTTP snapshot fallback; it does not pretend to deliver row events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx

from common import policy

log = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\Z")
_ACTOR = re.compile(r"[a-z][a-z0-9_]*(?::[A-Za-z0-9_.-]+)?\Z")


class SpacetimeError(Exception):
    """Base for safe-to-log client errors; messages contain no response bodies."""

    write_id: str | None = None


class SpacetimeConfigurationError(SpacetimeError, ValueError):
    """A required option or input is missing or malformed."""


class SpacetimeDisabled(SpacetimeError):
    """Persistence is disabled; no request was made."""


class SpacetimePolicyError(SpacetimeError):
    """The shared policy denied the write or requires approval before dispatch."""

    def __init__(self, reducer: str, decision: policy.Decision):
        self.reducer = reducer
        self.decision = decision
        super().__init__(f"SpacetimeDB policy {decision.result} for {reducer}")


class SpacetimeTransportError(SpacetimeError):
    """A network error. An ambiguous write must be reconciled by its write_id."""

    def __init__(self, *, timeout: bool, may_have_committed: bool):
        self.timeout = timeout
        self.may_have_committed = may_have_committed
        super().__init__("SpacetimeDB request timed out" if timeout else "SpacetimeDB transport failed")


HTTPFailure = Literal["authorization", "not_found", "conflict", "rate_limited", "rejected", "server"]


class SpacetimeHTTPError(SpacetimeError):
    """Non-success HTTP status, without potentially sensitive server error text."""

    def __init__(self, status_code: int, *, mutation: bool):
        self.status_code = status_code
        self.kind: HTTPFailure = {
            401: "authorization", 403: "authorization", 404: "not_found",
            409: "conflict", 429: "rate_limited",
        }.get(status_code, "server" if status_code >= 500 else "rejected")
        self.may_have_committed = mutation and (status_code >= 500 or status_code == 408)
        super().__init__(f"SpacetimeDB HTTP {status_code} ({self.kind})")


class SpacetimeProtocolError(SpacetimeError):
    """The server's SQL response does not match the documented wire format."""


@dataclass(frozen=True)
class ReducerReceipt:
    """Transport acceptance, not a business outcome (e.g. an OTP can be invalid).

    Read the affected row after success to learn its current business state.
    """

    reducer: str
    status_code: int
    write_id: str | None


@dataclass(frozen=True)
class QueryResult:
    """Named SQL rows; SATS values are preserved without lossy numeric coercion."""

    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AgentConfig:
    agent: str
    model: str
    enabled: bool


def _sql_string(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise SpacetimeConfigurationError("SQL filter must be a string without NUL bytes")
    return "'" + value.replace("'", "''") + "'"


def _read_query(sql: str) -> str:
    """Accept one SELECT; quoted semicolons and doubled quotes are literals."""
    if not isinstance(sql, str) or not re.match(r"\s*SELECT\s", sql, re.IGNORECASE):
        raise SpacetimeConfigurationError("SQL reads require one SELECT statement")
    statement = sql.strip()
    quote_char = None
    position = 0
    while position < len(statement):
        char = statement[position]
        if char == "\x00":
            raise SpacetimeConfigurationError("SQL must not contain NUL bytes")
        if quote_char:
            if char == quote_char:
                if statement[position + 1:position + 2] == quote_char:
                    position += 1
                else:
                    quote_char = None
        elif char in ("'", '"'):
            quote_char = char
        elif statement[position:position + 2] in ("--", "/*"):
            raise SpacetimeConfigurationError("SQL comments are not supported by the read-only client")
        elif char == ";" and position != len(statement) - 1:
            raise SpacetimeConfigurationError("SQL reads require one SELECT statement")
        position += 1
    if quote_char:
        raise SpacetimeConfigurationError("SQL contains an unterminated quoted value")
    return statement


def canonical_sql_column(name: str) -> str:
    """Normalize the module SDK's numeric acronym spelling to the Python contract."""
    return "a2a_state" if name == "a_2_a_state" else name


def journal_lines(value: Any) -> list:
    """Journal lines as the policy and reducers take them: ``{account, debit, credit}`` objects.

    SQL reads return the module's product type as ``[account, debit, credit]`` triples; anything else is
    passed through unchanged so a malformed payload still fails validation instead of being guessed at.
    """
    if not isinstance(value, list):
        return value
    return [dict(zip(("account", "debit", "credit"), line, strict=True))
            if isinstance(line, list) and len(line) == 3 else line for line in value]


def _query_result(value: Any) -> QueryResult:
    try:
        if not isinstance(value, list) or len(value) != 1:
            raise ValueError
        result = value[0]
        columns = []
        for element in result["schema"]["elements"]:
            name = element["name"]
            if isinstance(name, dict):
                name = name.get("some")
            if not isinstance(name, str) or not name:
                raise ValueError
            name = canonical_sql_column(name)
            if name in columns:
                raise ValueError
            columns.append(name)
        rows = result["rows"]
        if not isinstance(rows, list):
            raise TypeError
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError
        return QueryResult(tuple(columns), tuple(dict(zip(columns, row, strict=True)) for row in rows))
    except (KeyError, TypeError, ValueError):
        raise SpacetimeProtocolError("SpacetimeDB returned an invalid SQL result") from None


class SpacetimeClient:
    """An explicit actor and its service token, independent of other agents.

    ``actor`` is policy context, never authentication: the server authenticates
    the bearer token and enforces its registered role. Do not share a privileged
    token across unrelated services. ``transport`` permits deterministic HTTP
    tests. Redirects are not followed and writes are never retried.
    """

    def __init__(
        self,
        *,
        actor: str | None = None,
        url: str | None = None,
        database: str | None = None,
        token: str | None = None,
        disabled: bool | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.actor = actor if actor is not None else os.getenv("SPACETIME_ACTOR") or None
        if self.actor is not None and not _ACTOR.fullmatch(self.actor):
            raise SpacetimeConfigurationError("actor must be a service name or a scoped name such as human:alan")
        self.url = (url if url is not None else os.getenv("SPACETIME_URL", "http://localhost:3000"))
        try:
            parsed = urlsplit(self.url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                     and not re.search(r"\s|[\x00-\x1f]", self.url))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise SpacetimeConfigurationError("SPACETIME_URL must be an HTTP(S) URL without credentials or query")
        self.url = self.url.rstrip("/")
        self.database = database if database is not None else os.getenv("SPACETIME_DB", "freightops")
        if (not self.database or self.database in (".", "..")
                or re.search(r"[\x00-\x1f]", self.database)):
            raise SpacetimeConfigurationError("database must be a nonempty name or identity")
        if token is None:
            key = re.sub(r"[^A-Z0-9_]", "_", self.actor.upper()) if self.actor else ""
            actor_token = os.getenv(f"SPACETIME_TOKEN_{key}") if key else None
            token = actor_token or os.getenv("SPACETIME_TOKEN", "")
        if re.search(r"[\x00-\x1f\x7f]", token):
            raise SpacetimeConfigurationError("token must not contain control characters")
        self._token = token
        if disabled is None:
            flag = os.getenv("SPACETIME_DISABLED", "1").strip().lower()
            if flag not in ("0", "1", "true", "false"):
                raise SpacetimeConfigurationError("SPACETIME_DISABLED must be 0, 1, true or false")
            disabled = flag in ("1", "true")
        self.disabled = disabled
        try:
            self.timeout = float(timeout if timeout is not None else os.getenv("SPACETIME_TIMEOUT_SECONDS", "5"))
        except (TypeError, ValueError):
            raise SpacetimeConfigurationError("timeout must be a positive finite number") from None
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise SpacetimeConfigurationError("timeout must be a positive finite number")
        self._transport = transport

    async def _request(self, suffix: str, *, mutation: bool = False, **kwargs: Any) -> httpx.Response:
        if self.disabled:
            raise SpacetimeDisabled("SpacetimeDB is disabled")
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        headers.update(kwargs.pop("headers", {}))
        endpoint = f"{self.url}/v1/database/{quote(self.database, safe='')}/{suffix}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport,
                                        follow_redirects=False) as client:
                response = await client.post(endpoint, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            definitely_not_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            raise SpacetimeTransportError(
                timeout=isinstance(exc, httpx.TimeoutException),
                may_have_committed=mutation and not definitely_not_sent,
            ) from None
        if not response.is_success:
            raise SpacetimeHTTPError(response.status_code, mutation=mutation)
        return response

    async def _stored_row(self, table: str, row_id: Any) -> dict[str, Any]:
        # Table names are internal constants, never caller-supplied SQL fragments.
        rows = await self.query_rows(f'SELECT * FROM "{table}" WHERE id = {_sql_string(row_id)}')
        if len(rows) != 1:
            raise SpacetimePolicyError(table, policy.Decision("deny", reason="required stored row missing"))
        return rows[0]

    async def _verified_approval(self, name: str, payload: Mapping[str, Any]) -> None:
        approval_id = payload.get("approval_id") or payload.get("human_approval_id")
        if not approval_id or not all(payload.get(field) for field in ("id", "case_id", "amount")):
            raise SpacetimePolicyError(name, policy.Decision("needs_approval", ["P-003"]))
        row = await self._stored_row("approval", approval_id)
        try:
            expires = datetime.fromisoformat(row["expires_at"])
            valid = (
                row["action"] == name and row["target_id"] == payload["id"]
                and row["case_id"] == payload["case_id"]
                and row["invoice_id"] == payload["invoice_id"]
                and Decimal(str(row["amount"])) == Decimal(str(payload["amount"]))
                and row["decision"] == "approved" and row["code_ok"] is True
                and not row["consumed_at"] and expires > datetime.now(UTC)
                and bool(row["approver"]) and bool(row["requester_identity"])
                and row["approver"] != row["requester_identity"]
                and bool(row["approver_name"]) and row["approver_name"] != row["requested_by"]
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            valid = False
        if not valid:
            raise SpacetimePolicyError(name, policy.Decision(
                "needs_approval", ["P-003", "P-005"], "stored approval is not valid for this operation"
            ))

    async def _check_write(self, name: str, payload: Mapping[str, Any]) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SpacetimeConfigurationError("reducer must be a valid reducer name")
        if self.disabled:
            raise SpacetimeDisabled("SpacetimeDB is disabled")
        if not self.actor:
            raise SpacetimeConfigurationError("writes require an explicit actor or SPACETIME_ACTOR")
        action = "approve" if name == "decide_approval" else name
        checked = dict(payload)
        requires_stored_approval = False

        def decision_for(context):
            try:
                return policy.check(action, self.actor, context)
            except (AttributeError, KeyError, TypeError, ValueError):
                raise SpacetimeConfigurationError("write payload is missing valid policy inputs") from None

        # Deny explicit policy violations without an HTTP call. A reference is
        # only permission to read an approval, never proof that it was granted.
        decision = decision_for(checked)
        if decision.result == "deny":
            raise SpacetimePolicyError(name, decision)
        reference = checked.get("approval_id") or checked.get("human_approval_id")
        if decision.result == "needs_approval" and not reference:
            raise SpacetimePolicyError(name, decision)

        # Financial contract fields come from stored records, not invented empty
        # defaults. These reads are preflight only; the reducer checks atomically.
        if name == "post_journal_entry" and checked.get("request_id"):
            stored = await self._stored_row("journal_request", checked["request_id"])
            checked.update(lines=journal_lines(stored.get("lines")), requested_by=stored.get("requested_by"))
        if name == "release_hold" and checked.get("id"):
            stored = await self._stored_row("hold", checked["id"])
            try:
                original_amount = Decimal(str(stored["amount"]))
                remaining = float(original_amount - Decimal(str(stored["released_amount"])))
                requires_stored_approval = original_amount > policy.APPROVAL_THRESHOLD_USD
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise SpacetimeProtocolError("SpacetimeDB returned an invalid hold balance") from None
            checked.setdefault("amount", remaining)
            checked["case_id"] = stored["case_id"]
            checked["invoice_id"] = stored["invoice_id"]
        if name == "propose_payment" and checked.get("invoice_id"):
            stored = await self._stored_row("invoice", checked["invoice_id"])
            try:
                requires_stored_approval = Decimal(str(stored["amount"])) > policy.APPROVAL_THRESHOLD_USD
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise SpacetimeProtocolError("SpacetimeDB returned an invalid invoice amount") from None
        if name == "decide_approval" and checked.get("id"):
            stored = await self._stored_row("approval", checked["id"])
            checked["requested_by"] = stored.get("requested_by")

        if checked != payload:
            decision = decision_for(checked)
        if decision.result == "deny":
            raise SpacetimePolicyError(name, decision)
        if (requires_stored_approval or (name in ("release_hold", "propose_payment")
                and checked.get("amount", 0) > policy.APPROVAL_THRESHOLD_USD)):
            await self._verified_approval(name, checked)
            # The stored authorization fulfills needs_approval. In particular,
            # do not lower amount or fabricate fields to turn check() into allow.
            # Original hold/invoice size prevents splitting large operations
            # into smaller unapproved calls; approval binds the exact amount.
            return
        if decision.result != "allow":
            raise SpacetimePolicyError(name, decision)

    async def mutate(self, name: str, payload: Mapping[str, Any]) -> ReducerReceipt:
        """Call a W2 business reducer with policy checks and a stable write_id.

        Pass your own persisted ``write_id`` when recovering from timeouts. A
        new UUID is generated only when the key is absent. The payload is not
        mutated. ``needs_approval`` is fulfilled only by checking the stored,
        unexpired authorization for the exact action, target, case, invoice and amount.
        Every release/payment against an over-threshold hold/invoice requires
        approval, including partial operations whose amounts are below the limit.
        The reducer independently verifies and consumes it in its transaction.
        After an uncertain outcome, an already consumed approval is refused:
        reconcile the financial row instead of blindly retrying. Network/HTTP
        exceptions expose the generated ``write_id`` for this reconciliation.
        """
        if not isinstance(payload, Mapping):
            raise SpacetimeConfigurationError("business reducer payload must be an object")
        document = dict(payload)
        if "write_id" not in document:
            document["write_id"] = str(uuid.uuid4())
        if not isinstance(document["write_id"], str) or not document["write_id"].strip():
            raise SpacetimeConfigurationError("write_id must be a nonempty string")
        await self._check_write(name, document)
        try:
            encoded = json.dumps(document, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise SpacetimeConfigurationError("write payload must contain finite JSON values") from None
        try:
            response = await self._request(f"call/{quote(name, safe='')}", mutation=True, json=[encoded])
        except SpacetimeError as exc:
            exc.write_id = document["write_id"]
            raise
        return ReducerReceipt(name, response.status_code, document["write_id"])

    async def call_reducer(self, name: str, *args: Any) -> ReducerReceipt:
        """Strict positional compatibility API for existing HTTP reducer callers.

        W2 business reducers take one JSON string; pass that string here or use
        ``mutate`` with an object. Positional arguments are sent unchanged, and
        this method does not invent a write_id. Policy checks use that JSON
        object. Non-object/positional administration calls remain available to
        explicit actors; their authorization is enforced by the database.
        """
        payload: dict[str, Any] = {}
        if len(args) == 1:
            if isinstance(args[0], Mapping):
                payload = dict(args[0])
            elif isinstance(args[0], str):
                try:
                    decoded = json.loads(args[0])
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict):
                    payload = decoded
        await self._check_write(name, payload)
        try:
            json.dumps(args, allow_nan=False)
        except (TypeError, ValueError):
            raise SpacetimeConfigurationError("reducer arguments must contain finite JSON values") from None
        response = await self._request(f"call/{quote(name, safe='')}", mutation=True, json=list(args))
        return ReducerReceipt(name, response.status_code, payload.get("write_id"))

    async def query(self, sql: str) -> QueryResult:
        """Run one read-only SELECT and map the documented SATS column names."""
        response = await self._request("sql", content=_read_query(sql).encode(),
                                       headers={"Content-Type": "text/plain; charset=utf-8"})
        try:
            document = response.json()
        except ValueError:
            raise SpacetimeProtocolError("SpacetimeDB SQL response is not JSON") from None
        return _query_result(document)

    async def query_rows(self, sql: str) -> list[dict[str, Any]]:
        """Return row dictionaries; values retain their wire representation."""
        return list((await self.query(sql)).rows)

    async def get_agent_config(self, agent: str) -> AgentConfig | None:
        """Read the latest UI model pin. Missing rows let the harness use defaults."""
        rows = await self.query_rows(f"SELECT * FROM agent_config WHERE agent = {_sql_string(agent)}")
        if not rows:
            return None
        row = rows[0]
        if (len(rows) != 1 or row.get("agent") != agent or not isinstance(row.get("model"), str)
                or not isinstance(row.get("enabled"), bool)):
            raise SpacetimeProtocolError("SpacetimeDB returned an invalid agent config")
        return AgentConfig(agent, row["model"], row["enabled"])

    async def get_agent_memory(self, agent: str, *, scope: Mapping[str, str] | None = None,
                               limit: int = 10) -> list[dict[str, Any]]:
        """Read active, relevant memory, preferring specific scopes and new versions.

        Every nonempty scope dimension must match the supplied task scope. A
        task without scope sees only global memory. The stored JSON ``scope``
        is decoded in returned rows; no task or shared envelope is modified.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SpacetimeConfigurationError("memory limit must be a positive integer")
        requested = dict(scope or {})
        rows = await self.query_rows(
            f"SELECT * FROM agent_memory WHERE agent = {_sql_string(agent)} AND active = true"
        )
        matches = []
        for row in rows:
            try:
                memory_scope = row["scope"]
                if isinstance(memory_scope, str):
                    memory_scope = json.loads(memory_scope)
                if not isinstance(memory_scope, dict):
                    raise TypeError
                dimensions = {key: value for key, value in memory_scope.items() if value is not None}
                if all(requested.get(key) == value for key, value in dimensions.items()):
                    version = int(row["version"])
                    matches.append((len(dimensions), version, str(row.get("id", "")),
                                    {**row, "scope": memory_scope}))
            except (KeyError, TypeError, ValueError):
                raise SpacetimeProtocolError("SpacetimeDB returned invalid agent memory") from None
        matches.sort(key=lambda match: match[:3], reverse=True)
        return [match[3] for match in matches[:limit]]

    async def get_instruction_version(self, agent: str) -> dict[str, Any] | None:
        """Read active version metadata; use history to reconstruct instruction text.

        ``diff`` alone is not a full document. W3 owns applying patches to its
        version-zero instructions and interpreting the ``reverts_version``
        anchor; see ``get_instruction_history``.
        """
        rows = await self.query_rows(
            f"SELECT * FROM instruction_version WHERE agent = {_sql_string(agent)} AND active = true"
        )
        if len(rows) > 1:
            raise SpacetimeProtocolError("SpacetimeDB returned multiple active instruction versions")
        return rows[0] if rows else None

    async def get_instruction_history(self, agent: str) -> list[dict[str, Any]]:
        """Read all versions in ascending order without applying instruction patches.

        W3 starts from its version-zero instructions. A normal row's diff is
        applied to the previous effective text. A row with ``reverts_version``
        restores the fully reconstructed text at that earlier version instead;
        its copied diff must not be applied again to the latest text. Later
        patches continue from the restored text. This preserves intervening
        history and makes reverts auditable without changing the A2A envelope.
        """
        rows = await self.query_rows(f"SELECT * FROM instruction_version WHERE agent = {_sql_string(agent)}")
        try:
            versions = [int(row["version"]) for row in rows]
            if len(set(versions)) != len(versions) or any(version < 1 for version in versions):
                raise ValueError
            return sorted(rows, key=lambda row: int(row["version"]))
        except (KeyError, TypeError, ValueError):
            raise SpacetimeProtocolError("SpacetimeDB returned invalid instruction history") from None

    async def poll_rows(self, sql: str, *, interval_s: float = 2,
                        stop: asyncio.Event | None = None) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield an initial snapshot and changed snapshots until stopped/cancelled.

        HTTP errors propagate to the owner; there is no hidden retry loop.
        Ordering alone is ignored when comparing snapshots. Unlike a websocket
        subscription, intermediate updates between polls can be missed.
        """
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise SpacetimeConfigurationError("poll interval must be positive and finite")
        previous = None
        while stop is None or not stop.is_set():
            rows = await self.query_rows(sql)
            fingerprint = sorted(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)
            if fingerprint != previous:
                previous = fingerprint
                yield rows
            if stop is None:
                await asyncio.sleep(interval_s)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_s)
                except TimeoutError:
                    pass


async def call_reducer(name: str, *args: Any) -> bool:
    """Legacy best-effort positional API: False for disabled/failed persistence.

    New code should use an explicit ``SpacetimeClient`` to choose whether a
    write failure is fatal. Environment is resolved on each legacy call.
    Neither reducer payloads, bearer tokens nor server bodies are logged.
    """
    try:
        await SpacetimeClient().call_reducer(name, *args)
    except SpacetimeDisabled:
        return False
    except SpacetimeError as exc:
        log.warning("SpacetimeDB write failed (%s)", type(exc).__name__)
        return False
    return True
