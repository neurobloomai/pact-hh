"""
conftest.py
───────────
Register pact-hh's root directory as the pact_hh package in sys.modules.

pact-hh uses `from pact_hh.xxx import ...` throughout, but the repo root is
named `pact-hh` (hyphen). Python won't auto-resolve the hyphen → underscore
mapping, so we wire it here before any tests import.
"""
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Register the repo root as pact_hh so internal imports resolve
if "pact_hh" not in sys.modules:
    pkg = types.ModuleType("pact_hh")
    pkg.__path__ = [_REPO_ROOT]
    pkg.__package__ = "pact_hh"
    pkg.__file__ = os.path.join(_REPO_ROOT, "__init__.py")
    sys.modules["pact_hh"] = pkg
