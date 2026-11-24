from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from .api import Client, verify_identity
from .config import read_json, write_json
from .errors import OuraError
from .http import checked_url, expect, fetch

SCOPES = "daily heartrate personal workout tag session spo2 heart_health ring_configuration stress"


def configure(profile, values, relay_url=None):
    if not isinstance(values, dict) or not all(
        isinstance(values.get(k), str) and values[k].strip() for k in ["client_id", "client_secret"]
    ):
        raise OuraError(
            "invalid_config", "Provide client_id and client_secret through a private JSON file or stdin"
        )
    allowed = {k: values[k] for k in ("client_id", "client_secret", "relay_key") if values.get(k)}
    with profile.lock("auth"):
        profile.save_secrets({**profile.secrets, **allowed})
    relay = relay_url or values.get("relay_url")
    if relay:
        profile.config["relay_url"] = checked_url(relay)
    if values.get("redirect_uri"):
        profile.config["redirect_uri"] = checked_url(values["redirect_uri"])
    if values.get("scopes"):
        profile.config["scopes"] = values["scopes"]
    profile.save_config()
    return {"ok": True, "owner": profile.owner, "configured": True}


def import_legacy(profile, env_file):
    token = None
    for line in Path(env_file).read_text().splitlines():
        if line.startswith("OURA_ACCESS_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        raise OuraError("token_missing", "OURA_ACCESS_TOKEN was not found in the source file")
    with profile.lock("auth"):
        client = Client(profile)
        identity = client.candidate_identity(token)
        uid = verify_identity(profile, identity)
        old = profile.secrets
        preserved = {k: old[k] for k in ["client_id", "client_secret", "relay_key"] if k in old}
        profile.save_secrets({**preserved, "access_token": token, "token_kind": "legacy"})
        profile.config["account_id"] = uid
        profile.save_config()
    return {"ok": True, "owner": profile.owner, "account_id": uid, "refresh_available": False}


def relay_request(profile, method, path, body=None, bearer=None):
    url = profile.config.get("relay_url")
    if not url:
        raise OuraError(
            "relay_not_configured", "Configure the public HTTPS relay URL for phone/browser login"
        )
    token = bearer if bearer is not None else profile.secrets.get("relay_key")
    if not token:
        raise OuraError("relay_not_configured", "Relay enrollment credential is missing")
    return expect(*fetch(method, checked_url(url) + path, {"Authorization": "Bearer " + token}, body))


def start(profile):
    app = profile.secrets
    if not app.get("client_id") or not app.get("client_secret"):
        raise OuraError("app_not_configured", "Configure the Oura OAuth application first")
    with profile.lock("auth"):
        previous = read_json(profile.path / "pending-auth.json")
        if previous.get("expires_at", 0) > time.time():
            return {
                "ok": True,
                "owner": profile.owner,
                "status": "pending",
                "authorize_url": previous["authorize_url"],
                "expires_at": previous["expires_at"],
                "finish_command": "oura auth finish",
            }
        state = secrets.token_urlsafe(32)
        poll = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        registered = relay_request(
            profile,
            "POST",
            "/oauth/start",
            {
                "state": state,
                "poll_hash": hashlib.sha256(poll.encode()).hexdigest(),
                "client_id": app["client_id"],
            },
        )
        redirect = profile.config.get("redirect_uri") or registered["redirect_uri"]
        if redirect != registered["redirect_uri"]:
            raise OuraError("redirect_mismatch", "CLI redirect URI differs from the relay's registered URI")
        base = checked_url(os.environ.get("OURA_AUTH_URL", "https://cloud.ouraring.com/oauth/authorize"))
        url = (
            base
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": app["client_id"],
                    "redirect_uri": redirect,
                    "state": state,
                    "scope": profile.config.get("scopes", SCOPES),
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        pending = {
            "state": state,
            "poll_secret": poll,
            "verifier": verifier,
            "redirect_uri": redirect,
            "authorize_url": url,
            "expires_at": registered["expires_at"],
        }
        write_json(profile.path / "pending-auth.json", pending)
        return {
            "ok": True,
            "owner": profile.owner,
            "status": "pending",
            "authorize_url": url,
            "expires_at": pending["expires_at"],
            "finish_command": "oura auth finish",
        }


def finish(profile):
    with profile.lock("auth"):
        path = profile.path / "pending-auth.json"
        pending = read_json(path)
        if not pending or pending.get("expires_at", 0) <= time.time():
            raise OuraError("login_required", "No active login; run oura auth start again", 3)
        if pending.get("exchange_uncertain"):
            raise OuraError(
                "login_required", "Previous exchange was uncertain; run auth cancel and start again", 3
            )
        result = relay_request(
            profile, "GET", "/oauth/poll/" + pending["state"], bearer=pending["poll_secret"]
        )
        if result.get("status") == "pending":
            return {"ok": True, "owner": profile.owner, "status": "pending"}
        if result.get("error"):
            raise OuraError("access_denied", "Oura authorization was denied; cancel and start again", 3)
        code = result.get("code")
        if not isinstance(code, str) or not code:
            raise OuraError("invalid_response", "Callback did not contain an authorization code")
        app = profile.secrets
        pending["exchange_uncertain"] = True
        write_json(path, pending)
        status, tokens = fetch(
            "POST",
            Client(profile).base + "/oauth/token",
            payload={
                "grant_type": "authorization_code",
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
                "redirect_uri": pending["redirect_uri"],
                "code": code,
                "code_verifier": pending["verifier"],
            },
            form=True,
        )
        expect(status, tokens)
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise OuraError("invalid_response", "OAuth token response was incomplete")
        identity = Client(profile).candidate_identity(tokens["access_token"])
        uid = verify_identity(profile, identity)
        updated = {
            **app,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
            "token_kind": "oauth",
        }
        updated.pop("refresh_uncertain", None)
        profile.save_secrets(updated)
        profile.config["account_id"] = uid
        profile.save_config()
        path.unlink()
        # Cleanup is best-effort after the durable grant; never repeat an exchanged OAuth code.
        try:
            relay_request(profile, "DELETE", "/oauth/poll/" + pending["state"], bearer=pending["poll_secret"])
        except OuraError:
            pass
        return {"ok": True, "owner": profile.owner, "status": "authorized", "account_id": uid}


def cancel(profile):
    with profile.lock("auth"):
        p = profile.path / "pending-auth.json"
        pending = read_json(p)
        if pending:
            try:
                relay_request(
                    profile, "DELETE", "/oauth/poll/" + pending["state"], bearer=pending["poll_secret"]
                )
            except OuraError:
                pass
            p.unlink(missing_ok=True)
    return {"ok": True, "cancelled": True, "owner": profile.owner}
