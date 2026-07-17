"""Garmin Connect session handling with persistent OAuth tokens."""

from __future__ import annotations

import getpass
import os

from garminconnect import Garmin

TOKENSTORE = os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect"))


class AuthError(Exception):
    pass


def login_interactive() -> Garmin:
    """Prompt for credentials (and MFA code if needed), persist tokens."""
    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password: ")
    garmin = Garmin(email=email, password=password, return_on_mfa=True)
    # login() persists tokens to the tokenstore path itself on success
    result1, result2 = garmin.login(TOKENSTORE)
    if result1 == "needs_mfa":
        code = input("MFA code (from email/authenticator): ").strip()
        garmin.resume_login(result2, code)
        garmin.client.dump(TOKENSTORE)
    print(f"Logged in as {email}; tokens saved to {TOKENSTORE}")
    return garmin


def get_client() -> Garmin:
    """Return an authenticated client from stored tokens."""
    if not os.path.isdir(TOKENSTORE):
        raise AuthError(
            f"No stored Garmin tokens in {TOKENSTORE}. Run: gwk login"
        )
    garmin = Garmin()
    try:
        garmin.login(TOKENSTORE)
    except Exception as e:
        raise AuthError(
            f"Stored tokens rejected ({e}). Re-authenticate with: gwk login"
        ) from e
    return garmin
