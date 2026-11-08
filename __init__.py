"""Hermes plugin shim: hand the loader Aletheia's provider.

Everything lives in :mod:`aletheia.providers.hermes`. This file is the entry point the plugin
loader imports, and it sits beside ``plugin.yaml`` because the catalog pins the directory rather
than the installed package. It imports absolutely, so it always reaches the substrate the manifest
declares as a dependency — never a copy that happens to sit in the plugin directory.
"""

from aletheia.providers.hermes import AletheiaMemoryProvider, register  # noqa: F401  (register is the entry point)

__all__ = ["AletheiaMemoryProvider", "register"]
