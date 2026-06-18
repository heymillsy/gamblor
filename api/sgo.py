"""Vercel serverless function: SportsGameOdds (SGO) World Cup odds + player props.

the-odds-api has no player props for TAB/World Cup, so this endpoint pulls from
SportsGameOdds, which covers World Cup player props and includes TAB.

GET /api/sgo
  Auth: requires CRON_SECRET (`Authorization: Bearer <secret>` or `?key=`).

  Query params:
    mode=league   (default) pull all World Cup events with odds; store one blob
                  per event. leagueID overridable via ?leagueID=.
    mode=leagues  discovery: list soccer leagues so we can read the World Cup
                  leagueID. Not stored, cheap.
    mode=event    one match via ?event_id=SGO_EVENT_ID (1 object, cheapest).
    bookmaker=tab surface one book in the trimmed response (raw blob keeps all).

Reads SGO_API_KEY + TURSO_* from the environment. Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

SGO_BASE_URL = "https://api.sportsgameodds.com/v2"
SGO_SPORT_ID = "SOCCER"
# Best-guess World Cup leagueID; confirm/override via ?mode=leagues then ?leagueID=.
WORLD_CUP_LEAGUE_ID = "FIFA_WORLD_CUP"
DEFAULT_BOOKMAKER = "tab"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sgo_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fetched_at TEXT NOT NULL,
  league_id  TEXT,
  scope      TEXT NOT NULL,
  event_id   TEXT,
  objects    INTEGER,
  payload    TEXT NOT NULL
)
"""


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --- SportsGameOdds --------------------------------------------------------

def sgo_get(path: str, params: dict):
    """Call the SGO API and return the parsed JSON body."""
    key = os.environ.get("SGO_API_KEY")
    if not key:
        raise AppError(500, "SGO_API_KEY is not set in Vercel env vars. "
                       "Get a free key at sportsgameodds.com.")
    url = f"{SGO_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"X-Api-Key": key, "User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        hints = {401: "Invalid SGO API key.", 403: "Forbidden (plan/quota?).",
                 404: "Not found (bad leagueID/eventID?).",
                 429: "SGO rate limit or object quota exceeded."}
        raise AppError(e.code, f"sgo: {hints.get(e.code, 'error')} {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach SportsGameOdds: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from SportsGameOdds: {e}")


def _sgo_data(body):
    """SGO wraps results in {"success": true, "data": [...]}; unwrap defensively."""
    if isinstance(body, dict):
        if body.get("success") is False:
            raise AppError(502, f"SGO API error: {body.get('error', 'unknown error')}")
        return body.get("data", body)
    return body


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
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AppError(502, f"Turso HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from Turso: {e}")
    for r in out.get("results", []):
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {r.get('error', {}).get('message')}")


def _insert_stmt(league_id, scope, event_id, payload):
    now = datetime.now(timezone.utc).isoformat()
    return (
        "INSERT INTO sgo_snapshots "
        "(fetched_at, league_id, scope, event_id, objects, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [now, league_id, scope, event_id, 1,
         json.dumps(payload, separators=(",", ":"))],
    )


# --- response trimming -----------------------------------------------------

def _event_id(ev):
    if isinstance(ev, dict):
        return ev.get("eventID") or ev.get("eventId") or ev.get("id")
    return None


def summarise_event(ev, bookmaker):
    """Pull a compact view of one book's markets (incl. player props)."""
    if not isinstance(ev, dict):
        return {"raw": ev}
    odds = ev.get("odds")
    if not isinstance(odds, (dict, list)):
        odds = {}
    markets = []
    # SGO v2: odds is a dict keyed by oddID; each has byBookmaker with prices.
    items = odds.items() if isinstance(odds, dict) else enumerate(odds)
    for odd_id, odd in items:
        if not isinstance(odd, dict):
            continue
        by_book = odd.get("byBookmaker") or {}
        book = by_book.get(bookmaker) if isinstance(by_book, dict) else None
        if not isinstance(book, dict):
            continue
        markets.append({
            "oddID": odd.get("oddID", odd_id),
            "marketName": odd.get("marketName") or odd.get("market"),
            "statID": odd.get("statID"),
            "playerID": odd.get("playerID"),
            "available": book.get("available"),
            "odds": book.get("odds"),
            "overUnder": book.get("overUnder") or odd.get("overUnder"),
        })
    return {
        "eventID": _event_id(ev),
        "leagueID": ev.get("leagueID"),
        "status": (ev.get("status") or {}).get("displayShort")
        if isinstance(ev.get("status"), dict) else ev.get("status"),
        "teams": ev.get("teams"),
        "bookmaker": bookmaker,
        "market_count": len(markets),
        "markets": markets,
    }


# --- HTTP handler ----------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)
            mode = qs.get("mode", ["league"])[0]
            bookmaker = qs.get("bookmaker", [DEFAULT_BOOKMAKER])[0]

            if mode == "leagues":
                data = _sgo_data(sgo_get("/leagues", {"sportID": SGO_SPORT_ID}))
                return self._send(200, {"ok": True, "mode": "leagues",
                                        "hint": "find the World Cup leagueID, "
                                        "then call /api/sgo?leagueID=...",
                                        "leagues": data})

            if mode == "event":
                event_id = (qs.get("event_id") or qs.get("id") or [""])[0].strip()
                if not event_id:
                    raise AppError(422, "event_id is required for mode=event.")
                body = sgo_get("/events", {"eventID": event_id,
                                           "includeAltLines": "true"})
                events = _sgo_data(body)
                if not isinstance(events, list):
                    events = [events] if events is not None else []
                events = [e for e in events if isinstance(e, dict)]
                if not events:
                    raise AppError(404, f"No SGO event for eventID={event_id}.")
                ev = events[0]
                turso([(CREATE_TABLE, []),
                       _insert_stmt(ev.get("leagueID"), "event", event_id, ev)])
                return self._send(200, {"ok": True, "mode": "event",
                                        "objects": 1, "rows_written": 1,
                                        "summary": summarise_event(ev, bookmaker),
                                        "payload": ev})

            # default: league pull
            league_id = qs.get("leagueID", [WORLD_CUP_LEAGUE_ID])[0]
            body = sgo_get("/events", {"leagueID": league_id,
                                       "oddsAvailable": "true",
                                       "includeAltLines": "true"})
            events = _sgo_data(body)
            if not isinstance(events, list):
                events = [events] if events is not None else []
            events = [e for e in events if isinstance(e, dict)]

            statements = [(CREATE_TABLE, [])]
            for ev in events:
                statements.append(
                    _insert_stmt(league_id, "event", _event_id(ev), ev))
            if len(statements) > 1:
                turso(statements)

            self._send(200, {
                "ok": True,
                "mode": "league",
                "league_id": league_id,
                "objects": len(events),
                "rows_written": len(events),
                "events": [summarise_event(ev, bookmaker) for ev in events],
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
            return  # not configured -> allow (dev); set CRON_SECRET to protect quota
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
