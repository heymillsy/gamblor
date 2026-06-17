"""Vercel serverless function: FIFA World Cup odds.

GET /api/worldcup
  Query params (all optional):
    regions=au            the-odds-api region (au = Australian books incl. TAB)
    markets=h2h           comma-separated markets (h2h, totals, spreads, ...)
    bookmaker=tab         filter to one book; use "all" for every book
    format=simple         "simple" (trimmed) or "raw" (full upstream payload)

Returns JSON. Reads ODDS_API_KEY from the environment (set it in Vercel ->
Project -> Settings -> Environment Variables). Stdlib only, no dependencies.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

BASE_URL = "https://api.the-odds-api.com/v4"
WORLD_CUP_KEY = "soccer_fifa_world_cup"


class OddsApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def get_json(path: str, params: dict):
    """Call the-odds-api and return (data, credit_headers)."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise OddsApiError(
            500,
            "ODDS_API_KEY is not set. Add it in Vercel -> Project -> Settings "
            "-> Environment Variables, then redeploy.",
        )
    params = dict(params)
    params["apiKey"] = key
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            credits = {
                "remaining": resp.headers.get("x-requests-remaining"),
                "used": resp.headers.get("x-requests-used"),
                "last_cost": resp.headers.get("x-requests-last"),
            }
        return json.loads(body), credits
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        hints = {
            401: "Invalid or missing API key.",
            422: "Bad parameter (region/market/sport key?).",
            429: "Monthly credit quota exceeded.",
        }
        raise OddsApiError(e.code, f"{hints.get(e.code, 'Upstream error')} {detail}")
    except urllib.error.URLError as e:
        raise OddsApiError(502, f"Could not reach the odds API: {e.reason}")
    except ValueError as e:
        raise OddsApiError(502, f"Invalid JSON from the odds API: {e}")


def simplify_match(match: dict, bookmaker: str | None) -> dict:
    """Trim a match to the fields we care about, optionally one bookmaker."""
    books = match.get("bookmakers") or []
    if bookmaker:
        books = [b for b in books if b.get("key") == bookmaker]
    return {
        "id": match.get("id"),
        "commence_time": match.get("commence_time"),
        "home_team": match.get("home_team"),
        "away_team": match.get("away_team"),
        "bookmakers": [
            {
                "key": b.get("key"),
                "title": b.get("title"),
                "last_update": b.get("last_update"),
                "markets": [
                    {
                        "key": m.get("key"),
                        "outcomes": [
                            {k: o.get(k) for k in ("name", "price", "point") if k in o}
                            for o in (m.get("outcomes") or [])
                        ],
                    }
                    for m in (b.get("markets") or [])
                ],
            }
            for b in books
        ],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            regions = qs.get("regions", ["au"])[0]
            markets = qs.get("markets", ["h2h"])[0]
            bookmaker = qs.get("bookmaker", ["tab"])[0]
            fmt = qs.get("format", ["simple"])[0]

            data, credits = get_json(
                f"/sports/{WORLD_CUP_KEY}/odds",
                {
                    "regions": regions,
                    "markets": markets,
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
            )

            if fmt == "raw":
                matches = data
            else:
                bm = None if bookmaker in ("", "all") else bookmaker
                matches = [simplify_match(m, bm) for m in data]

            self._send(
                200,
                {
                    "meta": {
                        "sport": WORLD_CUP_KEY,
                        "regions": regions,
                        "markets": markets,
                        "bookmaker": bookmaker,
                        "format": fmt,
                        "match_count": len(data),
                        "credits": credits,
                    },
                    "matches": matches,
                },
            )
        except OddsApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:  # never leak a stack trace to the caller
            self._send(500, {"error": f"Unexpected error: {e}"})

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=60, stale-while-revalidate=120")
        self.end_headers()
        self.wfile.write(body)
