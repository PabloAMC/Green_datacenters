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
	@echo "  report        build the GitHub Pages site (docs/*.html) from output/ + figs"
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

fetch-locations:  ## (re)download real ERA5 weather for all locations (2015-2025); needs a CDS key
	$(PYTHON) tools/fetch_locations.py

locations-h2:     ## per-state fig1: self-made-H2 zero-carbon, no-wind vs with-wind; needs output/era5/*.npz
	$(PYTHON) tools/build_locations_h2.py

locations-re:     ## per-state fig1: gas-backed ~55% (no wind) vs ~80% (with wind); needs output/era5/*.npz
	$(PYTHON) tools/build_locations_re.py

eu-siting:        ## rank EU sites by cheapest 24/7 carbon-free power (sun+wind / geothermal / hydro)
	$(PYTHON) tools/build_eu_siting.py

eu-siting-fetch:  ## (re)fetch ERA5 for the EU-siting RE candidates; needs a CDS key
	$(PYTHON) tools/build_eu_siting.py --fetch

scan-offshore:    ## re-price the scan's part-sea cells at offshore wind costs (needs the scan JSON)
	$(PYTHON) tools/scan_eu.py --offshore

scan-robustness:  ## weather-year stability of the scan ranking (needs the grid npz)
	$(PYTHON) tools/scan_robustness.py

tornado:          ## EU parity-gap sensitivity tornado (figure + JSON export)
	$(PYTHON) datacenter_lcoe.py --tornado --region eu

solar-only:
	$(PYTHON) tools/build_solar_only.py

zerocarbon:
	$(PYTHON) tools/build_zerocarbon.py

report:
	$(PYTHON) tools/build_report.py

check-docs:
	$(PYTHON) tools/check_doc_tables.py

check: test check-docs

clean:
	rm -f figs/cli_* output/cli_* output/sample_weather_*.npz
	rm -rf **/__pycache__ .pytest_cache
