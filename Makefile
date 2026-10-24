PY := .venv/bin/python
.PHONY: setup infra harness agents gateway router web test lint demo

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
web:              ## dashboard on :3001
	cd web && npm run dev -- -p 3001
test:
	$(PY) -m pytest -q
lint:
	.venv/bin/ruff check .
demo:             ## hero case smoke: AP -> Risk over real A2A
	$(PY) -m scripts.hero_smoke
