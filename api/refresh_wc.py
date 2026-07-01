"""Vercel serverless function: refresh all World Cup 2026 fixtures + results.

GET/POST /api/refresh_wc
  Auth: requires CRON_SECRET via `Authorization: Bearer <secret>` (Vercel Cron
  sends this automatically) or `?key=<secret>` for manual triggering. If
  CRON_SECRET is unset the endpoint is open (dev only).

Pulls the complete FIFA World Cup 2026 schedule from API-Football
(league=1, season=2026) — every match across the group stage and the knockout
bracket, including fixtures whose teams are not yet determined (stored as NULL /
shown as "TBD") and final/live scores for matches that have kicked off. Rows are
upserted into the Turso `wc_matches` table (created automatically).

Costs one API-Football request per run — comfortably within the free tier
(100 req/day). Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

BASE_URL = "https://v3.football.api-sports.io"
WORLD_CUP_LEAGUE = "1"
SEASON = "2026"

CREATE_MATCHES = """
CREATE TABLE IF NOT EXISTS wc_matches (
  fixture_id   INTEGER PRIMARY KEY,
  match_date   TEXT,
  timestamp    INTEGER,
  status_short TEXT,
  status_long  TEXT,
  round        TEXT,
  home_team    TEXT,
  away_team    TEXT,
  home_id      INTEGER,
  away_id      INTEGER,
  home_goals   INTEGER,
  away_goals   INTEGER,
  venue_name   TEXT,
  venue_city   TEXT,
  fetched_at   TEXT NOT NULL
)
"""

UPSERT_MATCH = (
    "INSERT OR REPLACE INTO wc_matches "
    "(fixture_id, match_date, timestamp, status_short, status_long, round, "
    " home_team, away_team, home_id, away_id, home_goals, away_goals, "
    " venue_name, venue_city, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- API-Football ----------------------------------------------------------

def apifootball_get(path, params):
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        raise AppError(500, "APIFOOTBALL_KEY is not set in Vercel env vars. "
                       "Get a free key at dashboard.api-football.com.")
    url = f"{BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"x-apisports-key": key, "User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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
    errs = body.get("errors") if isinstance(body, dict) else None
    if errs:
        raise AppError(502, f"API-Football error: {json.dumps(errs)[:300]}")
    return body


# --- Turso (libSQL HTTP pipeline) ------------------------------------------

def _turso_base():
    url = os.environ.get("TURSO_DATABASE_URL")
    if not url:
        raise AppError(500, "TURSO_DATABASE_URL is not set in Vercel env vars.")
    url = url.strip().rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    return {"type": "text", "value": str(v)}


def turso(statements):
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not token:
        raise AppError(500, "TURSO_AUTH_TOKEN is not set in Vercel env vars.")
    reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in args]}}
            for s, args in statements]
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode("utf-8")
    req = urllib.request.Request(
        f"{_turso_base()}/v2/pipeline", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AppError(502, f"Turso {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from Turso: {e}")
    for r in out.get("results", []):
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {r.get('error', {}).get('message')}")


# --- transform -------------------------------------------------------------

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def match_to_row(item, now):
    """Flatten one API-Football fixture object into UPSERT args."""
    fx = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    status = fx.get("status") or {}
    venue = fx.get("venue") or {}
    return [
        _to_int(fx.get("id")),
        fx.get("date"),
        _to_int(fx.get("timestamp")),
        status.get("short"),
        status.get("long"),
        league.get("round"),
        home.get("name"),
        away.get("name"),
        _to_int(home.get("id")),
        _to_int(away.get("id")),
        _to_int(goals.get("home")),
        _to_int(goals.get("away")),
        venue.get("name"),
        venue.get("city"),
        now,
    ]


# --- handler ---------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)

            body = apifootball_get(
                "fixtures",
                {"league": WORLD_CUP_LEAGUE, "season": SEASON},
            )
            matches = body.get("response") if isinstance(body, dict) else None
            if not isinstance(matches, list):
                matches = []

            now = datetime.now(timezone.utc).isoformat()
            stmts = [(CREATE_MATCHES, [])]
            rows = 0
            for item in matches:
                if not isinstance(item, dict):
                    continue
                args = match_to_row(item, now)
                if args[0] is None:  # no fixture id -> skip
                    continue
                stmts.append((UPSERT_MATCH, args))
                rows += 1
            turso(stmts)

            response = {
                "ok": True,
                "fetched": len(matches),
                "stored": rows,
                "updated_at": now,
            }
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

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

    def _send(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
