.PHONY: preflight/init preflight/check-vm-max-map-count build-images services-up deploy-up deploy-recreate app-recreate services-down backend-test backend-smoke backend-dev frontend-install frontend-build frontend-dev smoke

DATA_ROOT ?= ./data
ES_VM_MAX_MAP_COUNT_MIN ?= 262144
COMPOSE ?= docker compose --env-file .env
APP_SERVICES ?= backend frontend

preflight/init:
	sudo install -d -m 0755 "$(DATA_ROOT)" "$(DATA_ROOT)/postgres"
	sudo install -d -o 1000 -g 0 -m 0770 "$(DATA_ROOT)/elasticsearch"

preflight/check-vm-max-map-count:
	@current="$$(cat /proc/sys/vm/max_map_count 2>/dev/null || true)"; \
	current="$${current:-0}"; \
	if [ "$$current" -lt "$(ES_VM_MAX_MAP_COUNT_MIN)" ]; then \
		printf '%s\n' "WARNING: host vm.max_map_count=$$current is below $(ES_VM_MAX_MAP_COUNT_MIN); Elasticsearch may fail to start. To fix manually: sudo sysctl -w vm.max_map_count=$(ES_VM_MAX_MAP_COUNT_MIN)"; \
	fi

services-up: preflight/init preflight/check-vm-max-map-count
	docker compose up -d --wait postgres elasticsearch

build-images:
	sh scripts/build-images.sh

deploy-up: preflight/init preflight/check-vm-max-map-count
	sh scripts/build-images.sh
	docker compose up -d --wait

deploy-recreate: preflight/init preflight/check-vm-max-map-count
	$(COMPOSE) up -d --force-recreate --build

app-recreate: preflight/init preflight/check-vm-max-map-count
	$(COMPOSE) up -d --force-recreate --build --no-deps $(APP_SERVICES)

services-down:
	docker compose down

backend-test:
	cd backend && uv run pytest -q

backend-smoke:
	cd backend && uv run python scripts/smoke_backend.py

backend-dev:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

frontend-install:
	cd frontend && npm install

frontend-build:
	cd frontend && npm run build

frontend-dev:
	cd frontend && npm run dev -- --host 127.0.0.1

smoke:
	$(MAKE) services-up
	$(MAKE) backend-test
	$(MAKE) backend-smoke
	$(MAKE) frontend-build
