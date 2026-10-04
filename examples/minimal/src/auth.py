"""Tiny fixture for the Engineering Study Kit smoke test."""


def verify_token(token):
    if not token:
        return False
    return token.startswith("ok:")


def refresh_token(token):
    if not verify_token(token):
        raise ValueError("invalid token")
    return "ok:refreshed"
