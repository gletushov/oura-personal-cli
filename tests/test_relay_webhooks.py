import base64
import hashlib
import hmac
import json
import subprocess
import sys
import time
from urllib import error, parse, request

from .conftest import configure, create_owner, import_owner


def http(url, method="GET", body=None, bearer=None, headers=None):
    h = {**(headers or {})}
    if bearer:
        h["Authorization"] = "Bearer " + bearer
    raw = json.dumps(body).encode() if isinstance(body, dict) else body
    if raw is not None:
        h["Content-Type"] = "application/json"
    req = request.Request(url, data=raw, headers=h, method=method)
    try:
        with request.urlopen(req, timeout=4) as r:
            return r.status, json.loads(r.read())
    except error.HTTPError as e:
        body = e.read()
        try:
            value = json.loads(body)
        except ValueError:
            value = {}
        return e.code, value


def enroll(relay, uid="user-alice", route="r" * 43, poll="p" * 43):
    code, obj = http(
        relay["url"] + "/owners",
        "POST",
        {
            "route": route,
            "user_id": uid,
            "poll_hash": hashlib.sha256(poll.encode()).hexdigest(),
            "verification_token": "v" * 43,
        },
        "relay-reg-test",
    )
    assert code == 200, obj
    return route, poll


def event(relay, route, uid="user-alice", event_type="update", object_id="doc1", stamp=None, signature=True):
    payload = {
        "event_type": event_type,
        "data_type": "sleep",
        "object_id": object_id,
        "user_id": uid,
        "event_time": "2024-11-24T10:00:00Z",
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()) if stamp is None else stamp)
    sig = hmac.new(b"client-secret-test", timestamp.encode() + body, hashlib.sha256).hexdigest().upper()
    headers = {"x-oura-timestamp": timestamp, "x-oura-signature": sig if signature else "bad"}
    return http(relay["url"] + "/webhooks/" + route, "POST", body, headers=headers)


def test_relay_requires_enrollment_and_keeps_poll_results_private(relay):
    state = "s" * 43
    poll = "p" * 43
    payload = {
        "state": state,
        "poll_hash": hashlib.sha256(poll.encode()).hexdigest(),
        "client_id": "app-test",
    }
    assert http(relay["url"] + "/oauth/start", "POST", payload)[0] == 401
    code, obj = http(relay["url"] + "/oauth/start", "POST", payload, "relay-reg-test")
    assert code == 200 and obj["redirect_uri"] == relay["url"] + "/oauth/callback"
    assert http(relay["url"] + "/oauth/poll/" + state, bearer="wrong")[0] == 401
    assert http(relay["url"] + "/oauth/poll/" + state, bearer=poll)[1]["status"] == "pending"


def test_callback_rejects_unknown_state_and_cannot_overwrite_completed_session(relay):
    assert http(relay["url"] + "/oauth/callback?state=unknown&code=code-alice")[0] == 400
    state = "s" * 43
    poll = "p" * 43
    assert (
        http(
            relay["url"] + "/oauth/start",
            "POST",
            {"state": state, "poll_hash": hashlib.sha256(poll.encode()).hexdigest(), "client_id": "app-test"},
            "relay-reg-test",
        )[0]
        == 200
    )
    assert (
        http(relay["url"] + "/oauth/callback?" + parse.urlencode({"state": state, "code": "code-alice"}))[0]
        == 200
    )
    assert (
        http(relay["url"] + "/oauth/callback?" + parse.urlencode({"state": state, "code": "code-bob"}))[0]
        == 409
    )
    assert http(relay["url"] + "/oauth/poll/" + state, bearer=poll)[1]["code"] == "code-alice"


def test_phone_oauth_roundtrip_keeps_pkce_verifier_at_cli(cli, relay, provider):
    path = create_owner(cli)
    configure(cli, relay_url=relay["url"], relay_key="relay-reg-test")
    code, start = cli("auth", "start")
    assert code == 0, start
    q = parse.parse_qs(parse.urlsplit(start["authorize_url"]).query)
    assert q["redirect_uri"] == [relay["url"] + "/oauth/callback"] and q["code_challenge_method"] == ["S256"]
    assert {"stress", "ring_configuration"} <= set(q["scope"][0].split())
    pending = json.loads((path / "pending-auth.json").read_text())
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
    )
    assert q["code_challenge"] == [expected] and "verifier" not in start["authorize_url"]
    assert cli("auth", "finish")[1]["status"] == "pending"
    assert (
        http(q["redirect_uri"][0] + "?" + parse.urlencode({"state": q["state"][0], "code": "code-alice"}))[0]
        == 200
    )
    code, result = cli("auth", "finish")
    assert code == 0 and result["status"] == "authorized", result
    assert provider["last_exchange"]["code_verifier"] == [pending["verifier"]]
    assert not (path / "pending-auth.json").exists()
    assert cli("status", "--live")[1]["account_id"] == "user-alice"
    relay_files = "".join(
        p.read_bytes().decode(errors="ignore")
        for p in (relay["config"].parent / "relay-state").glob("*")
        if p.is_file()
    )
    assert "access-alice" not in relay_files and pending["verifier"] not in relay_files


def test_oauth_cannot_replace_pinned_account(cli, relay):
    import_owner(cli)
    configure(cli, relay_url=relay["url"], relay_key="relay-reg-test")
    start = cli("auth", "start")[1]
    q = parse.parse_qs(parse.urlsplit(start["authorize_url"]).query)
    http(q["redirect_uri"][0] + "?" + parse.urlencode({"state": q["state"][0], "code": "code-bob"}))
    code, result = cli("auth", "finish")
    assert code != 0 and result["error"]["code"] == "account_mismatch"
    assert cli("status", "--live")[1]["account_id"] == "user-alice"


def test_parallel_refresh_consumes_single_use_grant_once(cli, provider):
    path = import_owner(cli)
    configure(cli)
    f = path / "secrets.json"
    j = json.loads(f.read_text())
    j.update(access_token="expired", refresh_token="refresh-old", expires_at=0)
    f.write_text(json.dumps(j))
    cmds = [
        subprocess.Popen(
            [sys.executable, "-m", "oura_cli", "status", "--live", "--json"],
            env=cli.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for p in cmds:
        out, err = p.communicate(timeout=10)
        assert p.returncode == 0, (out, err)
    assert provider["refreshes"] == 1


def test_webhook_checks_signature_timestamp_owner_and_verification_token(relay):
    route, poll = enroll(relay)
    assert http(relay["url"] + "/webhooks/" + route + "?verification_token=wrong&challenge=hello")[0] == 401
    code, obj = http(
        relay["url"] + "/webhooks/" + route + "?verification_token=" + "v" * 43 + "&challenge=hello"
    )
    assert code == 200 and obj == {"challenge": "hello"}
    assert event(relay, route, signature=False)[0] == 401
    assert event(relay, route, stamp=int(time.time()) - 600)[0] == 401
    assert event(relay, route, uid="user-bob")[0] == 403
    assert http(relay["url"] + "/events/" + route, bearer=poll)[1]["events"] == []


def test_webhook_queue_survives_restart_deduplicates_and_acknowledges(relay):
    route, poll = enroll(relay)
    assert event(relay, route)[0] == 200
    assert event(relay, route)[0] == 200
    relay["restart"]()
    code, obj = http(relay["url"] + "/events/" + route, bearer=poll)
    assert code == 200 and len(obj["events"]) == 1
    assert http(relay["url"] + "/events/" + route, bearer="wrong")[0] == 401
    assert (
        http(relay["url"] + "/events/" + route + "/ack", "POST", {"ids": [obj["events"][0]["id"]]}, poll)[0]
        == 200
    )
    assert http(relay["url"] + "/events/" + route, bearer=poll)[1]["events"] == []


def test_route_registration_cannot_change_pinned_identity_or_poll_key(relay):
    route, poll = enroll(relay)
    assert (
        http(
            relay["url"] + "/owners",
            "POST",
            {
                "route": route,
                "user_id": "user-bob",
                "poll_hash": hashlib.sha256(b"new").hexdigest(),
                "verification_token": "v" * 43,
            },
            "relay-reg-test",
        )[0]
        == 409
    )
    assert http(relay["url"] + "/events/" + route, bearer=poll)[0] == 200


def test_webhook_enable_is_idempotent_and_renew_uses_documented_route(cli, relay, provider):
    import_owner(cli)
    configure(cli, relay_url=relay["url"], relay_key="relay-reg-test")
    code, result = cli("webhooks", "enable", "--endpoint", "sleep")
    assert code == 0
    assert result["callback_url"] == provider["subscriptions"][0]["callback_url"]
    assert cli("webhooks", "enable", "--endpoint", "sleep")[0] == 0
    assert len(provider["subscriptions"]) == 3
    assert {s["event_type"] for s in provider["subscriptions"]} == {"create", "update", "delete"}
    provider["subscriptions"][0]["expiration_time"] = "2020-01-01T00:00:00Z"
    assert cli("webhooks", "maintain")[0] == 0
    assert provider["subscriptions"][0]["expiration_time"].startswith("2099")


def test_webhook_pull_upserts_deletes_and_recovers_without_losing_event(cli, relay, provider):
    path = import_owner(cli)
    configure(cli, relay_url=relay["url"], relay_key="relay-reg-test")
    assert cli("webhooks", "enable", "--endpoint", "sleep")[0] == 0
    config = json.loads((path / "profile.json").read_text())
    route = config["webhook_route"]
    provider["pages"][("sleep/doc1", None)] = {
        "id": "doc1",
        "day": "2024-11-24",
        "type": "long_sleep",
        "total_sleep_duration": 20000,
    }
    assert event(relay, route)[0] == 200
    provider["failures"]["sleep/doc1"] = 503
    assert cli("webhooks", "pull")[0] != 0
    provider["failures"].pop("sleep/doc1")
    assert cli("webhooks", "pull")[0] == 0
    assert cli("sleep", "--date", "2024-11-24")[1]["main_sleep"]["id"] == "doc1"
    assert event(relay, route, event_type="delete")[0] == 200
    assert cli("webhooks", "pull")[0] == 0
    assert cli("read", "sleep")[1]["data"] == []
    assert cli("webhooks", "status")[1]["pending"] == 0


def test_provider_404_after_queued_update_is_a_deletion_not_an_infinite_retry(cli, relay, provider):
    path = import_owner(cli)
    configure(cli, relay_url=relay["url"], relay_key="relay-reg-test")
    assert cli("webhooks", "enable", "--endpoint", "sleep")[0] == 0
    route = json.loads((path / "profile.json").read_text())["webhook_route"]
    assert event(relay, route)[0] == 200
    provider["failures"]["sleep/doc1"] = 404
    code, value = cli("webhooks", "pull")
    assert code == 0 and value["processed"] == 1, value
    assert cli("webhooks", "status")[1]["pending"] == 0


def test_unknown_webhook_keys_cannot_store_biometric_payloads_in_relay(relay):
    route, poll = enroll(relay)
    payload = {
        "event_type": "update",
        "data_type": "sleep",
        "object_id": "doc1",
        "user_id": "user-alice",
        "event_time": "2024-11-24T10:00:00Z",
        "private_document": {"secret_biometric": "DO_NOT_RELAY"},
    }
    raw = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(b"client-secret-test", stamp.encode() + raw, hashlib.sha256).hexdigest()
    assert (
        http(
            relay["url"] + "/webhooks/" + route,
            "POST",
            raw,
            headers={"x-oura-timestamp": stamp, "x-oura-signature": sig},
        )[0]
        == 200
    )
    queue = http(relay["url"] + "/events/" + route, bearer=poll)[1]
    assert "DO_NOT_RELAY" not in json.dumps(queue)


def test_acked_event_replay_does_not_return_to_queue(relay):
    route, poll = enroll(relay)
    assert event(relay, route)[0] == 200
    queued = http(relay["url"] + "/events/" + route, bearer=poll)[1]["events"]
    assert (
        http(relay["url"] + "/events/" + route + "/ack", "POST", {"ids": [queued[0]["id"]]}, poll)[0] == 200
    )
    assert event(relay, route)[0] == 200
    assert http(relay["url"] + "/events/" + route, bearer=poll)[1]["pending"] == 0


def test_app_wide_webhook_dispatches_only_to_matching_owner_queue(relay):
    alice_route, alice_poll = enroll(relay)
    bob_route, bob_poll = enroll(relay, uid="user-bob", route="z" * 43, poll="q" * 43)
    assert event(relay, "", uid="user-alice")[0] == 200
    assert http(relay["url"] + "/events/" + alice_route, bearer=alice_poll)[1]["pending"] == 1
    assert http(relay["url"] + "/events/" + bob_route, bearer=bob_poll)[1]["pending"] == 0
    assert event(relay, "", uid="user-bob")[0] == 200
    assert http(relay["url"] + "/events/" + bob_route, bearer=bob_poll)[1]["pending"] == 1
    assert event(relay, "", uid="unregistered-user")[0] == 200
    assert http(relay["url"] + "/events/" + alice_route, bearer=alice_poll)[1]["pending"] == 1


def test_app_callback_verifies_subscription_with_any_enrolled_owner_token(relay):
    enroll(relay)
    code, value = http(relay["url"] + "/webhooks?verification_token=" + "v" * 43 + "&challenge=hello")
    assert code == 200 and value == {"challenge": "hello"}


def test_idle_webhook_worker_does_not_poll_oura_when_queue_is_empty(cli, relay, provider):
    import_owner(cli)
    configure(cli, relay_url=relay["url"], relay_key="relay-reg-test")
    assert cli("webhooks", "enable", "--endpoint", "sleep")[0] == 0
    before = len(provider["requests"])
    assert cli("webhooks", "pull")[0] == 0
    assert len(provider["requests"]) == before


def test_disabling_one_owner_preserves_shared_subscriptions_and_other_owner(cli, relay, provider):
    alice = import_owner(cli, "alice")
    bob = import_owner(cli, "bob")
    for owner in ["alice", "bob"]:
        configure(cli, owner, relay_url=relay["url"], relay_key="relay-reg-test")
        assert cli("webhooks", "enable", "--endpoint", "sleep", owner=owner)[0] == 0
    assert len(provider["subscriptions"]) == 3
    alice_route = json.loads((alice / "profile.json").read_text())["webhook_route"]
    bob_route = json.loads((bob / "profile.json").read_text())["webhook_route"]
    assert cli("webhooks", "disable", owner="alice")[0] == 0
    assert len(provider["subscriptions"]) == 3
    assert event(relay, "", uid="user-alice")[0] == 200
    assert event(relay, "", uid="user-bob")[0] == 200
    assert cli("webhooks", "status", owner="alice")[1]["enabled"] is False
    assert cli("webhooks", "status", owner="bob")[1]["pending"] == 1
    assert cli("webhooks", "enable", "--endpoint", "sleep", owner="alice")[0] == 0
    assert len(provider["subscriptions"]) == 3
    assert event(relay, "", uid="user-alice")[0] == 200
    assert cli("webhooks", "status", owner="alice")[1]["pending"] == 1
    assert alice_route != bob_route
