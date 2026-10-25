import pytest

from harness.pool import CONFIG_PATH, Pool


@pytest.fixture
async def dead_pool(tmp_path):
    """Models unreachable: the pipeline still routes deterministically (PRD H11)."""
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG_PATH.read_text().replace("127.0.0.1:800", "127.0.0.1:1800"))
    pool = Pool(cfg)
    await pool.check_health()
    yield pool
    await pool.aclose()
