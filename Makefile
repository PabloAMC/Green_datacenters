# Off-grid Datacenter LCOE model — common tasks.
# Override the interpreter with e.g.:  make test PYTHON=.venv/bin/python
PYTHON ?= python

.PHONY: help install install-lock test reproduce tables report check-docs check clean

help:
	@echo "Targets:"
	@echo "  install       install runtime deps (floors, requirements.txt)"
	@echo "  install-lock  install the exact verified versions (requirements-lock.txt)"
	@echo "  test          run the regression + unit suite"
	@echo "  reproduce     regenerate all figures + output/ JSON/CSV from scratch (firm suite)"
	@echo "  tables        print the §11 doc tables from output/ (paste-ready)"
	@echo "  locations     compute the cross-location comparison figure (EU countries / US states)"
	@echo "  report        build the GitHub Pages site (docs/index.html) from output/ + figs"
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

locations:
	$(PYTHON) tools/build_locations.py

locations-real:   ## needs output/era5/*.npz (tools/fetch_era5.py) + a CDS key
	$(PYTHON) tools/build_locations.py --real

solar-only:
	$(PYTHON) tools/build_solar_only.py

report:
	$(PYTHON) tools/build_report.py

check-docs:
	$(PYTHON) tools/check_doc_tables.py

check: test check-docs

clean:
	rm -f figs/cli_* output/cli_* output/sample_weather_*.npz
	rm -rf **/__pycache__ .pytest_cache
