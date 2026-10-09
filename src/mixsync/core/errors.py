class TransientError(Exception):
    """Retry later."""


class PermanentError(Exception):
    """Fail the job."""


class SafetyError(Exception):
    """Stop and alert; never retry."""
