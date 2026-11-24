from __future__ import annotations

import os
import time
from urllib.parse import quote, urlencode

from .errors import OuraError
from .http import checked_url, expect, fetch

ENDPOINTS = (
    "daily_sleep",
    "sleep",
    "daily_readiness",
    "daily_activity",
    "workout",
    "heartrate",
    "daily_stress",
    "daily_resilience",
    "sleep_time",
    "daily_spo2",
    "daily_cardiovascular_age",
    "session",
    "rest_mode_period",
    "ring_configuration",
    "enhanced_tag",
    "tag",
    "vO2_max",
    "ring_battery_level",
)
ALIASES = {
    "vo2": "vO2_max",
    "vo2_max": "vO2_max",
    "battery": "ring_battery_level",
    "readiness": "daily_readiness",
    "activity": "daily_activity",
    "stress": "daily_stress",
}


def endpoint_name(name):
    name = ALIASES.get(name, name)
    if name not in ENDPOINTS:
        raise OuraError("invalid_endpoint", "Unknown Oura endpoint")
    return name


def verify_identity(profile, identity):
    uid = identity.get("id") if isinstance(identity, dict) else None
    if not isinstance(uid, str) or not uid.strip():
        raise OuraError("invalid_identity", "Oura did not return a valid account ID")
    pinned = profile.config.get("account_id")
    if pinned and pinned != uid:
        raise OuraError("account_mismatch", "This Oura account differs from the owner pinned to this profile")
    return uid


class Client:
    def __init__(self, profile):
        self.profile = profile
        self.base = checked_url(os.environ.get("OURA_API_URL", "https://api.ouraring.com"))

    def candidate_identity(self, token):
        status, obj = fetch(
            "GET", self.base + "/v2/usercollection/personal_info", {"Authorization": "Bearer " + token}
        )
        expect(status, obj)
        verify_identity(self.profile, obj)
        return obj

    def _refresh(self, secrets):
        if secrets.get("refresh_uncertain"):
            raise OuraError(
                "login_required", "Previous token exchange was uncertain; authorize this owner again", 3
            )
        if not all(secrets.get(x) for x in ("client_id", "client_secret", "refresh_token")):
            raise OuraError("login_required", "Run oura auth login; this grant cannot be refreshed", 3)
        try:
            status, value = fetch(
                "POST",
                self.base + "/oauth/token",
                payload={
                    "grant_type": "refresh_token",
                    "refresh_token": secrets["refresh_token"],
                    "client_id": secrets["client_id"],
                    "client_secret": secrets["client_secret"],
                },
                form=True,
            )
        except OuraError as exc:
            # A refresh token is single-use. Never blindly retry an uncertain exchange.
            if exc.code == "network_error":
                secrets["refresh_uncertain"] = True
                self.profile.save_secrets(secrets)
            raise
        if status in {400, 401}:
            raise OuraError(
                "login_required", "Oura rejected the refresh grant; authorize this owner again", 3
            )
        expect(status, value)
        if not isinstance(value.get("access_token"), str) or not value.get("refresh_token"):
            secrets["refresh_uncertain"] = True
            self.profile.save_secrets(secrets)
            raise OuraError("login_required", "Token response was incomplete; authorize again", 3)
        updated = {
            **secrets,
            "access_token": value["access_token"],
            "refresh_token": value["refresh_token"],
            "expires_at": time.time() + float(value.get("expires_in", 3600)),
            "token_kind": "oauth",
        }
        updated.pop("refresh_uncertain", None)
        self.profile.save_secrets(updated)
        return updated["access_token"]

    def token(self, rejected=None):
        with self.profile.lock("auth"):
            secrets = self.profile.secrets
            token = secrets.get("access_token")
            if not token:
                raise OuraError("login_required", "Authorize this owner with oura auth login", 3)
            expiry = secrets.get("expires_at")
            expired = expiry is not None and float(expiry) <= time.time() + 60
            if expired or (rejected is not None and rejected == token):
                return self._refresh(secrets)
            return token

    def request(self, method, path, params=None, payload=None, *, app_auth=False):
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        if app_auth:
            s = self.profile.secrets
            if not s.get("client_id") or not s.get("client_secret"):
                raise OuraError("app_not_configured", "Configure Oura app credentials first")
            return expect(
                *fetch(
                    method,
                    url,
                    {"x-client-id": s["client_id"], "x-client-secret": s["client_secret"]},
                    payload,
                )
            )
        token = self.token()
        status, obj = fetch(method, url, {"Authorization": "Bearer " + token}, payload)
        if status == 401 and not obj.get("missing_scope"):
            token = self.token(rejected=token)
            status, obj = fetch(method, url, {"Authorization": "Bearer " + token}, payload)
        return expect(status, obj)

    def identity(self):
        obj = self.request("GET", "/v2/usercollection/personal_info")
        uid = verify_identity(self.profile, obj)
        if not self.profile.config.get("account_id"):
            self.profile.config["account_id"] = uid
            self.profile.save_config()
        return uid

    def collection(self, endpoint, params):
        endpoint_name(endpoint)
        rows, seen = [], set()
        query = dict(params)
        for _ in range(1000):
            obj = self.request("GET", "/v2/usercollection/" + endpoint, query)
            if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
                raise OuraError("invalid_response", "Oura collection has no data array")
            if any(not isinstance(x, dict) for x in obj["data"]):
                raise OuraError("invalid_response", "Oura collection contains an invalid record")
            rows.extend(obj["data"])
            nxt = obj.get("next_token")
            if not nxt:
                return rows
            if not isinstance(nxt, str) or nxt in seen:
                raise OuraError(
                    "pagination_loop", "Repeated pagination token; partial collection was not saved"
                )
            seen.add(nxt)
            query["next_token"] = nxt
        raise OuraError("pagination_limit", "Pagination limit reached; partial collection was not saved")

    def document(self, endpoint, document_id):
        endpoint_name(endpoint)
        return self.request("GET", "/v2/usercollection/" + endpoint + "/" + quote(document_id, safe=""))
