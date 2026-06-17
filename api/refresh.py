"""Vercel serverless function: fetch TAB World Cup odds and store as blobs.

GET /api/refresh
  Auth: requires CRON_SECRET via `Authorization: Bearer <secret>` (Vercel Cron
  sends this automatically) or `?key=<secret>` for manual triggering.

  Query params:
    mode=featured        (default) one /odds call -> h2h+spreads+totals for all
                         matches; 3 credits.
    mode=deep            per-match extra markets for imminent games via the
                         event-odds endpoint; credit-capped.
    markets=...          override the market list for the chosen mode.
    window_hours=48      (deep) only matches kicking off within this window.
    max_credits=50       (deep) hard cap on credits spent this run.

Stores the raw the-odds-api JSON as a blob row in Turso (table auto-created).
Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

BASE_URL = "https://api.the-odds-api.com/v4"
WORLD_CUP_KEY = "soccer_fifa_world_cup"

FEATURED_MARKETS = "h2h,spreads,totals"
# Curated soccer extras; first deep run doubles as discovery of what TAB offers.
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
    """Call the-odds-api and return (data, credits_dict)."""
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
        hints = {401: "Invalid API key.", 422: "Bad parameter.", 429: "Quota exceeded."}
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
    """Encode a Python value as a libSQL pipeline argument."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def turso(statements: list) -> list:
    """Run [(sql, [args...]), ...] in one pipeline; return per-statement results."""
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
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
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
    results = out.get("results", [])
    for r in results:
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {r.get('error', {}).get('message')}")
    return results


INSERT_SQL = (
    "INSERT INTO odds_snapshots "
    "(fetched_at, sport, regions, markets, scope, event_id, "
    " credits_remaining, credits_cost, payload) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _insert_stmt(scope, regions, markets, payload, credits, event_id=None):
    """Build a (sql, args) tuple for one snapshot insert."""
    now = datetime.now(timezone.utc).isoformat()
    return (INSERT_SQL, [
        now, WORLD_CUP_KEY, regions, markets, scope, event_id,
        credits.get("remaining"), credits.get("cost"),
        json.dumps(payload, separators=(",", ":")),
    ])


def store_snapshot(scope, regions, markets, payload, credits, event_id=None):
    turso([_insert_stmt(scope, regions, markets, payload, credits, event_id)])


# --- refresh modes ---------------------------------------------------------

def refresh_featured(regions, markets):
    data, credits = get_json(
        f"/sports/{WORLD_CUP_KEY}/odds",
        {"regions": regions, "markets": markets,
         "oddsFormat": "decimal", "dateFormat": "iso"},
    )
    store_snapshot("featured", regions, markets, data, credits)
    return {
        "mode": "featured",
        "markets": markets,
        "rows_written": 1,
        "match_count": len(data),
        "credits": credits,
    }


def refresh_deep(regions, markets, window_hours, max_credits):
    cost_per_event = len([m for m in markets.split(",") if m]) * len(
        [r for r in regions.split(",") if r]
    )
    if cost_per_event == 0:
        raise AppError(422, "No markets/regions specified for deep mode.")

    # Free events list (0 credits) -> pick imminent matches.
    events, _ = get_json(f"/sports/{WORLD_CUP_KEY}/events", {"dateFormat": "iso"})
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=window_hours)
    upcoming = []
    for e in events:
        ct = _parse_iso(e.get("commence_time"))
        if ct and now <= ct <= cutoff:
            upcoming.append(e)
    upcoming.sort(key=lambda e: e.get("commence_time") or "")

    max_events = max_credits // cost_per_event
    if max_events == 0:
        raise AppError(
            422,
            f"max_credits ({max_credits}) is below the per-event cost "
            f"({cost_per_event}). Raise max_credits or reduce markets/regions.",
        )
    max_events = min(max_events, 15)  # bound serverless run time
    selected = upcoming[:max_events]

    # Fetch sequentially, then write all rows in a single Turso pipeline to
    # minimise round-trips and serverless timeout risk.
    rows, spent, last_remaining = 0, 0, None
    statements = []
    for e in selected:
        data, credits = get_json(
            f"/sports/{WORLD_CUP_KEY}/events/{e['id']}/odds",
            {"regions": regions, "markets": markets,
             "oddsFormat": "decimal", "dateFormat": "iso"},
        )
        statements.append(
            _insert_stmt("event", regions, markets, data, credits, event_id=e["id"])
        )
        rows += 1
        cost = credits.get("cost")
        spent += cost if cost is not None else cost_per_event
        last_remaining = credits.get("remaining")

    if statements:
        turso(statements)

    return {
        "mode": "deep",
        "markets": markets,
        "window_hours": window_hours,
        "cost_per_event": cost_per_event,
        "max_credits": max_credits,
        "events_upcoming": len(upcoming),
        "events_fetched": rows,
        "rows_written": rows,
        "credits": {"cost": spent, "remaining": last_remaining},
    }


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- HTTP handler ----------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)

            mode = qs.get("mode", ["featured"])[0]
            regions = qs.get("regions", ["au"])[0]
            turso([(CREATE_TABLE, [])])  # idempotent; needed by both modes

            if mode == "deep":
                markets = qs.get("markets", [DEEP_MARKETS])[0]
                try:
                    window_hours = int(qs.get("window_hours", ["48"])[0])
                    max_credits = int(qs.get("max_credits", ["50"])[0])
                    if window_hours < 1 or max_credits < 1:
                        raise ValueError
                except ValueError:
                    raise AppError(
                        422,
                        "window_hours and max_credits must be positive integers.",
                    )
                summary = refresh_deep(regions, markets, window_hours, max_credits)
            else:
                markets = qs.get("markets", [FEATURED_MARKETS])[0]
                summary = refresh_featured(regions, markets)

            self._send(200, {"ok": True, **summary})
        except AppError as e:
            self._send(e.status, {"ok": False, "error": e.message})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"Unexpected: {e}"})

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

    def do_POST(self):  # allow POST too (some schedulers use it)
        self.do_GET()
