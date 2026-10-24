"""Minimal JSON Schema check for response_format validation (type, properties, required, enum, items,
additionalProperties=false). Enough to catch malformed model output; not a full validator."""
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,),
    "integer": (int,), "number": (int, float), "null": (type(None),),
}


def errors(value: Any, schema: dict, path: str = "$") -> list[str]:
    out: list[str] = []
    t = schema.get("type")
    if t is not None:
        allowed = t if isinstance(t, list) else [t]
        ok = any(isinstance(value, _TYPES.get(x, (object,))) and not (x in ("integer", "number")
                                                                     and isinstance(value, bool))
                 for x in allowed)
        if not ok:
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{path}: missing required '{k}'")
        for k, v in value.items():
            if k in props:
                out.extend(errors(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}: unexpected key '{k}'")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            out.extend(errors(v, schema["items"], f"{path}[{i}]"))
    return out
