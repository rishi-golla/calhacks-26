"""Run a service command with ENV_FILE (default .env) loaded, without executing it as shell code.

    python -m scripts.with_env -- python -m router.main

Variables already set in the caller's environment win, so `SIM_DATA_DIR=/tmp/x make ingest` still
works. A missing env file is not an error. Only the Makefile's service targets use this; `make test`
and `make lint` never do, so a live profile in .env cannot reach the test suite.
"""
import os
import sys
from pathlib import Path

from scripts.local_env import ROOT, read_env


def main(argv: list[str]) -> None:
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise SystemExit("usage: python -m scripts.with_env -- <command> [args...]")
    env_file = Path(os.getenv("ENV_FILE", ".env"))
    env_file = env_file if env_file.is_absolute() else ROOT / env_file
    if env_file.is_file():
        for name, value in read_env(env_file).items():
            os.environ.setdefault(name, value)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
