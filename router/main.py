"""W1 first-hop router: Redis `events` (group `router`) -> Stage 1 intake -> owning agent (PRD 7.1/7.3).
The pipeline lives in harness/ingest/ (redaction, extraction, routing, review flags, dedup). FIRST_HOP_TRANSPORT
picks the hop: `a2a` (default) sends with W3's common.a2a_transport and the router's A2A credential; `spacetime`
calls `enqueue_first_hop`, and the dispatch worker (common.review_worker) on the agents' machine delivers it.
Acknowledges Redis only after durable A2A admission or a committed enqueue.
Run either this (`make router`) or the harness server with HARNESS_CONSUME=1, never both."""
import asyncio
import logging

from harness.ingest.consumer import main
from harness.ingest.firsthop import route_event  # noqa: F401 -- deterministic first hop, kept as a public helper
from harness.ingest.routing import ROUTES  # noqa: F401 -- the 7.3 table; ingest's SpacetimeDB mirror reads it

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # health polls every 5 s
    asyncio.run(main())
