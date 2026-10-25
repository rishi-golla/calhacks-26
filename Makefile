PY := .venv/bin/python
SIM_DATA_DIR ?= data
SPACETIME_DB ?= freightops
SPACETIME_URL ?= http://127.0.0.1:3000
EVALUATOR_INTERVAL_S ?= 30
# Service targets load ENV_FILE (default .env); variables already in your shell win.
# test, lint, demo and the live-* launchers never use it, so tests stay offline.
ENV_FILE ?= .env
export ENV_FILE
RUN := $(PY) -m scripts.with_env --
.PHONY: setup infra harness agents gateway router web test lint demo spacetime-setup spacetime-start spacetime-build spacetime-publish spacetime-generate spacetime-test spacetime-demo w2-check world sim ingest source verify-data review-worker evaluator cloud-verify live-agents live-demo live-gateway

setup:            ## create venv + install
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd web && npm install
infra:            ## postgres + redis
	docker compose up -d
harness:          ## W1 mock harness on :8000
	$(RUN) $(PY) -m harness.server
agents:           ## all 9 A2A agents on :9101-9109
	$(RUN) ./scripts/dev_agents.sh
gateway:          ## webhooks + demo endpoints on :9000
	$(RUN) $(PY) -m gateway.app
router:           ## Redis -> first hop (A2A, or the SpacetimeDB outbox with FIRST_HOP_TRANSPORT=spacetime)
	$(RUN) $(PY) -m router.main
ingest:           ## Consume Redis raw; publish routable events and extracted documents
	$(RUN) $(PY) -m ingest.main
source:           ## re-export the agent source dataset from data/ingest.db (ingest keeps it current)
	$(RUN) $(PY) -m ingest.source_export
world:            ## refresh real carriers (FMCSA) + shippers (SEC EDGAR) -> data/world.json
	$(RUN) $(PY) -m sim.world --refresh
sim:              ## publish raw simulator arrivals to Redis `raw`
	$(RUN) $(PY) -m sim.generator
verify-data:      ## verify generated W4 documents and event traces
	$(PY) -m sim.verify --data "$(SIM_DATA_DIR)"
review-worker:    ## dispatch worker: outbox first hops -> agents, W2 reviews -> Settlement Quality Review
	$(RUN) $(PY) -m common.review_worker
evaluator:        ## score agents against the sim answer key -> SpacetimeDB `metric` (run on the sim's machine)
	$(RUN) $(PY) -m evaluator.main --loop $(EVALUATOR_INTERVAL_S)
cloud-verify:     ## read-only cutover checklist on Maincloud; VERIFY_ARGS="--server URL --database DB --readyz"
	$(PY) -m scripts.cloud_verify $(VERIFY_ARGS)
web:              ## dashboard on :3001
	cd web && npm run dev -- -p 3001
test:
	$(PY) -m pytest -q
lint:
	.venv/bin/ruff check .
demo:             ## fixture case across the real nine-service A2A mesh
	$(PY) -m scripts.hero_smoke
live-agents:      ## nine authenticated services using the private local live profile
	$(PY) -m scripts.live_agents
live-demo:        ## one bounded live concurrent workload with synthetic source data
	$(PY) -m scripts.live_concurrency_demo
live-gateway:     ## local Cartesia result polling with private service credentials
	$(PY) -m scripts.live_gateway

spacetime-setup:   ## install the pinned module + dashboard dependencies
	cd spacetime/module/spacetimedb && npm ci
	cd web && npm ci
spacetime-start:   ## persistent local server (separate terminal)
	spacetime start --listen-addr 127.0.0.1:3000 --data-dir data/spacetime --non-interactive
spacetime-build:
	spacetime build --module-path spacetime/module/spacetimedb
spacetime-publish: ## safe local publish + private service credentials
	$(PY) -m scripts.spacetime_admin --server $(SPACETIME_URL) --database $(SPACETIME_DB) publish
spacetime-generate:
	spacetime generate --lang typescript --module-path spacetime/module/spacetimedb --out-dir web/module_bindings --yes
spacetime-test:    ## real reducers against a fresh local test database
	$(PY) -m scripts.spacetime_smoke --server $(SPACETIME_URL)
spacetime-demo:    ## explicitly seed labeled W2 demonstration data
	$(PY) -m scripts.spacetime_demo --server $(SPACETIME_URL) --database $(SPACETIME_DB)
w2-check:          ## Python, module, and production dashboard checks
	$(MAKE) lint test
	cd spacetime/module/spacetimedb && npm test
	$(MAKE) spacetime-build
	cd web && npm run lint && npm test && npm run build
