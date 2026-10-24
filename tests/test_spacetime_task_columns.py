"""The TypeScript module exposes a2aState as a_2_a_state in HTTP SQL."""
import httpx
import pytest

from common.spacetime_client import SpacetimeClient, SpacetimeProtocolError
from scripts.spacetime_admin import LocalAdmin


def task_response(state_name="a_2_a_state"):
    return [{"schema": {"elements": [
        {"name": {"some": "task_id"}, "algebraic_type": {"String": []}},
        {"name": {"some": state_name}, "algebraic_type": {"String": []}},
        {"name": {"some": "artifact_json"}, "algebraic_type": {"String": []}},
    ]}, "rows": [["task-1", "completed", '{"outcome":"clear"}']]}]


@pytest.mark.parametrize("state_name", ["a_2_a_state", "a2a_state"])
async def test_sql_task_state_uses_canonical_python_name(state_name):
    client = SpacetimeClient(disabled=False, actor="validator", token="test-token",
                            transport=httpx.MockTransport(
                                lambda _: httpx.Response(200, json=task_response(state_name))))
    result = await client.query('SELECT * FROM "task"')
    assert result.columns == ("task_id", "a2a_state", "artifact_json")
    assert result.rows[0]["a2a_state"] == "completed"
    assert result.rows[0]["artifact_json"] == '{"outcome":"clear"}'


@pytest.mark.parametrize("state_name", ["a_2_a_state", "a2a_state"])
def test_local_admin_reads_the_same_canonical_state(state_name):
    # Avoid reading any persisted admin identity; this boundary test only performs SQL.
    admin = LocalAdmin.__new__(LocalAdmin)
    admin.database = "test-database"
    admin.client = httpx.Client(base_url="http://localhost:3000", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=task_response(state_name))))
    try:
        assert admin.rows("task") == [{"task_id": "task-1", "a2a_state": "completed",
                                       "artifact_json": '{"outcome":"clear"}'}]
    finally:
        admin.client.close()


async def test_alias_collision_is_rejected_instead_of_overwriting_state():
    response = task_response()
    response[0]["schema"]["elements"].append({"name": {"some": "a2a_state"}})
    response[0]["rows"][0].append("failed")
    client = SpacetimeClient(disabled=False, actor="validator", token="test-token",
                            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    with pytest.raises(SpacetimeProtocolError):
        await client.query_rows('SELECT * FROM "task"')
