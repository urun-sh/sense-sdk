"""Shared by the demos: the clickable page for making a URUN_API_KEY (it
also walks you through creating an account if you don't have one)."""

API_KEYS_URL = "https://console.urun.sh/settings/api-keys"


def missing_api_key(extra=""):
    """The message every demo prints when URUN_API_KEY is not set."""
    hint = f"Set URUN_API_KEY — make one at {API_KEYS_URL}"
    if extra:
        hint += f", or {extra}"
    return hint + "."
