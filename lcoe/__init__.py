from __future__ import annotations

"""Off-grid datacenter LCOE model (v5.5) — package API.

The implementation is split across focused modules (params, costs, weather,
dispatch, optimize, reporting, plots, simulate, analysis, cli). This re-exports
the full public surface so `from lcoe import ...` and the backward-compatible
`datacenter_lcoe` shim both see everything.
"""
from dataclasses import replace  # noqa: F401  (re-exported for convenience)

from .params import *            # noqa: F401,F403
from .costs import *             # noqa: F401,F403
from .weather import *           # noqa: F401,F403
from .dispatch import *          # noqa: F401,F403
from .optimize import *          # noqa: F401,F403
from .reporting import *         # noqa: F401,F403
from .plots import *             # noqa: F401,F403
from .h2system import *          # noqa: F401,F403
from .simulate import *          # noqa: F401,F403
from .analysis import *          # noqa: F401,F403
from .cli import *               # noqa: F401,F403

# Private helpers that are part of the (test-exercised) interface.
from .params import _sys_with            # noqa: F401
from .costs import _gas_plant_params     # noqa: F401
from .optimize import _warn_if_binding   # noqa: F401
from .reporting import _EXPORT_FIELDS    # noqa: F401
from .simulate import _nearest_re        # noqa: F401
from .analysis import _parity_year       # noqa: F401
from .cli import _validate_args, build_arg_parser, main  # noqa: F401
