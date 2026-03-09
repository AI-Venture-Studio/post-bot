"""
exceptions.py
-------------
Shared exception classes used across poster modules and app.py.
Kept in a separate file to avoid circular imports.
"""


class AbortedError(Exception):
    """Raised when the user aborts mid-automation."""
    pass


class AccountLockedError(Exception):
    """Raised when an account is locked by another bot's active session."""
    pass
