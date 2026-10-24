PY := .venv/bin/python
SIM_DATA_DIR ?= data
SPACETIME_DB ?= freightops
SPACETIME_URL ?= http://127.0.0.1:3000
.PHONY: setup infra harness agents gateway router web test lint demo spacetime-setup spacetime-start spacetime-build spacetime-publish spacetime-generate spacetime-test spacetime-demo w2-check world sim ingest verify-data

setup:            ## create venv + install
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd web && npm install
infra:            ## postgres + redis
	docker compose up -d
harness:          ## W1 mock harness on :8000
	$(PY) -m harness.server
agents:           ## all 8 A2A agents on :9101-9108
	./scripts/dev_agents.sh
gateway:          ## webhooks + demo endpoints on :9000
	$(PY) -m gateway.app
router:           ## Redis -> first-hop A2A
	$(PY) -m router.main
ingest:           ## Consume Redis raw; publish routable events and extracted documents
	$(PY) -m ingest.main
world:            ## refresh real carriers (FMCSA) + shippers (SEC EDGAR) -> data/world.json
	$(PY) -m sim.world --refresh
sim:              ## publish raw simulator arrivals to Redis `raw`
	$(PY) -m sim.generator
verify-data:      ## verify generated W4 documents and event traces
	$(PY) -m sim.verify --data "$(SIM_DATA_DIR)"
web:              ## dashboard on :3001
	cd web && npm run dev -- -p 3001
test:
	$(PY) -m pytest -q
lint:
	.venv/bin/ruff check .
demo:             ## hero case smoke: AP -> Risk over real A2A
	$(PY) -m scripts.hero_smoke

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
