"""Shared model-store exception (kept in its own module so manager.py and diskspace.py can both use it without an import cycle)."""


class IntegrityError(RuntimeError):
    pass
