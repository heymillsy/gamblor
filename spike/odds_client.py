"""Thin wrapper around the-odds-api.com v4, with credit-budget logging.

Docs: https://the-odds-api.com/liveapi/guides/v4/

The free tier gives 500 credits/month. The /sports endpoint is free (0
credits); /odds costs (#regions x #markets) credits per call. Every response
includes x-requests-remaining / x-requests-used headers, which we surface after
each call so we always know our remaining budget.
"""

import os
import sys

import requests

BASE_URL = "https://api.the-odds-api.com/v4"

# python-dotenv is a convenience, not a requirement: if it's installed we load a
# .env sitting next to this file; otherwise an exported ODDS_API_KEY works fine.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ModuleNotFoundError:
    pass


class OddsApiError(RuntimeError):
    """Raised for non-200 responses with a human-readable explanation."""


def _api_key() -> str:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise OddsApiError(
            "ODDS_API_KEY is not set. Copy spike/.env.example to spike/.env and "
            "add your free key from https://the-odds-api.com/#get-access "
            "(or run: export ODDS_API_KEY=...)."
        )
    return key


def get(path: str, params: dict | None = None) -> object:
    """GET {BASE_URL}{path}, inject the API key, log credits, return JSON.

    Raises OddsApiError with a clear message on the common failure codes.
    """
    params = dict(params or {})
    params["apiKey"] = _api_key()

    resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)

    # Credit budget lives in the response headers on every call.
    remaining = resp.headers.get("x-requests-remaining")
    used = resp.headers.get("x-requests-used")
    last = resp.headers.get("x-requests-last")
    if remaining is not None:
        print(
            f"[credits] this_call={last} used={used} remaining={remaining}",
            file=sys.stderr,
        )

    if resp.status_code == 200:
        return resp.json()

    hints = {
        401: "Unauthorized - the API key is missing or invalid.",
        404: "Not found - check the sport key / path.",
        422: "Unprocessable - bad parameter (region, market, sport key?).",
        429: "Quota exceeded - you have used all your monthly credits.",
    }
    hint = hints.get(resp.status_code, "Unexpected error.")
    raise OddsApiError(
        f"HTTP {resp.status_code}: {hint}\nResponse body: {resp.text[:500]}"
    )
