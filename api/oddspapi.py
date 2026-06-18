"""Vercel serverless function: OddsPapi spike (thin, secret-gated proxy).

OddsPapi's free tier (250 req/month) advertises World Cup odds *with player
props* across 300+ books — including TAB. We don't yet know the exact World Cup
tournament id / TAB bookmaker key / goalscorer market id, so this is a thin
discovery proxy: pass an OddsPapi endpoint + params and get the raw JSON back.
No storage yet (spike first; add storage once the shape is known).

GET /api/oddspapi?path=<endpoint>&<params>&key=<CRON_SECRET>
  Auth: requires CRON_SECRET (`Authorization: Bearer <secret>` or `?key=`),
  so random hits can't burn the 250 free monthly requests.

  path (default "sports") is one of:
    sports                reference list of sports (soccer = sportId 10)
    fixtures              schedule; needs sportId + dateFrom/dateTo (<=10 days).
                          Defaults applied: sportId=10, dateFrom=today,
                          dateTo=today+10. Find the World Cup tournamentId here.
    odds                  ?fixtureId=...  full prices for one fixture.
                          Defaults: oddsFormat=decimal, verbosity=3 (incl props).
    odds-by-tournaments   ?tournamentIds=...&bookmaker=tab
    bookmakers            reference list (find TAB's key)
    markets               reference list (find e.g. anytime-goalscorer market id)
    historical-odds       historical prices

Any other query params are forwarded to OddsPapi as-is. Reads ODDSPAPI_API_KEY
from the environment. Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

ODDSPAPI_BASE_URL = "https://api.oddspapi.io/v4"
SOCCER_SPORT_ID = "10"

ALLOWED_PATHS = {
    "sports", "fixtures", "odds", "odds-by-tournaments",
    "bookmakers", "markets", "historical-odds",
}
# Query params we consume here and must NOT forward upstream.
RESERVED = {"path", "key"}


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _today(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _flatten(qs: dict) -> dict:
    """parse_qs gives lists; take the first value of each, dropping reserved keys."""
    return {k: v[0] for k, v in qs.items() if k not in RESERVED and v}


def _apply_defaults(path: str, params: dict) -> dict:
    """Helpful defaults so discovery is one click on a phone."""
    params = dict(params)
    if path == "fixtures":
        params.setdefault("sportId", SOCCER_SPORT_ID)
        params.setdefault("from", _today(0))
        params.setdefault("to", _today(10))
    elif path in ("odds", "odds-by-tournaments", "historical-odds"):
        params.setdefault("oddsFormat", "decimal")
        params.setdefault("verbosity", "3")
    return params


def oddspapi_get(path: str, params: dict):
    """Call OddsPapi and return (data, requests_remaining, request_url_without_key)."""
    key = os.environ.get("ODDSPAPI_API_KEY")
    if not key:
        raise AppError(500, "ODDSPAPI_API_KEY is not set in Vercel env vars. "
                       "Get a free key at oddspapi.io.")
    safe_url = f"{ODDSPAPI_BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
    params = dict(params)
    params["apiKey"] = key
    url = f"{ODDSPAPI_BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            remaining = (resp.headers.get("x-ratelimit-remaining")
                         or resp.headers.get("x-requests-remaining"))
        return json.loads(body), remaining, safe_url
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        hints = {401: "Invalid API key.", 403: "Forbidden (plan/quota?).",
                 404: "Not found.", 422: "Bad parameter.",
                 429: "Free-tier request quota exceeded."}
        raise AppError(e.code, f"oddspapi: {hints.get(e.code, 'error')} {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach OddsPapi: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from OddsPapi: {e}")


# --- fixtures convenience (client-side narrowing of the big soccer list) ---

def _fixtures_list(data):
    """Defensively pull the list of fixture dicts out of the response."""
    if isinstance(data, list):
        return [f for f in data if isinstance(f, dict)]
    if isinstance(data, dict):
        for k in ("fixtures", "data", "results", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return [f for f in v if isinstance(f, dict)]
    return []


def _tournament_summary(fixtures):
    """Collapse fixtures into distinct tournaments so the World Cup id is easy to spot."""
    seen = {}
    for f in fixtures:
        tid = f.get("tournamentId")
        key = tid if tid is not None else f.get("tournamentName")
        row = seen.setdefault(key, {
            "tournamentId": tid,
            "tournamentName": f.get("tournamentName"),
            "categoryName": f.get("categoryName"),
            "fixtures": 0,
            "with_odds": 0,
        })
        row["fixtures"] += 1
        if f.get("hasOdds"):
            row["with_odds"] += 1
    return sorted(seen.values(),
                  key=lambda t: (str(t.get("categoryName") or ""),
                                 str(t.get("tournamentName") or "")))


def _filter_tournament(fixtures, substr):
    """Keep fixtures whose tournament/category text contains substr (case-insensitive)."""
    s = substr.lower()
    fields = ("tournamentName", "tournamentSlug", "categoryName", "categorySlug")
    return [f for f in fixtures
            if s in " ".join(str(f.get(k, "")) for k in fields).lower()]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Build the response first, then send once — so a broken pipe during
        # send can't trigger a second _send (and a secondary exception).
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)

            path = (qs.get("path", ["sports"])[0] or "sports").strip().strip("/")
            if path not in ALLOWED_PATHS:
                raise AppError(422, f"path must be one of {sorted(ALLOWED_PATHS)}.")

            flat = _flatten(qs)
            # local-only conveniences for fixtures; not forwarded upstream
            summary = flat.pop("summary", None)
            tournament = flat.pop("tournament", None)

            params = _apply_defaults(path, flat)
            data, remaining, safe_url = oddspapi_get(path, params)

            response = {"ok": True, "path": path, "request": safe_url,
                        "requests_remaining": remaining}
            if path == "fixtures" and (summary or tournament):
                fixtures = _fixtures_list(data)
                if tournament:
                    fixtures = _filter_tournament(fixtures, tournament)
                if summary == "tournaments":
                    response["tournaments"] = _tournament_summary(fixtures)
                else:
                    response["count"] = len(fixtures)
                    response["fixtures"] = fixtures
            else:
                response["data"] = data
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}

        try:
            self._send(status, response)
        except Exception:
            pass  # client disconnected / broken pipe — nothing more to do

    def do_POST(self):
        self.do_GET()

    def _authorize(self, qs):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return  # not configured -> allow (dev); set CRON_SECRET to protect quota
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
