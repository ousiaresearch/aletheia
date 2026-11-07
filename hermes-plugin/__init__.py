"""Hermes plugin shim: register Weft as the active external memory provider.

Everything lives in :mod:`weft.providers.hermes`; this file exists so the plugin loader has an
entry point and so the manifest sits beside it (the catalog pins the directory, not the package).
"""

from weft.providers.hermes import WeftMemoryProvider, register  # noqa: F401  (register is the entry point)

__all__ = ["WeftMemoryProvider", "register"]
