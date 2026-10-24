"""Reconstruct versioned instruction patches without executing generated code."""
from __future__ import annotations

import re
from pathlib import Path

from common.config import AGENTS

ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class InvalidInstructionPatch(ValueError):
    pass


def apply_instruction_patch(original: str, patch: str) -> str:
    """Apply a single-file unified diff, rejecting offsets and mismatched context."""
    lines = original.splitlines(keepends=True)
    changes = patch.splitlines(keepends=True)
    if len(patch) > 32_000 or not changes:
        raise InvalidInstructionPatch("empty or oversized instruction patch")
    if changes[0].startswith("--- "):
        if len(changes) < 3 or not changes[1].startswith("+++ "):
            raise InvalidInstructionPatch("invalid unified diff headers")
        for header in changes[:2]:
            target = header[4:].strip().split("\t")[0]
            if target not in {"instructions.md", "a/instructions.md", "b/instructions.md"}:
                raise InvalidInstructionPatch("patch may only target instructions.md")
        changes = changes[2:]
    output: list[str] = []
    position = 0
    i = 0
    while i < len(changes):
        match = HUNK.match(changes[i])
        if not match:
            raise InvalidInstructionPatch("expected unified diff hunk")
        old_start, old_count, new_start, new_count = match.groups()
        count_before = int(old_count) if old_count is not None else 1
        count_after = int(new_count) if new_count is not None else 1
        start = int(old_start) - (1 if count_before else 0)
        if start < position or start > len(lines):
            raise InvalidInstructionPatch("overlapping or out-of-range patch")
        output.extend(lines[position:start])
        position = start
        expected_start = int(new_start) - (1 if count_after else 0)
        if expected_start != len(output):
            raise InvalidInstructionPatch("new-file line offset does not match")
        read = written = 0
        i += 1
        while i < len(changes) and not changes[i].startswith("@@"):
            line = changes[i]
            if not line or line[0] not in " +-":
                raise InvalidInstructionPatch("unsupported diff line")
            if line[0] in " -":
                if position >= len(lines) or lines[position] != line[1:]:
                    raise InvalidInstructionPatch("patch context does not match instruction version")
                position += 1
                read += 1
            if line[0] in " +":
                output.append(line[1:])
                written += 1
            i += 1
        if (read, written) != (count_before, count_after):
            raise InvalidInstructionPatch("hunk length mismatch")
    output.extend(lines[position:])
    result = "".join(output)
    if not result.strip() or len(result) > 64_000:
        raise InvalidInstructionPatch("empty or oversized effective instructions")
    return result


def reconstruct_instructions(agent: str, history: list[dict]) -> tuple[str, int]:
    if agent not in AGENTS:
        raise ValueError("unknown agent")
    base = (ROOT / "agents" / agent / "instructions.md").read_text()
    snapshots = {0: base}
    effective, version = base, 0
    for row in sorted(history, key=lambda r: int(r["version"])):
        next_version = int(row["version"])
        if next_version != version + 1 or row.get("agent", agent) != agent:
            raise InvalidInstructionPatch("non-contiguous or cross-agent instruction history")
        revert = int(row.get("reverts_version") or 0)
        if not row.get("diff") and not revert:
            effective = base  # A compensating version restoring the checked-in baseline.
        elif revert:
            if revert not in snapshots:
                raise InvalidInstructionPatch("unknown revert version")
            effective = snapshots[revert]
        else:
            effective = apply_instruction_patch(effective, row["diff"])
        version = next_version
        snapshots[version] = effective
    return effective, version
