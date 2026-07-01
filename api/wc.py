"""Vercel serverless function: World Cup 2026 fixtures + results.

GET  /api/wc                       -> stored fixtures + results (read-only, 0 cost)
GET  /api/wc?job=refresh&key=…     -> refresh the full schedule + results and
POST /api/wc?key=…                    upsert into Turso (gated by CRON_SECRET)

The daily Vercel Cron (vercel.json) hits GET /api/wc?job=refresh; Vercel sends
CRON_SECRET as a Bearer token automatically. Trigger manually in a browser with
?job=refresh&key=YOUR_CRON_SECRET.

Fixture data comes from the openfootball/worldcup.json project (public-domain
JSON on GitHub, no API key required): the complete 2026 tournament — group stage
and the full knockout bracket — including fixtures whose teams aren't decided
yet (winner placeholders like "W95" are stored NULL and shown as "TBD") and
final scores. The file is auto-generated and refreshed several times a day.
Stdlib only, no dependencies.
"""

import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

# Public-domain 2026 schedule + results (no key, no quota).
DATA_URL = ("https://raw.githubusercontent.com/openfootball/worldcup.json/"
            "master/2026/worldcup.json")

# Stop fetching once we're this many days past the final. The final date is read
# from the stored schedule (the last-scheduled match); this ISO fallback (2026
# final: 2026-07-19) is used only if nothing is stored yet.
STOP_DAYS_AFTER_FINAL = 7
FINAL_FALLBACK_ISO = "2026-07-19T19:00:00+00:00"

# "13:00 UTC-6" / "20:00 UTC-4:30" -> hour, minute, offset.
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*UTC([+-]\d{1,2})(?::?(\d{2}))?")

# status_short codes that mean a match is finished / currently in play.
FINISHED = {"FT", "AET", "PEN"}
LIVE = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}

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
  grp          TEXT,
  fetched_at   TEXT NOT NULL
)
"""

UPSERT_MATCH = (
    "INSERT OR REPLACE INTO wc_matches "
    "(fixture_id, match_date, timestamp, status_short, status_long, round, "
    " home_team, away_team, home_id, away_id, home_goals, away_goals, "
    " venue_name, venue_city, grp, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

SELECT_MATCHES = (
    "SELECT fixture_id, match_date, timestamp, status_short, status_long, "
    "round, home_team, away_team, home_goals, away_goals, venue_name, "
    "venue_city, grp, fetched_at FROM wc_matches "
    "ORDER BY timestamp IS NULL, timestamp ASC, match_date ASC"
)


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- openfootball data source ----------------------------------------------

def fetch_schedule():
    """Fetch the openfootball 2026 match list. Returns a list of match dicts."""
    req = urllib.request.Request(DATA_URL, headers={"User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AppError(502, f"openfootball HTTP {e.code} fetching the schedule.")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach the schedule source: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from the schedule source: {e}")
    matches = body.get("matches") if isinstance(body, dict) else None
    return matches if isinstance(matches, list) else []


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


def _cell(cell):
    if not isinstance(cell, dict):
        return None
    t, v = cell.get("type"), cell.get("value")
    if t == "null":
        return None
    if t == "integer":
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    if t == "float":
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return v


def turso(statements):
    """Run [(sql, [args]), ...]; return a list of row-dict lists per statement."""
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

    parsed = []
    for r in (out.get("results", []) if isinstance(out, dict) else []):
        if not isinstance(r, dict):
            parsed.append([])
            continue
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {(r.get('error') or {}).get('message')}")
        res = ((r.get("response") or {}).get("result") or {}) if isinstance(r, dict) else {}
        cols = [c.get("name") for c in res.get("cols", [])]
        parsed.append([{col: _cell(val) for col, val in zip(cols, raw)}
                       for raw in res.get("rows", [])])
    return parsed


# --- transform -------------------------------------------------------------

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _team(name):
    """Real team, or None for a placeholder (e.g. "W95", "1A") -> shown as TBD."""
    if not name or any(ch.isdigit() for ch in name):
        return None
    return name


def parse_kickoff(date_str, time_str):
    """openfootball date + "13:00 UTC-6" -> (iso8601, epoch_seconds)."""
    if not date_str:
        return None, None
    try:
        y, mo, d = (int(x) for x in str(date_str).split("-"))
    except (ValueError, TypeError):
        return None, None
    hh = mm = 0
    off = timezone.utc
    m = _TIME_RE.match(str(time_str or "").strip())
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        oh, om = int(m.group(3)), int(m.group(4) or 0)
        off = timezone(timedelta(hours=oh, minutes=(om if oh >= 0 else -om)))
    try:
        dt = datetime(y, mo, d, hh, mm, tzinfo=off)
    except ValueError:
        return None, None
    return dt.isoformat(), int(dt.timestamp())


def match_to_row(item, index, now):
    """Flatten one openfootball match into UPSERT args (index is the PK)."""
    iso, ts = parse_kickoff(item.get("date"), item.get("time"))
    ft = (item.get("score") or {}).get("ft") or []
    hg = ft[0] if len(ft) >= 2 else None
    ag = ft[1] if len(ft) >= 2 else None
    finished = isinstance(hg, int) and isinstance(ag, int)
    return [
        index,
        iso or item.get("date"),
        ts,
        "FT" if finished else "NS",
        "Match Finished" if finished else "Not Started",
        item.get("round"),
        _team(item.get("team1")),
        _team(item.get("team2")),
        None,  # home_id (unavailable)
        None,  # away_id (unavailable)
        hg if finished else None,
        ag if finished else None,
        None,  # venue_name (openfootball's "ground" is a city -> venue_city)
        item.get("ground"),
        item.get("group"),
        now,
    ]


def row_to_fixture(r):
    status = r.get("status_short")
    if status in FINISHED:
        state = "finished"
    elif status in LIVE:
        state = "live"
    else:
        state = "upcoming"
    return {
        "fixture_id": r.get("fixture_id"),
        "date": r.get("match_date"),
        "round": r.get("round"),
        "group": r.get("grp"),
        "status": status,
        "status_long": r.get("status_long"),
        "state": state,
        "home_team": r.get("home_team"),   # may be null -> TBD
        "away_team": r.get("away_team"),
        "home_goals": r.get("home_goals"),
        "away_goals": r.get("away_goals"),
        "venue": r.get("venue_name"),
        "city": r.get("venue_city"),
    }


# --- operations ------------------------------------------------------------

def read_fixtures():
    # CREATE first so a read before the first cron run returns [] cleanly.
    results = turso([(CREATE_MATCHES, []), (SELECT_MATCHES, [])])
    rows = results[1] if len(results) > 1 else []
    fixtures = [row_to_fixture(r) for r in rows]
    updated_at = max((r.get("fetched_at") or "" for r in rows), default=None) or None
    return {"ok": True, "count": len(fixtures),
            "updated_at": updated_at, "fixtures": fixtures}


def _final_datetime():
    """The final's kickoff = latest scheduled match in storage, else fallback."""
    try:
        results = turso([(CREATE_MATCHES, []),
                         ("SELECT MAX(timestamp) AS ts FROM wc_matches", [])])
        rows = results[1] if len(results) > 1 else []
        ts = rows[0].get("ts") if rows else None
        if ts is not None:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (AppError, ValueError, TypeError, OSError):
        pass  # fall back to the hard-coded date below
    return datetime.fromisoformat(FINAL_FALLBACK_ISO)


def refresh_fixtures():
    # Stop fetching once we're a week past the final.
    now = datetime.now(timezone.utc)
    final = _final_datetime()
    cutoff = final + timedelta(days=STOP_DAYS_AFTER_FINAL)
    if now > cutoff:
        return {
            "ok": True,
            "skipped": True,
            "reason": "Tournament over; refresh stopped one week after the final.",
            "final": final.isoformat(),
            "cutoff": cutoff.isoformat(),
        }

    matches = fetch_schedule()
    if not matches:
        # Don't wipe good data if the source returns an empty/unexpected payload.
        raise AppError(502, "Schedule source returned no matches; keeping existing data.")

    now_iso = now.isoformat()
    # Full replace each run so stale rows can't linger.
    stmts = [(CREATE_MATCHES, []), ("DELETE FROM wc_matches", [])]
    rows = 0
    for i, item in enumerate(matches, start=1):
        if not isinstance(item, dict):
            continue
        stmts.append((UPSERT_MATCH, match_to_row(item, i, now_iso)))
        rows += 1
    turso(stmts)
    return {"ok": True, "fetched": len(matches), "stored": rows, "updated_at": now_iso}


# --- handler ---------------------------------------------------------------

def _is_refresh(qs):
    job = (qs.get("job", [""])[0] or "").lower()
    return job in ("refresh", "update", "cron")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response, cache = 200, {}, "s-maxage=300, stale-while-revalidate=600"
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if _is_refresh(qs):
                self._authorize(qs)
                response, cache = refresh_fixtures(), "no-store"
            else:
                response = read_fixtures()
        except AppError as e:
            status, response, cache = e.status, {"ok": False, "error": e.message}, "no-store"
        except Exception as e:
            status, response, cache = 500, {"ok": False, "error": f"Unexpected: {e}"}, "no-store"
        self._send(status, response, cache)

    def do_POST(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)
            response = refresh_fixtures()
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response, "no-store")

    def _authorize(self, qs):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.lower().startswith("bearer ") else qs.get("key", [""])[0]
        if not isinstance(provided, str) or not isinstance(secret, str) \
                or not hmac.compare_digest(provided, secret):
            raise AppError(401, "Unauthorized: missing or wrong CRON_SECRET.")

    def _send(self, status, obj, cache="no-store"):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)
