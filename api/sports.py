"""Vercel serverless function: list available sports/competitions.

GET /api/sports
  Query params (optional):
    soccer=true   only return soccer_* competitions

Free to call (the-odds-api /sports costs 0 credits). Confirms the
soccer_fifa_world_cup key is live. Stdlib only, no dependencies.
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
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        hints = {401: "Invalid or missing API key.", 429: "Quota exceeded."}
        raise OddsApiError(e.code, f"{hints.get(e.code, 'Upstream error')} {detail}")
    except urllib.error.URLError as e:
        raise OddsApiError(502, f"Could not reach the odds API: {e.reason}")
    except ValueError as e:
        raise OddsApiError(502, f"Invalid JSON from the odds API: {e}")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            soccer_only = qs.get("soccer", ["false"])[0].lower() in ("1", "true", "yes")

            sports = get_json("/sports", {})
            if soccer_only:
                sports = [s for s in sports if str(s.get("key", "")).startswith("soccer_")]

            world_cup = next(
                (s for s in sports if s.get("key") == WORLD_CUP_KEY), None
            )
            self._send(
                200,
                {
                    "meta": {
                        "count": len(sports),
                        "world_cup_active": bool(world_cup and world_cup.get("active")),
                    },
                    "sports": sports,
                },
            )
        except OddsApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:
            self._send(500, {"error": f"Unexpected error: {e}"})

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=600")
        self.end_headers()
        self.wfile.write(body)
