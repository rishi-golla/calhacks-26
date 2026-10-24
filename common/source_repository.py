"""Read-only source boundary: fixture inputs or a trusted W4-exported dataset.

SOURCE_DATA_PATH points to JSON with ``cases``, ``companies`` and ``contacts``
maps. Live execution never promotes arbitrary task payloads to source facts.
The source adapter is deliberately read-only; money changes remain W2 reducers.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from common.envelope import TaskEnvelope


class SourceUnavailable(ValueError):
    """Required authoritative evidence has not been supplied."""


def fixture_mode() -> bool:
    mode = os.getenv("FREIGHTOPS_MODE", "fixture")
    if mode not in {"fixture", "live"}:
        raise SourceUnavailable("FREIGHTOPS_MODE must be fixture or live")
    return mode == "fixture"


class SourceRepository:
    def __init__(self, data: dict[str, Any] | None = None):
        self._injected = data

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
        data = self._data()
        cases = data.get("cases", {})
        if not isinstance(cases, dict):
            raise SourceUnavailable("source cases must be keyed by case_id")
        source = cases.get(env.case_id)
        if source is None:
            if not fixture_mode() or os.getenv("SOURCE_DATA_PATH") or self._injected is not None:
                raise SourceUnavailable("case is absent from the configured source dataset")
            source = env.subject
        if not isinstance(source, dict):
            raise SourceUnavailable("case source must be an object")
        result = copy.deepcopy(source)
        if result.get("case_id", env.case_id) != env.case_id:
            raise SourceUnavailable("source case does not match the requested case")
        result["case_id"] = env.case_id
        result["simulated"] = fixture_mode()
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
        companies = self._data().get("companies", {})
        company = companies.get(counterparty_id) if isinstance(companies, dict) else None
        if not isinstance(company, dict) or company.get("id", counterparty_id) != counterparty_id:
            raise SourceUnavailable("counterparty is absent from the verified source directory")
        return {**copy.deepcopy(company), "id": counterparty_id, "simulated": fixture_mode()}

    async def get_contact(self, counterparty_id: str) -> dict[str, Any]:
        contacts = self._data().get("contacts", {})
        candidates = contacts.get(counterparty_id, []) if isinstance(contacts, dict) else []
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list):
            raise SourceUnavailable("directory contacts must be a list")
        for contact in candidates:
            if (isinstance(contact, dict) and contact.get("company_id", counterparty_id) == counterparty_id
                    and contact.get("verified") is True and contact.get("source")):
                return {**copy.deepcopy(contact), "company_id": counterparty_id,
                        "simulated": fixture_mode()}
        company = await self.get_company(counterparty_id)
        if company.get("verified") is True and company.get("source") and company.get("phone"):
            return company
        raise SourceUnavailable("no independently verified contact is available")
