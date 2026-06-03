"""Backward-compatible entry point.

The model implementation now lives in the `lcoe/` package. This module re-exports
the full public API so existing usage keeps working unchanged:

    import datacenter_lcoe as m      # m.SOLAR, m.run_simulation, ...
    python datacenter_lcoe.py        # runs the firm US+EU suite / CLI
"""
from lcoe import *                    # noqa: F401,F403
from lcoe import (                    # private helpers used by tests
    _sys_with, _parity_year, _nearest_re, _warn_if_binding,
    _gas_plant_params, _EXPORT_FIELDS, _validate_args,
)
from dataclasses import replace       # noqa: F401
from lcoe.cli import main

if __name__ == "__main__":
    main()
