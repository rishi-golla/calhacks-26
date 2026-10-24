"""Run the nine authenticated local agents with provisioned, private configuration.

Start the local databases first, then: .venv/bin/python -m scripts.live_agents
This submits no new work; services may resume admitted tasks and spend provider credits.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time

from scripts.local_env import ROOT, load_live_env


def main() -> None:
    load_live_env()
    # Import after configuration so advertised ports/hosts resolve correctly.
    from common.config import AGENTS
    from common.providers import configuration, mode
    if mode() != "live":
        raise ValueError("This launcher requires the provisioned live configuration")
    for agent in AGENTS:
        configuration(agent)
    directory = ROOT / "data/live/logs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    processes = {}
    logs = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for agent, spec in AGENTS.items():
            path = directory / f"{agent}.log"
            log = path.open("a")
            path.chmod(0o600)
            logs.append(log)
            processes[agent] = subprocess.Popen(
                [sys.executable, "-m", f"agents.{agent}.main"], cwd=ROOT, stdout=log, stderr=log,
            )
            print(f"Started {spec.title} on {spec.url}", flush=True)
        while not stopping:
            failed = [name for name, process in processes.items() if process.poll() is not None]
            if failed:
                raise RuntimeError(f"Agent services exited: {', '.join(failed)}. Inspect data/live/logs.")
            time.sleep(0.5)
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
