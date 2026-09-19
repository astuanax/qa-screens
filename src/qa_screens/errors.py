class QAUserError(Exception):
    """An expected failure caused by input or environment (missing reference,
    unreachable URL, bad credentials...). Returned to the caller, never auto-reported."""


class AuthError(QAUserError):
    """Authentication failed or the profile is misconfigured."""
