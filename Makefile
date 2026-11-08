.PHONY: setup bootstrap run check report site serve test lint fmt fixtures push-state

UV ?= uv
START ?= 2020-01-01

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

serve:            ## serve the built site at http://localhost:8000
	$(UV) run python -m http.server -d site 8000

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
