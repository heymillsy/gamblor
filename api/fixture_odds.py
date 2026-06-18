"""Vercel function: all odds for one World Cup fixture from DraftKings.

GET  /api/fixture_odds?event_id=ID            -> stored odds for the fixture (0 credits)
POST /api/fixture_odds?event_id=ID&key=SECRET -> retrieve all markets from DraftKings,
                                                 store, and return them (costs credits)

Stdlib only, no dependencies.
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
REGIONS = "us"
BOOKMAKER = "draftkings"

# "All" the markets we ask DraftKings for. If the-odds-api rejects one (422),
# we fall back to the confirmed-valid SAFE_MARKETS so the user always gets data.
DEFAULT_MARKETS = (
    "h2h,spreads,totals,btts,draw_no_bet,double_chance,"
    "player_goal_scorer_anytime,player_first_goal_scorer,player_last_goal_scorer,"
    "player_shots_on_target,player_assists"
)
SAFE_MARKETS = "h2h,spreads,totals,player_goal_scorer_anytime"

CREATE_ODDS = """
CREATE TABLE IF NOT EXISTS fixture_odds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id          TEXT NOT NULL,
  bookmaker         TEXT,
  regions           TEXT,
  markets           TEXT,
  credits_cost      INTEGER,
  credits_remaining INTEGER,
  fetched_at        TEXT NOT NULL,
  payload           TEXT NOT NULL
)
"""


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- the-odds-api ----------------------------------------------------------

def odds_get(path, params):
    """Return (data, credits_dict). Raises AppError(status,...) on failure."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise AppError(500, "ODDS_API_KEY is not set in Vercel env vars.")
    params = dict(params)
    params["apiKey"] = key
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            credits = {
                "remaining": _to_int(resp.headers.get("x-requests-remaining")),
                "cost": _to_int(resp.headers.get("x-requests-last")),
            }
        return data, credits
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AppError(e.code, f"odds-api {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach the odds API: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from the odds API: {e}")


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_fixture_odds(event_id, markets):
    """Fetch DraftKings odds; on a 422 (bad market) retry with SAFE_MARKETS."""
    path = f"/sports/{WORLD_CUP_KEY}/events/{urllib.parse.quote(event_id, safe='')}/odds"
    params = {"regions": REGIONS, "bookmakers": BOOKMAKER, "markets": markets,
              "oddsFormat": "decimal", "dateFormat": "iso"}
    try:
        data, credits = odds_get(path, params)
        return data, credits, markets
    except AppError as e:
        if e.status == 422 and markets != SAFE_MARKETS:
            params["markets"] = SAFE_MARKETS
            data, credits = odds_get(path, params)
            return data, credits, SAFE_MARKETS
        raise


def draftkings_markets(payload):
    """Trim an event payload to DraftKings' markets/outcomes."""
    if not isinstance(payload, dict):
        return []
    book = next((b for b in (payload.get("bookmakers") or [])
                 if b.get("key") == BOOKMAKER), None)
    if not book:
        return []
    out = []
    for m in (book.get("markets") or []):
        out.append({
            "key": m.get("key"),
            "last_update": m.get("last_update"),
            "outcomes": [
                {k: o.get(k) for k in ("name", "description", "price", "point") if k in o}
                for o in (m.get("outcomes") or [])
            ],
        })
    return out


# --- Turso -----------------------------------------------------------------

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


def _cell(c):
    t, v = c.get("type"), c.get("value")
    if t == "null":
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return v


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
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AppError(502, f"Turso {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from Turso: {e}")

    parsed = []
    for r in out.get("results", []):
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {r.get('error', {}).get('message')}")
        res = (r.get("response") or {}).get("result") or {}
        cols = [c["name"] for c in res.get("cols", [])]
        parsed.append([{col: _cell(val) for col, val in zip(cols, raw)}
                       for raw in res.get("rows", [])])
    return parsed


# --- handler ---------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            event_id = qs.get("event_id", [""])[0].strip()
            if not event_id:
                raise AppError(422, "event_id is required.")
            rows = turso([
                (CREATE_ODDS, []),
                ("SELECT * FROM fixture_odds WHERE event_id = ? "
                 "ORDER BY id DESC LIMIT 1", [event_id]),
            ])[1]
            if not rows:
                response = {"ok": True, "retrieved": False, "event_id": event_id}
            else:
                row = rows[0]
                payload = json.loads(row["payload"])
                response = {
                    "ok": True, "retrieved": True, "event_id": event_id,
                    "fetched_at": row["fetched_at"], "markets_requested": row["markets"],
                    "credits": {"cost": row["credits_cost"],
                                "remaining": row["credits_remaining"]},
                    "home_team": payload.get("home_team"),
                    "away_team": payload.get("away_team"),
                    "commence_time": payload.get("commence_time"),
                    "bookmaker": BOOKMAKER,
                    "markets": draftkings_markets(payload),
                }
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def do_POST(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)
            event_id = qs.get("event_id", [""])[0].strip()
            if not event_id:
                raise AppError(422, "event_id is required.")
            markets = qs.get("markets", [DEFAULT_MARKETS])[0]

            data, credits, used = fetch_fixture_odds(event_id, markets)
            if not isinstance(data, dict):
                raise AppError(502, "Unexpected response from the odds API.")

            now = datetime.now(timezone.utc).isoformat()
            turso([
                (CREATE_ODDS, []),
                ("INSERT INTO fixture_odds (event_id, bookmaker, regions, markets, "
                 "credits_cost, credits_remaining, fetched_at, payload) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [event_id, BOOKMAKER, REGIONS, used, credits.get("cost"),
                  credits.get("remaining"), now,
                  json.dumps(data, separators=(",", ":"))]),
            ])
            response = {
                "ok": True, "retrieved": True, "event_id": event_id,
                "fetched_at": now, "markets_requested": used, "credits": credits,
                "home_team": data.get("home_team"), "away_team": data.get("away_team"),
                "commence_time": data.get("commence_time"), "bookmaker": BOOKMAKER,
                "markets": draftkings_markets(data),
            }
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def _authorize(self, qs):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.lower().startswith("bearer ") else qs.get("key", [""])[0]
        if not hmac.compare_digest(provided, secret):
            raise AppError(401, "Unauthorized: missing or wrong access key.")

    def _send(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
