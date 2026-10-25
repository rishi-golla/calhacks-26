import pytest

from harness.ingest import pipeline
from harness.ingest.context import ContextStore
from harness.ingest.memory import Memory
from harness.pool import CONFIG_PATH, Pool


@pytest.fixture(autouse=True)
def isolated_intake_memory(tmp_path, monkeypatch):
    """process() called without memory/context (e.g. through the consumer) falls back to the live intake memory
    and the live Elasticsearch index on the ASUS box; tests get a private memory and no index."""
    monkeypatch.setattr(pipeline, "_memory", Memory(tmp_path / "intake-memory.sqlite"))
    monkeypatch.setattr(pipeline, "_context", ContextStore(""))


@pytest.fixture
async def dead_pool(tmp_path):
    """Models unreachable: the pipeline still routes deterministically (PRD H11)."""
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG_PATH.read_text().replace("127.0.0.1:800", "127.0.0.1:1800"))
    pool = Pool(cfg)
    await pool.check_health()
    yield pool
    await pool.aclose()
