PY ?= python

.PHONY: up install run status test drill down clean

up:            ## start PostGIS (localhost:5433)
	docker compose up -d --wait

install:       ## install the CLI and test deps
	$(PY) -m pip install -e ".[dev]"

run: up        ## the one command: promote -> refresh views -> export dossiers
	$(PY) -m iris_pilot run

status:        ## artifact freshness + latest run
	$(PY) -m iris_pilot status

test: up
	$(PY) -m pytest -q

drill: up      ## failure drill: BESS view refresh fails, peatland still delivers
	-$(PY) -m iris_pilot run --fail-stage refresh_bess_view
	-$(PY) -m iris_pilot status

down:
	docker compose down -v

clean:
	rm -rf exports runs .pytest_cache
