"""Vercel function: betting odds attached to World Cup games (per Gamblor Round).

POST /api/game_odds                        body: a scraped odds JSON (see below)
  -> upsert one row per match in the payload. Gated by the login token
     (Authorization: Bearer <token>). Returns {"ok": true, "saved": N, "rounds": [...]}.

GET  /api/game_odds                        -> lightweight list of stored games (no
                                              per-market payload) so the home page can
                                              mark which fixtures have odds. Public.
GET  /api/game_odds?round=N&match=Home vs Away
                                           -> the full stored odds (all markets) for one
                                              match, for the odds page. Public.

Odds are keyed by a durable natural key — gamblor_round + normalised team pair — because
wc_matches.fixture_id is a volatile array index that is rebuilt on every daily refresh.

Expected POST body shape (one scraped round):
  { "source", "competition", "round", "gamblor_round": 7, "scraped_at",
    "matches": [ { "match": "Spain vs Austria", "date": "2026-07-02",
                   "markets": [ { "market": "...",
                                  "selections": [ { "selection": "...", "odds": "1.33" } ] } ] } ] }

Stdlib only, no dependencies.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

CREATE_GAME_ODDS = """
CREATE TABLE IF NOT EXISTS game_odds (
  match_key     TEXT PRIMARY KEY,
  gamblor_round INTEGER,
  match_name    TEXT,
  match_date    TEXT,
  source        TEXT,
  scraped_at    TEXT,
  market_count  INTEGER,
  payload       TEXT NOT NULL,
  saved_at      TEXT NOT NULL
)
"""

UPSERT_GAME_ODDS = (
    "INSERT OR REPLACE INTO game_odds "
    "(match_key, gamblor_round, match_name, match_date, source, scraped_at, "
    " market_count, payload, saved_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# List view: everything except the (large) payload.
SELECT_LIST = (
    "SELECT match_key, gamblor_round, match_name, match_date, source, "
    "scraped_at, market_count, saved_at FROM game_odds "
    "ORDER BY gamblor_round ASC, match_name ASC"
)

SELECT_ONE = (
    "SELECT match_key, gamblor_round, match_name, match_date, source, "
    "scraped_at, market_count, payload, saved_at FROM game_odds "
    "WHERE match_key = ? LIMIT 1"
)


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- login-token auth (mirrors api/login.py) -------------------------------

def _auth_secret():
    """Signing key for tokens. Dedicated AUTH_SECRET, else reuse CRON_SECRET."""
    secret = os.environ.get("AUTH_SECRET") or os.environ.get("CRON_SECRET")
    if not secret:
        raise AppError(500, "AUTH_SECRET (or CRON_SECRET) is not set in Vercel env vars.")
    return secret.encode("utf-8")


def _b64d(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def verify_token(token, now=None):
    """Return the token payload dict if valid, else None."""
    now = int(now if now is not None else time.time())
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expected = _b64e(hmac.new(_auth_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < now:
            return None
        return payload
    except (ValueError, TypeError):
        return None


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


# --- match-key normalisation ------------------------------------------------

def _norm_team(name):
    return " ".join(str(name or "").strip().lower().split())


def match_key(gamblor_round, home, away):
    """Durable key: "<round>|<home> vs <away>", normalised. Stable across refreshes."""
    return f"{gamblor_round}|{_norm_team(home)} vs {_norm_team(away)}"


def _split_match(name):
    """"Spain vs Austria" -> ("Spain", "Austria"). Tolerant of odd spacing/case."""
    parts = re.split(r"\s+vs?\.?\s+", str(name or ""), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return str(name or "").strip(), ""


# --- operations ------------------------------------------------------------

def save_odds(body):
    """Persist every match in a scraped-round payload. Returns a summary dict."""
    if not isinstance(body, dict):
        raise AppError(422, "Body must be a JSON object.")
    matches = body.get("matches")
    if not isinstance(matches, list) or not matches:
        raise AppError(422, "Body must contain a non-empty \"matches\" array.")

    gamblor_round = body.get("gamblor_round")
    try:
        gamblor_round = int(gamblor_round) if gamblor_round is not None else None
    except (TypeError, ValueError):
        gamblor_round = None
    if gamblor_round is None:
        raise AppError(422, "Body must contain an integer \"gamblor_round\".")

    source = body.get("source")
    scraped_at = body.get("scraped_at")
    now_iso = datetime.now(timezone.utc).isoformat()

    stmts = [(CREATE_GAME_ODDS, [])]
    rounds = set()
    saved_names = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        home, away = _split_match(m.get("match"))
        if not home or not away:
            continue
        key = match_key(gamblor_round, home, away)
        markets = m.get("markets") if isinstance(m.get("markets"), list) else []
        stmts.append((UPSERT_GAME_ODDS, [
            key, gamblor_round, m.get("match"), m.get("date"),
            source, scraped_at, len(markets),
            json.dumps(m, separators=(",", ":")), now_iso,
        ]))
        rounds.add(gamblor_round)
        saved_names.append(m.get("match"))

    if len(stmts) == 1:
        raise AppError(422, "No usable matches found in the payload.")

    turso(stmts)
    return {"ok": True, "saved": len(saved_names), "matches": saved_names,
            "rounds": sorted(rounds), "saved_at": now_iso}


def list_odds():
    rows = turso([(CREATE_GAME_ODDS, []), (SELECT_LIST, [])])[1]
    return {"ok": True, "count": len(rows), "games": rows}


def get_odds(round_val, match_val):
    """Fetch one match's full odds. Accepts a full match_key or round + "Home vs Away"."""
    if match_val and "|" in match_val and round_val is None:
        key = match_val  # already a full match_key
    else:
        if round_val is None:
            raise AppError(422, "round is required.")
        home, away = _split_match(match_val)
        if not home or not away:
            raise AppError(422, "match must look like \"Home vs Away\".")
        key = match_key(round_val, home, away)

    rows = turso([(CREATE_GAME_ODDS, []), (SELECT_ONE, [key])])[1]
    if not rows:
        return {"ok": True, "found": False, "match_key": key}
    row = rows[0]
    try:
        payload = json.loads(row.get("payload") or "{}")
    except ValueError:
        payload = {}
    return {
        "ok": True, "found": True, "match_key": key,
        "gamblor_round": row.get("gamblor_round"),
        "match_name": row.get("match_name"),
        "match_date": row.get("match_date"),
        "source": row.get("source"),
        "scraped_at": row.get("scraped_at"),
        "market_count": row.get("market_count"),
        "saved_at": row.get("saved_at"),
        "markets": payload.get("markets") or [],
    }


# --- handler ---------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            match_val = qs.get("match", [""])[0].strip()
            round_raw = qs.get("round", [""])[0].strip()
            round_val = int(round_raw) if round_raw.isdigit() else None
            if match_val:
                response = get_odds(round_val, match_val)
            else:
                response = list_odds()
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def do_POST(self):
        status, response = 200, {}
        try:
            self._authorize()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                raise AppError(400, "Body must be valid JSON.")
            response = save_odds(data)
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def _authorize(self):
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token or not verify_token(token):
            raise AppError(401, "Unauthorized: sign in and try again.")

    def _send(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
