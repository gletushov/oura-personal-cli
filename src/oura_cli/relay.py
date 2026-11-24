from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import private_dir, read_json
from .errors import OuraError
from .http import checked_url

TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
WEBHOOK_TYPES = {
    "tag",
    "enhanced_tag",
    "workout",
    "session",
    "sleep",
    "daily_sleep",
    "daily_readiness",
    "daily_activity",
    "daily_spo2",
    "sleep_time",
    "rest_mode_period",
    "ring_configuration",
    "daily_stress",
    "daily_cardiovascular_age",
    "daily_resilience",
    "vo2_max",
}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Relay:
    def __init__(self, config_path):
        self.config = read_json(config_path)
        self.public = checked_url(self.config["public_url"])
        if len(self.config.get("registration_key", "")) < 12:
            raise OuraError("invalid_config", "Relay requires a private enrollment credential")
        self.directory = private_dir(
            self.config.get("state_dir", str(Path(config_path).parent / "relay-state"))
        )
        self.path = self.directory / "relay.sqlite3"
        if self.path.is_symlink():
            raise OuraError("unsafe_path", "Relay database must not be a symbolic link")
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (state TEXT PRIMARY KEY, poll_hash TEXT NOT NULL,
                expires REAL NOT NULL, code TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS routes (route TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                poll_hash TEXT NOT NULL,verification_token TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (route TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
                received REAL NOT NULL,acked INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(route,id));
            """)
        with self.connect() as db:
            if "enabled" not in {r[1] for r in db.execute("PRAGMA table_info(routes)")}:
                db.execute("ALTER TABLE routes ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def admin(self, bearer):
        if not bearer or not hmac.compare_digest(bearer, self.config["registration_key"]):
            raise OuraError("unauthorized", "Enrollment credential is required", 401)

    def authorize(self, db, table, key, bearer):
        field = "state" if table == "sessions" else "route"
        row = db.execute(f"SELECT * FROM {table} WHERE {field}=?", (key,)).fetchone()
        if not row or not bearer or not hmac.compare_digest(row["poll_hash"], digest(bearer)):
            raise OuraError("unauthorized", "Invalid polling credential", 401)
        if table == "sessions" and row["expires"] <= time.time():
            raise OuraError("expired", "Authorization session expired", 410)
        return row

    def handle(self, method, path, query, body, headers, raw):
        bearer = headers.get("Authorization", "").removeprefix("Bearer ")
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM events WHERE acked=1 AND received<?", (time.time() - 7 * 86400,))
            if path == "/health" and method == "GET":
                return 200, {
                    "ok": True,
                    "service": "oura-relay",
                    "oauth_ready": bool(self.config.get("client_id")),
                    "webhooks_ready": bool(self.config.get("client_secret")),
                }
            if path == "/oauth/start" and method == "POST":
                self.admin(bearer)
                if not self.config.get("client_id") or body.get("client_id") != self.config["client_id"]:
                    raise OuraError("app_not_configured", "Relay application ID is missing or differs", 400)
                state, phash = body.get("state", ""), body.get("poll_hash", "")
                if not TOKEN.fullmatch(state) or not HASH.fullmatch(phash):
                    raise OuraError("invalid_request", "Invalid session key", 400)
                existing = db.execute("SELECT * FROM sessions WHERE state=?", (state,)).fetchone()
                if existing:
                    if not hmac.compare_digest(existing["poll_hash"], phash):
                        raise OuraError("conflict", "Session is already registered", 409)
                    expiry = existing["expires"]
                else:
                    if db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1000:
                        raise OuraError("capacity", "Pending authorization capacity reached", 503)
                    expiry = time.time() + 600
                    db.execute(
                        "INSERT INTO sessions(state,poll_hash,expires) VALUES(?,?,?)", (state, phash, expiry)
                    )
                return 200, {
                    "ok": True,
                    "redirect_uri": self.public + "/oauth/callback",
                    "expires_at": expiry,
                }
            if path == "/oauth/callback" and method == "GET":
                state = query.get("state", [""])[0]
                row = db.execute("SELECT * FROM sessions WHERE state=?", (state,)).fetchone()
                if not row or row["expires"] <= time.time():
                    raise OuraError(
                        "invalid_state", "Unknown or expired authorization; start again from your CLI", 400
                    )
                if row["code"] or row["error"]:
                    raise OuraError("conflict", "This authorization response was already received", 409)
                code = query.get("code", [""])[0]
                denied = query.get("error", [""])[0]
                if not code and not denied or len(code) > 4096:
                    raise OuraError("invalid_callback", "Authorization response is incomplete", 400)
                db.execute(
                    "UPDATE sessions SET code=?,error=? WHERE state=?",
                    (code or None, "access_denied" if denied else None, state),
                )
                return 200, {
                    "ok": True,
                    "message": "Oura authorization received. Return to your agent or terminal.",
                    "status": "received",
                }
            if path.startswith("/oauth/poll/"):
                state = path.removeprefix("/oauth/poll/")
                row = self.authorize(db, "sessions", state, bearer)
                if method == "DELETE":
                    db.execute("DELETE FROM sessions WHERE state=?", (state,))
                    return 200, {"ok": True}
                if method == "GET":
                    return 200, {
                        "ok": True,
                        "status": "received" if row["code"] or row["error"] else "pending",
                        "code": row["code"],
                        "error": row["error"],
                    }
            if path == "/owners" and method == "POST":
                self.admin(bearer)
                route, phash, verify, uid = (
                    body.get(k, "") for k in ("route", "poll_hash", "verification_token", "user_id")
                )
                if (
                    not TOKEN.fullmatch(route)
                    or not HASH.fullmatch(phash)
                    or not TOKEN.fullmatch(verify)
                    or not isinstance(uid, str)
                    or not 1 <= len(uid) <= 256
                ):
                    raise OuraError("invalid_request", "Invalid webhook route registration", 400)
                row = db.execute("SELECT * FROM routes WHERE route=?", (route,)).fetchone()
                if row and (
                    row["user_id"] != uid or row["poll_hash"] != phash or row["verification_token"] != verify
                ):
                    raise OuraError(
                        "conflict", "Existing route identity and credentials cannot be replaced", 409
                    )
                db.execute(
                    "INSERT OR IGNORE INTO routes(route,user_id,poll_hash,verification_token) VALUES(?,?,?,?)",
                    (route, uid, phash, verify),
                )
                db.execute("UPDATE routes SET enabled=1 WHERE route=?", (route,))
                return 200, {"ok": True, "callback_url": self.public + "/webhooks"}
            if path.startswith("/owners/") and path.endswith("/disable") and method == "POST":
                route = path.removeprefix("/owners/").removesuffix("/disable")
                self.authorize(db, "routes", route, bearer)
                db.execute("UPDATE routes SET enabled=0 WHERE route=?", (route,))
                return 200, {"ok": True, "enabled": False}
            if path.rstrip("/") == "/webhooks" or path.startswith("/webhooks/"):
                common = path.rstrip("/") == "/webhooks"
                if common:
                    routes = db.execute("SELECT * FROM routes").fetchall()
                else:
                    route = path.removeprefix("/webhooks/")
                    row = db.execute("SELECT * FROM routes WHERE route=?", (route,)).fetchone()
                    if not row:
                        raise OuraError("not_found", "Unknown webhook route", 404)
                    routes = [row]
                if method == "GET":
                    token = query.get("verification_token", [""])[0]
                    challenge = query.get("challenge", [""])[0]
                    if not any(hmac.compare_digest(token, r["verification_token"]) for r in routes):
                        raise OuraError("unauthorized", "Invalid verification token", 401)
                    if not challenge or len(challenge) > 1024:
                        raise OuraError("invalid_request", "Invalid verification challenge", 400)
                    return 200, {"challenge": challenge}
                if method == "POST":
                    stamp = headers.get("x-oura-timestamp", "")
                    sig = headers.get("x-oura-signature", "")
                    try:
                        seconds = float(stamp)
                        if seconds > 1e12:
                            seconds /= 1000
                        valid_time = abs(time.time() - seconds) <= 300
                    except (ValueError, OverflowError):
                        valid_time = False
                    secret = self.config.get("client_secret")
                    expected = hmac.new(
                        (secret or "").encode(), stamp.encode() + raw, hashlib.sha256
                    ).hexdigest()
                    if not secret or not valid_time or not hmac.compare_digest(sig.lower(), expected):
                        raise OuraError("unauthorized", "Invalid webhook signature or timestamp", 401)
                    if not isinstance(body.get("user_id"), str) or not 1 <= len(body["user_id"]) <= 256:
                        raise OuraError("invalid_event", "Invalid user identifier", 400)
                    matching = [r for r in routes if r["user_id"] == body["user_id"]]
                    targets = [r for r in matching if r["enabled"]]
                    if not common and not matching:
                        raise OuraError("wrong_owner", "Webhook belongs to a different owner", 403)
                    if (
                        body.get("event_type") not in {"create", "update", "delete"}
                        or body.get("data_type") not in WEBHOOK_TYPES
                    ):
                        raise OuraError("invalid_event", "Unknown webhook event type", 400)
                    if not isinstance(body.get("object_id"), str) or not 1 <= len(body["object_id"]) <= 256:
                        raise OuraError("invalid_event", "Invalid document identifier", 400)
                    event = {k: body[k] for k in ["user_id", "event_type", "data_type", "object_id"]}
                    event["event_time"] = str(body.get("event_time", ""))[:128]
                    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
                    eid = hashlib.sha256(payload.encode()).hexdigest()
                    for row in targets:
                        route = row["route"]
                        if db.execute("SELECT 1 FROM events WHERE route=? AND id=?", (route, eid)).fetchone():
                            continue
                        if (
                            db.execute(
                                "SELECT COUNT(*) FROM events WHERE route=? AND acked=0", (route,)
                            ).fetchone()[0]
                            >= 10000
                        ):
                            raise OuraError("capacity", "Queue is full; provider should retry", 503)
                        db.execute("INSERT INTO events VALUES(?,?,?,?,0)", (route, eid, payload, time.time()))
                    # Application subscriptions include all consenting users. Unknown users are
                    # acknowledged without retention; never reveal whether a particular owner exists.
                    return 200, {"ok": True}
            if path.startswith("/events/"):
                parts = path.removeprefix("/events/").split("/")
                route = parts[0]
                row = self.authorize(db, "routes", route, bearer)
                if len(parts) == 1 and method == "GET":
                    if not row["enabled"]:
                        return 200, {"ok": True, "events": [], "pending": 0}
                    events = [
                        {"id": r["id"], "event": json.loads(r["payload"])}
                        for r in db.execute(
                            "SELECT id,payload FROM events WHERE route=? AND acked=0 ORDER BY received,id LIMIT 100",
                            (route,),
                        )
                    ]
                    count = db.execute(
                        "SELECT COUNT(*) FROM events WHERE route=? AND acked=0", (route,)
                    ).fetchone()[0]
                    return 200, {"ok": True, "events": events, "pending": count}
                if len(parts) == 2 and parts[1] == "ack" and method == "POST":
                    ids = body.get("ids")
                    if (
                        not isinstance(ids, list)
                        or len(ids) > 100
                        or any(not isinstance(x, str) for x in ids)
                    ):
                        raise OuraError("invalid_request", "Provide up to 100 event IDs", 400)
                    db.executemany(
                        "UPDATE events SET acked=1 WHERE route=? AND id=?", ((route, eid) for eid in ids)
                    )
                    return 200, {"ok": True}
            raise OuraError("not_found", "Route not found", 404)


def serve(config_path, host, port):
    relay = Relay(config_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "OuraRelay"

        def log_message(self, *_):
            pass  # OAuth codes and webhook route credentials must never enter access logs.

        def respond(self, status, obj):
            callback_html = self.path.startswith("/oauth/callback") and "text/html" in self.headers.get(
                "Accept", ""
            )
            if callback_html:
                title = "Authorization received" if status == 200 else "Authorization incomplete"
                message = (
                    obj.get("message")
                    if status == 200
                    else "Return to your agent or terminal and request a new sign-in link."
                )
                raw = (
                    f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                    f"<title>{title}</title><style>body{{font:20px system-ui;max-width:32em;margin:12vh auto;padding:24px;"
                    f"color-scheme:light dark}}h1{{font-size:1.6em}}</style><h1>{title}</h1><p>{message}</p></html>"
                ).encode()
            else:
                raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8" if callback_html else "application/json; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def handle_request(self):
            self.connection.settimeout(15)
            try:
                raw = b""
                if self.command in {"POST", "PUT"}:
                    try:
                        length = int(self.headers.get("Content-Length", "-1"))
                    except ValueError:
                        length = -1
                    if length < 0 or length > 65536:
                        raise OuraError(
                            "too_large", "Body length is required and must not exceed 64 KiB", 413
                        )
                    raw = self.rfile.read(length)
                try:
                    body = json.loads(raw) if raw else {}
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise OuraError("invalid_request", "Expected a JSON object", 400) from None
                url = urlsplit(self.path)
                status, obj = relay.handle(
                    self.command, url.path, parse_qs(url.query), body, self.headers, raw
                )
                self.respond(status, obj)
            except OuraError as exc:
                self.respond(
                    exc.status if 400 <= exc.status <= 599 else 400, {"ok": False, "error": exc.as_dict()}
                )
            except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
                self.respond(503, {"ok": False, "error": {"code": "temporarily_unavailable"}})

        do_GET = do_POST = do_DELETE = handle_request

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()
