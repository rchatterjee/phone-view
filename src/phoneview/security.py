"""Who may use PhoneView: a per-run access token, a session cookie, and an origin allow-list.

PhoneView always listens on 127.0.0.1. Remote viewing goes through a reverse proxy
on the same machine (`tailscale serve`), which provides TLS and decides which
tailnet devices can connect at all. Inside that, the per-run token decides which
browser may watch and control the phone:

  1. Open  /?token=<token>  (printed at startup, or handed over by ISDi).
  2. A matching token creates a random session id, stored in an HttpOnly,
     SameSite=Strict cookie; the token is removed from the URL by a redirect.
  3. Every page request and the WebSocket handshake must carry that cookie.

The token comes from the PHONEVIEW_TOKEN environment variable (never a command-line
argument, which other users could read from the process list) or is generated.
Sessions live in memory, so restarting PhoneView signs everyone out.
"""
import hmac
import ipaddress
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

COOKIE = "phoneview_session"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
MAX_SESSIONS = 32

FORBIDDEN_PAGE = """<!doctype html>
<meta charset="utf-8"><title>PhoneView - access denied</title>
<h1>Access denied</h1>
<p>Open PhoneView with the link printed where it was started (it ends with
<code>?token=...</code>), or with the PhoneView button in ISDi.</p>
"""


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalise_origin(url):
    """'https://Laptop.tail1.ts.net:8443/' -> ('https://laptop.tail1.ts.net:8443', 'laptop.tail1.ts.net')."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Not an http(s) origin: {url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"An origin has no path or query: {url!r}")
    host = parts.hostname.lower()
    default = 443 if parts.scheme == "https" else 80
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{host}{port}", host


class Access:
    def __init__(self, token, port, public_origins=()):
        if not token or len(token) < 16:
            raise ValueError("The access token must be at least 16 characters")
        self.token = token
        self.sessions = {}            # session id -> creation time
        public = [normalise_origin(o) for o in public_origins]
        self.public_hosts = [host for _, host in public]
        self.allowed_hosts = list(LOOPBACK_NAMES) + self.public_hosts
        self.allowed_origins = {f"http://{name}:{port}" for name in LOOPBACK_NAMES}
        self.allowed_origins.update(origin for origin, _ in public)
        self.https_hosts = {host for origin, host in public if origin.startswith("https:")}

    @classmethod
    def generate_token(cls):
        return secrets.token_urlsafe(32)

    def token_matches(self, value):
        return bool(value) and hmac.compare_digest(str(value), self.token)

    def new_session(self):
        if len(self.sessions) >= MAX_SESSIONS:   # drop the oldest
            oldest = min(self.sessions, key=self.sessions.get)
            del self.sessions[oldest]
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = time.time()
        return sid

    def valid_session(self, sid):
        return bool(sid) and any(hmac.compare_digest(sid, known) for known in self.sessions)

    def origin_allowed(self, origin):
        try:
            normalised, _ = normalise_origin(origin)
        except ValueError:
            return False
        return normalised in self.allowed_origins

    def cookie_secure(self, host_header):
        """Mark the cookie Secure when the page was reached over the HTTPS proxy."""
        host = (host_header or "").rsplit(":", 1)[0].lower()
        return host in self.https_hosts


def viewer_identity(headers, peer):
    """Who is watching: the tailnet login that `tailscale serve` adds, else 'local browser'.

    The header is only trusted from a loopback peer (the proxy itself). A local
    process could forge it, but such a process can reach the phone through adb or
    usbmuxd anyway; this is for the audit trail, not for access control.
    """
    if not is_loopback(peer):
        return None
    login = headers.get("tailscale-user-login")
    if login:
        name = headers.get("tailscale-user-name")
        return f"{name} <{login}>" if name else login
    return "local browser"


class AuditLog:
    """Append-only JSON lines: who viewed which device, and when."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None

    def write(self, event, **fields):
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        log.info("audit: %s", json.dumps(record))
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
            except OSError as exc:
                log.warning("Could not write the audit log %s: %s", self.path, exc)
