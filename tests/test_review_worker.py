import json
from types import SimpleNamespace

import pytest

from common.envelope import DelegationMeta, TaskEnvelope
from common.review_worker import ReviewWorker
from common.runtime_store import RuntimeStore, task_id_for
from common.source_repository import SourceUnavailable


class Database:
    def __init__(self, target, *, status="resolved", items=None):
        self.target = target
        self.status = status
        self.items = items or []
        self.calls = []

    async def query_rows(self, sql):
        if "FROM routing_outbox" in sql:
            return self.items
        if 'FROM "case"' in sql:
            return [{"case_id": "case", "status": self.status}]
        if 'FROM "task"' in sql:
            return [self.target]
        return []

    async def mutate(self, action, payload):
        self.calls.append((action, payload))


async def original_task(store, *, parent=None, agent="cfo", key="initial"):
    env = TaskEnvelope(task_type="cash_forecast", case_id="case", subject={"invoice_amount": 1200})
    meta = DelegationMeta(case_id="case", from_agent="router", parent_task_id=parent)
    row, _ = await store.admit(agent, "router", key, env.model_dump(), meta.model_dump(), {})
    await store.update(row["id"], status="completed")
    return {"task_id": row["id"], "case_id": "case", "agent": agent,
            "skill": env.task_type, "a2a_state": "completed", "artifact_json": "{}"}


def followup(target, identifier="followup"):
    return {"id": identifier, "kind": "review_followup", "target_agent": target["agent"],
            "case_id": "case", "task_id": target["task_id"], "status": "pending", "attempts": 0,
            "payload_json": json.dumps({"actions": ["rerun_task"], "target_task_id": target["task_id"]})}


async def test_correction_survives_worker_restart_and_only_one_per_lineage(tmp_path):
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite'}")
    target = await original_task(store)
    sent = []

    async def submit(agent, env, meta):
        row, fresh = await store.admit(agent, meta.from_agent, meta.idempotency_key,
                                      env.model_dump(), meta.model_dump(), {})
        sent.append((row["id"], fresh))
        await store.update(row["id"], status="completed")
        return SimpleNamespace(id=row["id"], context_id=env.case_id)

    db = Database(target)
    row = followup(target)
    worker = ReviewWorker(db=db, store=store, submit=submit)
    assert await worker.deliver(row) == "accepted"
    # Lost ack after acceptance: restart redelivers the same stable task, not a new run.
    worker = ReviewWorker(db=db, store=store, submit=submit)
    assert await worker.deliver(row) == "accepted"
    assert sent[0][0] == sent[1][0] and [fresh for _, fresh in sent] == [True, False]
    corrected = {**target, "task_id": sent[0][0]}
    db.target = corrected
    assert await worker.deliver(followup(corrected, "review-of-correction")) == "lineage_correction_limit"
    # Children of a corrective run consume the same lineage budget.
    nested = await original_task(store, parent=corrected["task_id"], key="nested")
    db.target = nested
    assert await worker.deliver(followup(nested, "review-of-nested")) == "lineage_correction_limit"
    assert len(sent) == 2
    assert (await store.get(target["task_id"]))["status"] == "completed"
    await store.close()


async def test_human_takeover_prevents_corrective_dispatch(tmp_path):
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite'}")
    target = await original_task(store)

    async def forbidden(*args):
        raise AssertionError("human-owned case must not dispatch")

    worker = ReviewWorker(db=Database(target, status="human_owned"), store=store, submit=forbidden)
    assert await worker.deliver(followup(target)) == "human_owned"
    await store.close()


async def test_missing_original_history_is_not_recreated_from_outbox(tmp_path):
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite'}")
    target = {"task_id": "absent", "case_id": "case", "agent": "cfo"}
    worker = ReviewWorker(db=Database(target), store=store)
    with pytest.raises(SourceUnavailable, match="history"):
        await worker.deliver(followup(target))
    await store.close()


async def test_review_delivery_acks_only_after_acceptance_and_retries_stable_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite'}")
    target = await original_task(store)
    item = {**followup(target), "kind": "review_output", "target_agent": "validator",
            "payload_json": json.dumps({"artifact": {"findings": {"made_up": True}}})}
    db = Database(target, items=[item])
    keys = []

    async def submit(agent, env, meta):
        keys.append(meta.idempotency_key)
        assert env.subject == {"target_task_id": target["task_id"]}
        if len(keys) == 1:
            raise TimeoutError("uncertain network acceptance")
        return SimpleNamespace(id=task_id_for(agent, meta.from_agent, env.case_id, meta.idempotency_key),
                               context_id=env.case_id)

    worker = ReviewWorker(db=db, store=store, submit=submit)
    assert await worker.run_once() == 0
    assert db.calls[-1][1]["success"] is False
    item["attempts"] = 1
    assert await worker.run_once() == 1
    assert db.calls[-1][1]["success"] is True
    assert keys[0] == keys[1]
    await store.close()
