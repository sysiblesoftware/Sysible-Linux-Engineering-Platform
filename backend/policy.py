"""Server-side password strength policy (mirrors Controller's backend/policy.py).

Enforced on every path that sets a password — setup, user create, password reset —
so a weak password can't get in regardless of what the client allows. The default
is deliberately modest (length + a couple of character classes); tighten by
raising MIN_LENGTH or requiring more classes.
"""
from __future__ import annotations

MIN_LENGTH = 10
# How many of the four classes (lower, upper, digit, symbol) must be present.
MIN_CLASSES = 2


def validate_password(password: str) -> tuple[bool, str]:
    """Return (ok, message). message is '' when ok."""
    pw = password or ""
    if len(pw) < MIN_LENGTH:
        return False, f"Password must be at least {MIN_LENGTH} characters."
    classes = sum([
        any(c.islower() for c in pw),
        any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw),
        any(not c.isalnum() for c in pw),
    ])
    if classes < MIN_CLASSES:
        return False, (f"Password must mix at least {MIN_CLASSES} of: lowercase, "
                       "uppercase, digits, symbols.")
    return True, ""
