"""Fixture module used by the acceptance tests."""


def verify_token(token):
    if not token:
        return False
    return token.startswith("ok:")


def refresh_token(token):
    if not verify_token(token):
        raise ValueError("invalid token")
    return "ok:refreshed"
