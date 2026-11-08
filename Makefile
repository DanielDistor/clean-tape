.PHONY: setup bootstrap run check report site serve test lint fmt fixtures push-state

UV ?= uv
START ?= 2020-01-01
PORT ?= 8000

setup:            ## install pinned dependencies into .venv
	$(UV) sync

bootstrap:        ## backfill history into local ./state
	$(UV) run cleantape backfill --start $(START)

run:              ## full pipeline run (forced, evening slot: includes the EOD report)
	$(UV) run cleantape run --slot evening --force

check:
	$(UV) run cleantape check

report:
	$(UV) run cleantape report

site:
	$(UV) run cleantape build-site

serve:            ## serve the built site at http://localhost:$(PORT) (e.g. make serve PORT=8001)
	@if lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
	  echo "port $(PORT) is already in use (another server?) — try: make serve PORT=8001"; exit 1; fi
	@echo "Clean Tape → http://localhost:$(PORT)/"
	$(UV) run python -m http.server -d site $(PORT)

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

fixtures:         ## one-off: capture small real samples into tests/fixtures (network)
	$(UV) run python scripts/capture_fixtures.py

push-state:       ## force-push local ./state to the `data` branch
	bash scripts/push_state.sh
