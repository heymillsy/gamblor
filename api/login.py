"""Vercel function: basic username/password login.

POST /api/login   body: {"username": "...", "password": "..."}
  -> {"ok": true, "token": "<signed>", "expires_at": "<iso>"}  on success
  -> 401                                                        on bad creds

Credentials are checked against env vars (constant-time). On success we return a
signed, expiring token so the frontend can gate the fixtures view. Verify a
token later with GET /api/login?token=... (0 side effects).

Stdlib only, no dependencies.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _secret():
    """Signing key for tokens. Dedicated AUTH_SECRET, else reuse CRON_SECRET."""
    secret = os.environ.get("AUTH_SECRET") or os.environ.get("CRON_SECRET")
    if not secret:
        raise AppError(500, "AUTH_SECRET (or CRON_SECRET) is not set in Vercel env vars.")
    return secret.encode("utf-8")


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_token(username, now=None):
    now = int(now if now is not None else time.time())
    payload = {"u": username, "exp": now + TOKEN_TTL_SECONDS}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", payload["exp"]


def verify_token(token, now=None):
    """Return the payload dict if valid, else None."""
    now = int(now if now is not None else time.time())
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) < now:
        return None
    return payload


def check_credentials(username, password):
    expected_user = os.environ.get("AUTH_USERNAME")
    expected_pass = os.environ.get("AUTH_PASSWORD")
    if not expected_user or not expected_pass:
        raise AppError(500, "AUTH_USERNAME / AUTH_PASSWORD are not set in Vercel env vars.")
    # Compare both, always, to avoid short-circuit timing leaks.
    ok_user = hmac.compare_digest(username or "", expected_user)
    ok_pass = hmac.compare_digest(password or "", expected_pass)
    return ok_user and ok_pass


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        status, response = 200, {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                raise AppError(400, "Body must be JSON.")
            if not isinstance(data, dict):
                raise AppError(400, "Body must be a JSON object.")
            username = data.get("username", "")
            password = data.get("password", "")
            if not check_credentials(username, password):
                raise AppError(401, "Invalid username or password.")
            token, exp = make_token(username)
            response = {
                "ok": True,
                "token": token,
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)),
            }
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def do_GET(self):
        """Verify a token: GET /api/login?token=..."""
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = qs.get("token", [""])[0]
            payload = verify_token(token)
            if not payload:
                raise AppError(401, "Invalid or expired token.")
            response = {"ok": True, "username": payload.get("u")}
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def _send(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
