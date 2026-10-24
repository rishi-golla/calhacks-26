"""Run the local voice completion poller with the private live configuration."""
import os

import uvicorn

from scripts.local_env import load_live_env


def main():
    load_live_env()
    uvicorn.run("gateway.app:app", host=os.getenv("GATEWAY_BIND_HOST", "127.0.0.1"), port=9000,
                log_level="warning")


if __name__ == "__main__":
    main()
