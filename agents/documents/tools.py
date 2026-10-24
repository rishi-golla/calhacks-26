"""Document packet verification never treats a promise as a received document."""
from common.source_repository import SourceUnavailable


def check_packet(source: dict, required: list[str] | None = None) -> dict:
    required = required or source.get("required_documents", ["rate_confirmation", "pod"])
    if not isinstance(required, list) or any(not isinstance(kind, str) for kind in required):
        raise SourceUnavailable("required document kinds must be strings")
    accepted, rejected = [], []
    for document in source.get("documents", []):
        if not isinstance(document, dict):
            raise SourceUnavailable("source document must be an object")
        matching_load = document.get("load_id", source.get("load_id")) == source.get("load_id")
        if (document.get("ref") and matching_load and document.get("status", "received") == "received"
                and (document.get("kind") != "pod" or document.get("signed") is True)):
            accepted.append(document)
        else:
            rejected.append(document.get("ref", "unidentified"))
    kinds = {document.get("kind") for document in accepted}
    return {"missing": sorted(set(required) - kinds), "documents": accepted, "rejected_refs": rejected,
            "complete": set(required).issubset(kinds)}
