"""
exceptions.py
-------------
Shared exception classes used across poster modules and app.py.
Kept in a separate file to avoid circular imports.
"""


class AbortedError(Exception):
    """Raised when the user aborts mid-automation."""
    pass
