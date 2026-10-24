import json
import os
from email import policy
from email.parser import BytesParser
from pathlib import Path

HERO_LOADS = {"LOAD-4821", "LOAD-4877"}


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _size(root: Path) -> int:
    return sum(p.stat().st_size for folder in ("docs", "mail", "bank")
               for p in (root / folder).glob("*") if p.is_file())


def prune(data_dir, max_mb: int = int(os.getenv("SIM_DATA_MAX_MB", "500")),
          keep_loads: int = int(os.getenv("SIM_KEEP_LOADS", "2000"))) -> list[str]:
    root = Path(data_dir)
    event_log = root / "events.jsonl"
    if event_log.exists() and event_log.stat().st_size > 100 * 1024 * 1024:
        rotated = root / "events.jsonl.1"
        with rotated.open("ab") as target, event_log.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        event_log.write_bytes(b"")

    answers = _jsonl(root / "answer_key.jsonl")
    pruned_path = root / "pruned_loads.jsonl"
    pruned = {x.get("load_id") for x in _jsonl(pruned_path)}
    remaining = [a.get("load_id") for a in answers if a.get("load_id") not in pruned]
    removed: list[str] = []
    docs_dir, mail_dir = root / "docs", root / "mail"
    while (len(remaining) > keep_loads or _size(root) > max_mb * 1024 * 1024):
        candidate = next((load_id for load_id in remaining if load_id not in HERO_LOADS), None)
        if candidate is None:
            break
        records = []
        for path in docs_dir.glob("*.json"):
            try:
                doc = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if doc.get("load_id") == candidate:
                records.append(doc)
        refs = {d.get("ref") for d in records}
        attachments = {Path(d.get("pdf", f"{d.get('ref')}.pdf")).name for d in records}
        for doc in records:
            for name in (f"{doc.get('ref')}.json", doc.get("pdf", f"{doc.get('ref')}.pdf")):
                (docs_dir / name).unlink(missing_ok=True)
        for path in mail_dir.glob("*.eml"):
            try:
                message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            except OSError:
                continue
            names = {Path(p.get_filename()).name for p in message.iter_attachments() if p.get_filename()}
            if names.intersection(attachments):
                path.unlink(missing_ok=True)
        pruned.add(candidate)
        removed.append(candidate)
        remaining.remove(candidate)
        with pruned_path.open("a") as out:
            out.write(json.dumps({"load_id": candidate}) + "\n")
    return removed
