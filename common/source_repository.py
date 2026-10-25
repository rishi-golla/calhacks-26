"""Read-only source boundary: fixture inputs or a trusted W4-exported dataset.

SOURCE_DATA_PATH points to JSON with ``cases``, ``companies`` and ``contacts``
maps. Live execution never promotes arbitrary task payloads to source facts.
The source adapter is deliberately read-only; money changes remain W2 reducers.

``SOURCE_DATA_BACKEND=spacetime`` reads the same records from the public
``source_record`` table instead (ids ``case:<case_id>``, ``company:<id>``,
``contacts:<company_id>`` and ``dataset:meta``; see spacetime/module/CONTRACT.md).
Unset or ``file`` keeps SOURCE_DATA_PATH. Rows are cached per process for 5 s,
the dataset metadata for 30 s.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from common.envelope import TaskEnvelope
from common.spacetime_client import SpacetimeClient, SpacetimeError, _sql_string

log = logging.getLogger(__name__)
RECORD_TTL_S = 5.0
META_TTL_S = 30.0
_META_ID = "dataset:meta"
_SECTIONS = {"cases": "case", "companies": "company", "contacts": "contacts"}
_MISSING = object()
# (url, database, record id) -> (expires_at, parsed record_json). Absent rows
# and failed reads are never cached, so a record written just now is visible.
_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_CACHE_LIMIT = 1024  # expired entries are pruned once the cache reaches this size


class SourceUnavailable(ValueError):
    """Required authoritative evidence has not been supplied."""


def source_backend() -> str:
    backend = os.getenv("SOURCE_DATA_BACKEND", "").strip().lower() or "file"
    if backend not in {"file", "spacetime"}:
        raise SourceUnavailable("SOURCE_DATA_BACKEND must be file or spacetime")
    return backend


def clear_source_cache() -> None:
    _cache.clear()


def fixture_mode() -> bool:
    mode = os.getenv("FREIGHTOPS_MODE", "fixture")
    if mode not in {"fixture", "live"}:
        raise SourceUnavailable("FREIGHTOPS_MODE must be fixture or live")
    return mode == "fixture"


def source_provenance(dataset: dict, record: dict) -> dict[str, Any]:
    """Keep synthetic evidence labeled when the executor uses real providers.

    This describes the source only. Provider output has its own ``simulated``
    flag; using a real model cannot turn synthetic source records into real data.
    """
    simulated = fixture_mode() or any(
        value.get(key) is True
        for value in (dataset, record)
        for key in ("fixture", "synthetic", "simulated", "source_simulated")
    )
    return {"simulated": simulated, "source_simulated": simulated,
            "source_kind": "synthetic" if simulated else "configured_source"}


class SourceRepository:
    def __init__(self, data: dict[str, Any] | None = None, *, actor: str | None = None,
                 client: SpacetimeClient | None = None):
        # ``actor`` is policy context for SpacetimeDB reads; the table is public.
        self._injected = data
        self._actor = actor
        self._client = client

    def _uses_spacetime(self) -> bool:
        return self._injected is None and source_backend() == "spacetime"

    async def _dataset(self, section: str, key: str) -> dict[str, Any]:
        """The configured dataset, or a SpacetimeDB view holding ``section[key]`` if it exists."""
        if not self._uses_spacetime():
            return self._data()
        record = await self._record(f"{_SECTIONS[section]}:{key}")
        if record is _MISSING:
            return {section: {}}
        meta = await self._record(_META_ID)
        if meta is _MISSING:
            # Ingest's metadata carries no provenance flags; records label themselves.
            meta = {}
        if not isinstance(meta, dict):
            raise SourceUnavailable("source dataset must be a JSON object")
        return {**{name: value for name, value in meta.items() if name not in _SECTIONS}, section: {key: record}}

    async def _record(self, record_id: str) -> Any:
        try:
            if self._client is None:
                self._client = SpacetimeClient(actor=self._actor)
            client = self._client
            cache_key = (client.url, client.database, record_id)
            cached = _cache.get(cache_key)
            if cached is not None and cached[0] > time.monotonic():
                return copy.deepcopy(cached[1])
            rows = await client.query_rows(f"SELECT * FROM source_record WHERE id = {_sql_string(record_id)}")
        except SpacetimeError as exc:
            raise SourceUnavailable(f"source dataset could not be read: {exc}") from exc
        if not rows:
            return _MISSING
        if len(rows) != 1 or rows[0].get("id") != record_id or not isinstance(rows[0].get("record_json"), str):
            raise SourceUnavailable("SpacetimeDB returned an invalid source record")
        try:
            value = json.loads(rows[0]["record_json"])
        except ValueError as exc:
            raise SourceUnavailable("source record could not be parsed") from exc
        log.info("Source record %s read from SpacetimeDB", record_id)
        now = time.monotonic()
        if len(_cache) >= _CACHE_LIMIT:
            for stale in [key for key, (expires, _) in _cache.items() if expires <= now]:
                del _cache[stale]
        _cache[cache_key] = (now + (META_TTL_S if record_id == _META_ID else RECORD_TTL_S), value)
        return copy.deepcopy(value)

    def _data(self) -> dict[str, Any]:
        if self._injected is not None:
            return copy.deepcopy(self._injected)
        source = os.getenv("SOURCE_DATA_PATH")
        if not source:
            if fixture_mode():
                return {}
            raise SourceUnavailable("live execution requires SOURCE_DATA_PATH from the source-data owner")
        try:
            data = json.loads(Path(source).read_text())
        except (OSError, ValueError) as exc:
            raise SourceUnavailable("source dataset could not be read") from exc
        if not isinstance(data, dict):
            raise SourceUnavailable("source dataset must be a JSON object")
        return data

    async def load(self, env: TaskEnvelope) -> dict[str, Any]:
        data = await self._dataset("cases", env.case_id)
        cases = data.get("cases", {})
        if not isinstance(cases, dict):
            raise SourceUnavailable("source cases must be keyed by case_id")
        source = cases.get(env.case_id)
        if source is None:
            if (not fixture_mode() or os.getenv("SOURCE_DATA_PATH") or self._injected is not None
                    or self._uses_spacetime()):
                raise SourceUnavailable("case is absent from the configured source dataset")
            source = env.subject
        if not isinstance(source, dict):
            raise SourceUnavailable("case source must be an object")
        result = copy.deepcopy(source)
        if result.get("case_id", env.case_id) != env.case_id:
            raise SourceUnavailable("source case does not match the requested case")
        result["case_id"] = env.case_id
        result.update(source_provenance(data, source))
        # The signed/ingested dataset supplies these values in live mode.
        # Identifier normalization is allowed; financial defaults are not.
        carrier = result.get("carrier", {})
        if isinstance(carrier, dict) and carrier.get("id"):
            result.setdefault("carrier_id", carrier["id"])
        shipper = result.get("shipper", {})
        if isinstance(shipper, dict) and shipper.get("id"):
            result.setdefault("shipper_id", shipper["id"])
        return result

    async def get_company(self, counterparty_id: str) -> dict[str, Any]:
        data = await self._dataset("companies", counterparty_id)
        companies = data.get("companies", {})
        company = companies.get(counterparty_id) if isinstance(companies, dict) else None
        if not isinstance(company, dict) or company.get("id", counterparty_id) != counterparty_id:
            raise SourceUnavailable("counterparty is absent from the verified source directory")
        return {**copy.deepcopy(company), "id": counterparty_id, **source_provenance(data, company)}

    async def get_contact(self, counterparty_id: str) -> dict[str, Any]:
        data = await self._dataset("contacts", counterparty_id)
        contacts = data.get("contacts", {})
        candidates = contacts.get(counterparty_id, []) if isinstance(contacts, dict) else []
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list):
            raise SourceUnavailable("directory contacts must be a list")
        for contact in candidates:
            if (isinstance(contact, dict) and contact.get("company_id", counterparty_id) == counterparty_id
                    and contact.get("verified") is True and contact.get("source")):
                return {**copy.deepcopy(contact), "company_id": counterparty_id,
                        **source_provenance(data, contact)}
        company = await self.get_company(counterparty_id)
        if company.get("verified") is True and company.get("source") and company.get("phone"):
            return company
        raise SourceUnavailable("no independently verified contact is available")
