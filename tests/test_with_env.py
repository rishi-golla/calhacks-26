"""Service targets load ENV_FILE without letting it override the caller or reach the tests."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRINT = [sys.executable, "-c", "import os; print(os.getenv('W4_A', '-'), os.getenv('W4_B', '-'))"]


def run(env_file: Path, **extra: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("W4_")}
    env.update(ENV_FILE=str(env_file), **extra)
    return subprocess.run([sys.executable, "-m", "scripts.with_env", "--", *PRINT], cwd=ROOT, env=env,
                          check=True, capture_output=True, text=True).stdout.split()


def test_env_file_fills_unset_variables_and_the_caller_wins(tmp_path):
    env_file = tmp_path / "team.env"
    env_file.write_text("W4_A='from file'   # comment\nW4_B='$(touch never-run)'\n")
    assert run(env_file) == ["from", "file", "$(touch", "never-run)"]
    assert run(env_file, W4_A="caller") == ["caller", "$(touch", "never-run)"]
    assert not (ROOT / "never-run").exists()


def test_missing_env_file_is_not_an_error(tmp_path):
    assert run(tmp_path / "absent.env") == ["-", "-"]


def test_test_and_lint_targets_do_not_load_the_env_file():
    makefile = (ROOT / "Makefile").read_text()
    for target in ("test", "lint", "demo"):
        recipe = makefile.split(f"\n{target}:", 1)[1].split("\n", 2)[1]
        assert "$(RUN)" not in recipe
