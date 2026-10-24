"""Run only capability-authenticated read-only voice lookups for an HTTPS tunnel."""
import uvicorn

from scripts.local_env import load_live_env


def main():
    load_live_env()
    uvicorn.run("gateway.voice_tools_public:app", host="127.0.0.1", port=9001, log_level="warning")


if __name__ == "__main__":
    main()
