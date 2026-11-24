from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def provider():
    state = {
        "requests": [],
        "pages": {},
        "failures": {},
        "refreshes": 0,
        "subscriptions": [],
        "identities": {"access-alice": "user-alice", "access-bob": "user-bob"},
        "code_users": {"code-alice": "alice", "code-bob": "bob"},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path)
            q = parse_qs(path.query)
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            state["requests"].append(("GET", path.path, q, token))
            if path.path == "/v2/webhook/subscription":
                return self.reply(200, state["subscriptions"])
            if token not in state["identities"]:
                return self.reply(401, {"error": "expired"})
            if path.path.endswith("personal_info"):
                return self.reply(
                    200, {"id": state["identities"][token], "email": "synthetic@example.invalid"}
                )
            endpoint = path.path.removeprefix("/v2/usercollection/")
            if endpoint in state.get("scope_denied", set()):
                return self.reply(401, {"detail": f"Token is not authorized access {endpoint} scope."})
            if endpoint == "ring_battery_level" and q.get("start_datetime") and q.get("end_datetime"):
                from datetime import datetime

                start = datetime.fromisoformat(q["start_datetime"][0])
                end = datetime.fromisoformat(q["end_datetime"][0])
                if (end - start).total_seconds() > 30 * 86400:
                    return self.reply(400, {"detail": "Timerange must not exceed 30 days"})
            if endpoint in state["failures"]:
                code = state["failures"][endpoint]
                if isinstance(code, list):
                    code = code.pop(0) if code else 200
                if code != 200:
                    return self.reply(
                        code, {"error": "provider message may contain secret"}, {"Retry-After": "0"}
                    )
            key = (endpoint, q.get("next_token", [None])[0])
            if key in state["pages"]:
                return self.reply(200, state["pages"][key])
            return self.reply(200, {"data": [], "next_token": None})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            path = urlsplit(self.path).path
            state["requests"].append(("POST", path, {}, ""))
            if path == "/oauth/token":
                q = parse_qs(raw.decode())
                if state.get("token_error"):
                    return self.reply(400, state["token_error"])
                if q.get("grant_type") == ["refresh_token"]:
                    if q.get("refresh_token") != ["refresh-old"]:
                        return self.reply(400, {"error": "invalid_grant"})
                    state["refreshes"] += 1
                    state["identities"]["access-rotated"] = "user-alice"
                    return self.reply(
                        200,
                        {
                            "access_token": "access-rotated",
                            "refresh_token": "refresh-new",
                            "expires_in": 3600,
                        },
                    )
                user = state["code_users"].get(q.get("code", [""])[0])
                if not user or not q.get("code_verifier"):
                    return self.reply(400, {"error": "invalid_grant"})
                state["last_exchange"] = q
                return self.reply(
                    200,
                    {"access_token": "access-" + user, "refresh_token": "refresh-old", "expires_in": 3600},
                )
            if path == "/v2/webhook/subscription":
                obj = json.loads(raw)
                obj.update(
                    id="sub-" + str(len(state["subscriptions"])), expiration_time="2099-01-01T00:00:00+00:00"
                )
                state["subscriptions"].append(obj)
                return self.reply(200, obj)
            self.reply(404, {})

        def do_PUT(self):
            if not urlsplit(self.path).path.startswith("/v2/webhook/subscription/renew/"):
                return self.reply(404, {})
            sid = urlsplit(self.path).path.rsplit("/", 1)[-1]
            for sub in state["subscriptions"]:
                if sub["id"] == sid:
                    sub["expiration_time"] = "2099-01-01T00:00:00+00:00"
                    return self.reply(200, sub)
            self.reply(404, {})

        def do_DELETE(self):
            sid = urlsplit(self.path).path.rsplit("/", 1)[-1]
            state["subscriptions"][:] = [x for x in state["subscriptions"] if x["id"] != sid]
            self.reply(200, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = "http://127.0.0.1:" + str(server.server_port)
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def cli(tmp_path, provider):
    home = tmp_path / "home"
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "src"),
        OURA_CLI_HOME=str(home),
        OURA_API_URL=provider["url"],
        OURA_AUTH_URL=provider["url"] + "/authorize",
        OURA_RETRY_WAIT_MAX="0",
    )

    def run(*args, stdin=None, owner=None):
        cmd = [sys.executable, "-m", "oura_cli", "--json"]
        if owner:
            cmd += ["--owner", owner]
        r = subprocess.run(cmd + list(args), input=stdin, text=True, capture_output=True, env=env, timeout=15)
        output = r.stdout.strip() or r.stderr.strip()
        try:
            result = json.loads(output) if output else None
        except ValueError:
            pytest.fail(f"CLI produced non-JSON: {output[:500]}")
        return r.returncode, result

    run.home = home
    run.env = env
    return run


def create_owner(cli, name="alice"):
    code, value = cli("profile", "create", name, "--timezone", "Asia/Dubai")
    assert code == 0, value
    return cli.home / "owners" / name


def configure(cli, owner="alice", **extra):
    data = {"client_id": "app-test", "client_secret": "client-secret-test", **extra}
    code, value = cli("auth", "configure", "--file", "-", stdin=json.dumps(data), owner=owner)
    assert code == 0, value


def import_owner(cli, owner="alice"):
    path = create_owner(cli, owner)
    envfile = path.parent.parent / (owner + ".env")
    envfile.write_text("OURA_ACCESS_TOKEN=access-" + owner + "\n")
    code, value = cli("auth", "import-legacy", "--env-file", str(envfile), owner=owner)
    assert code == 0, value
    return path


@pytest.fixture
def relay(tmp_path, cli):
    import socket
    import time
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    config = tmp_path / "relay-config.json"
    config.write_text(
        json.dumps(
            {
                "public_url": url,
                "registration_key": "relay-reg-test",
                "client_id": "app-test",
                "client_secret": "client-secret-test",
                "state_dir": str(tmp_path / "relay-state"),
            }
        )
    )
    config.chmod(0o600)
    stderr = (tmp_path / "relay-stderr.txt").open("w+")

    def launch():
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "oura_cli",
                "relay",
                "serve",
                "--config",
                str(config),
                "--port",
                str(port),
            ],
            env=cli.env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if p.poll() is not None:
                stderr.seek(0)
                pytest.fail("Relay startup failed: " + stderr.read()[:1000])
            try:
                urllib.request.urlopen(url + "/health", timeout=0.2).close()
                return p
            except OSError:
                time.sleep(0.02)
        p.terminate()
        p.wait()
        pytest.fail("Relay did not become ready")

    proc = launch()
    info = {"url": url, "config": config, "process": proc}

    def restart():
        nonlocal proc
        proc.terminate()
        proc.wait(timeout=5)
        proc = launch()
        info["process"] = proc

    info["restart"] = restart
    yield info
    proc.terminate()
    proc.wait(timeout=5)
    stderr.close()
