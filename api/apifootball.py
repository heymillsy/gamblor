"""Vercel serverless function: API-Football spike (thin, secret-gated proxy).

API-Football (api-sports.io) free tier (100 req/day) covers the 2026 World Cup
with odds including exotic markets (Correct Score, etc.) the-odds-api lacks.
This is a discovery proxy to confirm coverage before integrating it.

GET /api/apifootball?path=<endpoint>&<params>&key=<CRON_SECRET>
  Auth: requires CRON_SECRET (`Authorization: Bearer <secret>` or `?key=`).

  path (default "status") is one of:
    status              your plan + requests used today (free, sanity check)
    leagues             find the World Cup league id (it's 1)
    fixtures            World Cup fixtures (defaults league=1, season=2026)
    odds                ?fixture=ID  pre-match odds for one fixture (bookmakers/bets)
    odds/bets           catalog of bet types (find "Correct Score" / margin markets)
    odds/bookmakers     catalog of bookmakers

Any other query params are forwarded to API-Football. Reads APIFOOTBALL_KEY
from the environment. Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

BASE_URL = "https://v3.football.api-sports.io"
WORLD_CUP_LEAGUE = "1"
SEASON = "2026"

ALLOWED_PATHS = {
    "status", "leagues", "fixtures", "odds", "odds/bets", "odds/bookmakers",
}
RESERVED = {"path", "key"}


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _flatten(qs: dict) -> dict:
    return {k: v[0] for k, v in qs.items() if k not in RESERVED and v}


def _apply_defaults(path: str, params: dict) -> dict:
    params = dict(params)
    if path == "fixtures":
        params.setdefault("league", WORLD_CUP_LEAGUE)
        params.setdefault("season", SEASON)
    return params


def apifootball_get(path: str, params: dict):
    """Call API-Football; return (body, request_url). Surfaces API errors."""
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        raise AppError(500, "APIFOOTBALL_KEY is not set in Vercel env vars. "
                       "Get a free key at dashboard.api-football.com.")
    safe_url = f"{BASE_URL}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(
        safe_url, headers={"x-apisports-key": key, "User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        hints = {403: "Forbidden (key/plan?).", 429: "Daily request limit reached.",
                 499: "Invalid API key."}
        raise AppError(e.code, f"api-football {e.code}: {hints.get(e.code, '')} {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach API-Football: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from API-Football: {e}")
    # API-Football returns 200 with an "errors" field on problems.
    errs = body.get("errors") if isinstance(body, dict) else None
    if errs:
        raise AppError(502, f"API-Football error: {json.dumps(errs)[:300]}")
    return body, safe_url


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)
            path = (qs.get("path", ["status"])[0] or "status").strip().strip("/")
            if path not in ALLOWED_PATHS:
                raise AppError(422, f"path must be one of {sorted(ALLOWED_PATHS)}.")

            params = _apply_defaults(path, _flatten(qs))
            body, safe_url = apifootball_get(path, params)
            response = {
                "ok": True,
                "path": path,
                "request": safe_url,
                "results": body.get("results") if isinstance(body, dict) else None,
                "paging": body.get("paging") if isinstance(body, dict) else None,
                "data": body.get("response", body) if isinstance(body, dict) else body,
            }
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        try:
            self._send(status, response)
        except Exception:
            pass

    def do_POST(self):
        self.do_GET()

    def _authorize(self, qs):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.lower().startswith("bearer ") else qs.get("key", [""])[0]
        if not hmac.compare_digest(provided, secret):
            raise AppError(401, "Unauthorized: missing or wrong CRON_SECRET.")

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
