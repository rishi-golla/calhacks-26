"""W1 first-hop router: Redis `events` (group `router`) -> Stage 1 ingest -> owning agent over A2A (PRD 7.1/7.3).
The pipeline lives in harness/ingest/; this is the standalone entrypoint (`make router`). Run either this or
the harness server with HARNESS_CONSUME=1, never both."""
import asyncio
import logging

from harness.ingest.consumer import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # health polls every 5 s
    asyncio.run(main())
