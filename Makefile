# Off-grid Datacenter LCOE model — common tasks.
# Override the interpreter with e.g.:  make test PYTHON=.venv/bin/python
PYTHON ?= python

.PHONY: help install install-lock test reproduce tables check-docs check clean

help:
	@echo "Targets:"
	@echo "  install       install runtime deps (floors, requirements.txt)"
	@echo "  install-lock  install the exact verified versions (requirements-lock.txt)"
	@echo "  test          run the regression + unit suite"
	@echo "  reproduce     regenerate all figures + output/ JSON/CSV from scratch (firm suite)"
	@echo "  tables        print the §11 doc tables from output/ (paste-ready)"
	@echo "  check-docs    fail if the committed doc tables drift from output/"
	@echo "  check         test + check-docs (what CI runs)"
	@echo "  clean         remove generated figures, CLI outputs, caches"

install:
	$(PYTHON) -m pip install -r requirements.txt

install-lock:
	$(PYTHON) -m pip install -r requirements-lock.txt

test:
	$(PYTHON) tests/test_model.py

reproduce:
	$(PYTHON) datacenter_lcoe.py

tables:
	$(PYTHON) tools/regen_doc_tables.py

check-docs:
	$(PYTHON) tools/check_doc_tables.py

check: test check-docs

clean:
	rm -f figs/cli_* output/cli_* output/sample_weather_*.npz
	rm -rf **/__pycache__ .pytest_cache
