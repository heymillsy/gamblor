"""Vercel serverless function: deep extra-markets sweep for ONE match.

GET /api/match?event_id=ID
  Auth: requires CRON_SECRET (`Authorization: Bearer <secret>` or `?key=`),
  since it spends the-odds-api credits.

  Query params:
    event_id=ID   (required) the-odds-api event id. Aliases: id, match_id.
                  Find ids in /api/worldcup or /api/latest (each match's "id").
    markets=...    (optional) override the extra-market list.
    regions=au     (optional) bookmaker region (au includes TAB).

Fetches the per-match event-odds (all the extra markets the bookmaker offers),
stores the raw JSON as a blob row in Turso (scope='event'), and returns it.
Cost = #markets x #regions credits. Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

BASE_URL = "https://api.the-odds-api.com/v4"
WORLD_CUP_KEY = "soccer_fifa_world_cup"
DEEP_MARKETS = "btts,double_chance,draw_no_bet,team_totals,alternate_totals,alternate_spreads"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fetched_at        TEXT NOT NULL,
  sport             TEXT NOT NULL,
  regions           TEXT NOT NULL,
  markets           TEXT NOT NULL,
  scope             TEXT NOT NULL,
  event_id          TEXT,
  credits_remaining INTEGER,
  credits_cost      INTEGER,
  payload           TEXT NOT NULL
)
"""


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --- the-odds-api ----------------------------------------------------------

def get_json(path: str, params: dict):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise AppError(500, "ODDS_API_KEY is not set in Vercel env vars.")
    params = dict(params)
    params["apiKey"] = key
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            credits = {
                "remaining": _to_int(resp.headers.get("x-requests-remaining")),
                "cost": _to_int(resp.headers.get("x-requests-last")),
            }
        return json.loads(body), credits
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        hints = {401: "Invalid API key.", 404: "Unknown event_id or sport.",
                 422: "Bad parameter.", 429: "Quota exceeded."}
        raise AppError(e.code, f"odds-api: {hints.get(e.code, 'error')} {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach the odds API: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from the odds API: {e}")


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- Turso (libSQL HTTP pipeline) ------------------------------------------

def _turso_base() -> str:
    url = os.environ.get("TURSO_DATABASE_URL")
    if not url:
        raise AppError(500, "TURSO_DATABASE_URL is not set in Vercel env vars.")
    url = url.strip().rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url


def _arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def turso(statements: list):
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not token:
        raise AppError(500, "TURSO_AUTH_TOKEN is not set in Vercel env vars.")
    requests = [
        {"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}
        for sql, args in statements
    ]
    requests.append({"type": "close"})
    body = json.dumps({"requests": requests}).encode("utf-8")
    req = urllib.request.Request(
        f"{_turso_base()}/v2/pipeline",
        data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AppError(502, f"Turso HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")
    for r in out.get("results", []):
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {r.get('error', {}).get('message')}")


def store_event(regions, markets, event_id, payload, credits):
    now = datetime.now(timezone.utc).isoformat()
    turso([
        (CREATE_TABLE, []),
        ("INSERT INTO odds_snapshots "
         "(fetched_at, sport, regions, markets, scope, event_id, "
         " credits_remaining, credits_cost, payload) "
         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
         [now, WORLD_CUP_KEY, regions, markets, "event", event_id,
          credits.get("remaining"), credits.get("cost"),
          json.dumps(payload, separators=(",", ":"))]),
    ])


# --- HTTP handler ----------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)

            event_id = (qs.get("event_id") or qs.get("id")
                        or qs.get("match_id") or [""])[0].strip()
            if not event_id:
                raise AppError(422, "event_id is required (e.g. "
                               "?event_id=ID&key=SECRET). Find ids in "
                               "/api/worldcup or /api/latest.")
            regions = qs.get("regions", ["au"])[0]
            markets = qs.get("markets", [DEEP_MARKETS])[0]

            data, credits = get_json(
                f"/sports/{WORLD_CUP_KEY}/events/{event_id}/odds",
                {"regions": regions, "markets": markets,
                 "oddsFormat": "decimal", "dateFormat": "iso"},
            )
            store_event(regions, markets, event_id, data, credits)

            books = (data or {}).get("bookmakers") or []
            self._send(200, {
                "ok": True,
                "event_id": event_id,
                "home_team": (data or {}).get("home_team"),
                "away_team": (data or {}).get("away_team"),
                "commence_time": (data or {}).get("commence_time"),
                "regions": regions,
                "markets_requested": markets,
                "bookmakers_returned": [b.get("key") for b in books],
                "rows_written": 1,
                "credits": credits,
                "payload": data,
            })
        except AppError as e:
            self._send(e.status, {"ok": False, "error": e.message})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"Unexpected: {e}"})

    def do_POST(self):
        self.do_GET()

    def _authorize(self, qs):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return  # not configured -> allow (dev); set CRON_SECRET to protect credits
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else qs.get("key", [""])[0]
        if not hmac.compare_digest(provided, secret):
            raise AppError(401, "Unauthorized: missing or wrong CRON_SECRET.")

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
