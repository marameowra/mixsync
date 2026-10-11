class TransientError(Exception):
    """Retry later."""


class PermanentError(Exception):
    """Fail the job."""


class AuthError(PermanentError):
    """A service rejected our credentials: no other candidate or retry will help."""


class SafetyError(Exception):
    """Stop and alert; never retry."""
