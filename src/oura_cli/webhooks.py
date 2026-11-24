from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from .api import Client, endpoint_name
from .auth import relay_request
from .config import read_json
from .errors import OuraError
from .relay import WEBHOOK_TYPES
from .store import Store, now_iso

DEFAULT_TYPES = ["sleep", "daily_sleep", "daily_readiness", "daily_activity", "daily_stress"]


def _creds(profile):
    config, secure = profile.config, profile.secrets
    route, poll = config.get("webhook_route"), secure.get("webhook_poll_secret")
    if not route or not poll:
        raise OuraError("webhooks_not_configured", "Enable webhook subscriptions for this owner first")
    return route, poll


def _remote(profile, method, suffix="", body=None):
    route, poll = _creds(profile)
    return relay_request(profile, method, "/events/" + route + suffix, body, bearer=poll)


def _subscriptions(profile):
    value = Client(profile).request("GET", "/v2/webhook/subscription", app_auth=True)
    if not isinstance(value, list):
        raise OuraError("invalid_response", "Expected provider subscription list")
    return value


def maintain(profile):
    with profile.lock("webhooks"):
        profile.config = read_json(profile.path / "profile.json")
        return _maintain(profile)


def _maintain(profile):
    if profile.config.get("webhook_enabled") is False:
        return {"ok": True, "owner": profile.owner, "enabled": False, "changes": []}
    _creds(profile)
    callback = profile.config["relay_url"] + "/webhooks"
    existing = _subscriptions(profile)
    wanted = profile.config.get("webhook_types", DEFAULT_TYPES)
    changes = []
    client = Client(profile)
    for dtype in wanted:
        for operation in ["create", "update", "delete"]:
            found = next(
                (
                    s
                    for s in existing
                    if s.get("callback_url") == callback
                    and s.get("event_type") == operation
                    and s.get("data_type") == dtype
                ),
                None,
            )
            if found:
                try:
                    expires = datetime.fromisoformat(
                        found["expiration_time"].replace("Z", "+00:00")
                    ).timestamp()
                except (KeyError, ValueError, TypeError):
                    expires = 0
                if expires < datetime.now(UTC).timestamp() + 21600:
                    # Documented renew route differs from subscription update.
                    client.request("PUT", "/v2/webhook/subscription/renew/" + found["id"], app_auth=True)
                    changes.append({"action": "renewed", "type": dtype, "event": operation})
            else:
                created = client.request(
                    "POST",
                    "/v2/webhook/subscription",
                    payload={
                        "callback_url": callback,
                        "verification_token": profile.secrets["webhook_verification_token"],
                        "event_type": operation,
                        "data_type": dtype,
                    },
                    app_auth=True,
                )
                if not isinstance(created, dict) or not created.get("id"):
                    raise OuraError(
                        "invalid_response",
                        "Subscription creation response has no ID; check status before retrying",
                    )
                changes.append({"action": "created", "type": dtype, "event": operation})
    profile.config["webhook_maintained_at"] = now_iso()
    profile.save_config()
    return {"ok": True, "owner": profile.owner, "changes": changes}


def enable(profile, endpoints=None):
    client = Client(profile)
    uid = client.identity()
    names = [
        "vo2_max" if endpoint_name(x) == "vO2_max" else endpoint_name(x) for x in (endpoints or DEFAULT_TYPES)
    ]
    if any(x not in WEBHOOK_TYPES for x in names):
        raise OuraError(
            "unsupported_webhook", "This data type is not available through Oura webhooks; use sync"
        )
    with profile.lock("webhooks"):
        profile.config = read_json(profile.path / "profile.json")
        profile.config["webhook_enabled"] = True
        secure = profile.secrets
        route = profile.config.get("webhook_route") or secrets.token_urlsafe(32)
        poll = secure.get("webhook_poll_secret") or secrets.token_urlsafe(32)
        verify = secure.get("webhook_verification_token") or secrets.token_urlsafe(32)
        # Persist before the network write so uncertain enrollment remains recoverable/idempotent.
        profile.config["webhook_route"] = route
        profile.config["webhook_types"] = list(
            dict.fromkeys([*profile.config.get("webhook_types", []), *names])
        )
        profile.save_secrets({**secure, "webhook_poll_secret": poll, "webhook_verification_token": verify})
        profile.save_config()
        relay_request(
            profile,
            "POST",
            "/owners",
            {
                "route": route,
                "user_id": uid,
                "poll_hash": hashlib.sha256(poll.encode()).hexdigest(),
                "verification_token": verify,
            },
        )
        result = _maintain(profile)
        result["callback_url"] = profile.config["relay_url"] + "/webhooks"
        return result


def status(profile):
    if not profile.config.get("webhook_route"):
        return {"ok": True, "owner": profile.owner, "configured": False, "pending": None}
    remote = _remote(profile, "GET")
    callback = profile.config["relay_url"] + "/webhooks"
    return {
        "ok": True,
        "owner": profile.owner,
        "configured": True,
        "enabled": profile.config.get("webhook_enabled", True),
        "pending": remote["pending"],
        "types": profile.config.get("webhook_types", []),
        "subscriptions": [
            {k: v for k, v in s.items() if k in {"id", "event_type", "data_type", "expiration_time"}}
            for s in _subscriptions(profile)
            if s.get("callback_url") == callback
        ],
    }


def pull(profile):
    with profile.lock("webhooks"):
        profile.config = read_json(profile.path / "profile.json")
        if profile.config.get("webhook_enabled") is False:
            return {
                "ok": True,
                "owner": profile.owner,
                "enabled": False,
                "processed": 0,
                "failed": {},
                "pending": 0,
            }
        remote = _remote(profile, "GET")
        client = Client(profile)
        uid = client.identity() if remote["events"] else profile.config.get("account_id")
        store = Store(profile)
        acknowledged, failed = [], {}
        try:
            for item in remote["events"]:
                eid, event = item["id"], item["event"]
                if event.get("user_id") != uid:
                    failed[eid] = {"code": "account_mismatch", "message": "Event is for another account"}
                    continue
                name = endpoint_name(event["data_type"])
                try:
                    if not store.event_done(eid):
                        try:
                            row = (
                                None
                                if event["event_type"] == "delete"
                                else client.document(name, event["object_id"])
                            )
                        except OuraError as exc:
                            if exc.code != "not_found":
                                raise
                            # A newer provider deletion may overtake the queued update.
                            row = None
                        if row is not None and not isinstance(row, dict):
                            raise OuraError("invalid_response", "Expected a single Oura document")
                        store.apply_event(eid, name, event["object_id"], row)
                    acknowledged.append(eid)
                except OuraError as exc:
                    failed[eid] = exc.as_dict()
            if acknowledged:
                _remote(profile, "POST", "/ack", {"ids": acknowledged})
        finally:
            store.close()
        result = {
            "ok": not failed,
            "owner": profile.owner,
            "processed": len(acknowledged),
            "failed": failed,
            "pending": remote["pending"] - len(acknowledged),
        }
        last = profile.config.get("webhook_maintained_at")
        if not last or datetime.now(UTC).timestamp() - datetime.fromisoformat(last).timestamp() > 3600:
            try:
                _maintain(profile)
            except OuraError as exc:
                result["ok"] = False
                result["maintenance_error"] = exc.as_dict()
        return result


def disable(profile):
    with profile.lock("webhooks"):
        profile.config = read_json(profile.path / "profile.json")
        route, poll = _creds(profile)
        relay_request(profile, "POST", "/owners/" + route + "/disable", {}, bearer=poll)
        profile.config["webhook_enabled"] = False
        profile.save_config()
    return {"ok": True, "owner": profile.owner, "enabled": False, "shared_subscriptions_preserved": True}
