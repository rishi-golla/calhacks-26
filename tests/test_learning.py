import difflib

import pytest

from common.learning import ROOT, InvalidInstructionPatch, apply_instruction_patch, reconstruct_instructions


def patch(before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                       fromfile="instructions.md", tofile="instructions.md"))


def test_instruction_patch_reconstructs_and_revert_uses_snapshot():
    base = (ROOT / "agents/carrier_ap/instructions.md").read_text()
    first = base + "\nCheck source evidence before making a recommendation.\n"
    second = first + "Record missing receipts.\n"
    rows = [
        {"version": 1, "diff": patch(base, first)},
        {"version": 2, "diff": patch(first, second)},
        {"version": 3, "reverts_version": 1, "diff": patch(base, first)},
    ]
    assert reconstruct_instructions("carrier_ap", rows) == (first, 3)


def test_patch_cannot_touch_policy_or_apply_to_wrong_base():
    valid = patch("hello\n", "world\n")
    with pytest.raises(InvalidInstructionPatch):
        apply_instruction_patch("different\n", valid)
    with pytest.raises(InvalidInstructionPatch):
        apply_instruction_patch("hello\n", valid.replace("instructions.md", "policy.py"))


def test_instruction_history_rejects_missing_versions():
    with pytest.raises(InvalidInstructionPatch):
        reconstruct_instructions("carrier_ap", [{"version": 2, "diff": ""}])


def test_rollback_to_baseline_and_then_publish_again():
    base = (ROOT / "agents/carrier_ap/instructions.md").read_text()
    changed = base + "Check receipts.\n"
    rows = [{"version": 1, "diff": patch(base, changed)},
            {"version": 2, "diff": "", "reverts_version": 0}]
    assert reconstruct_instructions("carrier_ap", rows) == (base, 2)
    rows.append({"version": 3, "diff": patch(base, changed)})
    assert reconstruct_instructions("carrier_ap", rows) == (changed, 3)
