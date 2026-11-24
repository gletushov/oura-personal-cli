from __future__ import annotations

import argparse
import csv
import importlib.resources
import io
import json
import os
import sqlite3
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__, auth
from .api import Client, endpoint_name
from .config import create_profile, owners, read_json, select_profile
from .data import read, sleep_report, sync
from .errors import OuraError
from .store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise OuraError("usage", message)


def command_parser():
    p = Parser(prog="oura", description="Portable owner-scoped Oura data CLI")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    profile = sub.add_parser("profile").add_subparsers(dest="action", required=True)
    create = profile.add_parser("create")
    create.add_argument("name")
    create.add_argument("--timezone", default="UTC")
    profile.add_parser("list")
    account = sub.add_parser("auth").add_subparsers(dest="action", required=True)
    config = account.add_parser("configure")
    config.add_argument("--file", required=True)
    config.add_argument("--relay-url")
    imp = account.add_parser("import-legacy")
    imp.add_argument("--env-file", required=True)
    for name in ["start", "finish", "status", "cancel"]:
        account.add_parser(name)
    login = account.add_parser("login")
    login.add_argument("--no-browser", action="store_true")
    login.add_argument("--timeout", type=int, default=600)
    logout = account.add_parser("logout")
    logout.add_argument("--confirm-owner", required=True)
    for name in ["status", "doctor"]:
        status = sub.add_parser(name)
        status.add_argument("--live", action="store_true")
    fetch = sub.add_parser("sync")
    fetch.add_argument("--endpoint", action="append")
    fetch.add_argument("--start")
    fetch.add_argument("--end")
    fetch.add_argument("--initial-days", type=int, default=30)
    fetch.add_argument("--legacy-output")
    archive = sub.add_parser("import-archive")
    archive.add_argument("--directory", required=True)
    for name in ["read", "export"]:
        r = sub.add_parser(name)
        r.add_argument("endpoint")
        r.add_argument("--start")
        r.add_argument("--end")
        r.add_argument("--date")
        if name == "export":
            r.add_argument("--format", choices=["json", "csv"], default="json")
            r.add_argument("--output", required=True)
    for name in ["sleep", "readiness", "activity", "stress", "heartrate", "battery", "vo2", "today"]:
        r = sub.add_parser(name)
        r.add_argument("--date")
    skill = sub.add_parser("skill").add_subparsers(dest="action", required=True)
    install = skill.add_parser("install")
    install.add_argument("--destination", required=True)
    relay = sub.add_parser("relay").add_subparsers(dest="action", required=True)
    serve = relay.add_parser("serve")
    serve.add_argument("--config", required=True)
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18765)
    webhooks = sub.add_parser("webhooks").add_subparsers(dest="action", required=True)
    enable = webhooks.add_parser("enable")
    enable.add_argument("--endpoint", action="append")
    for name in ["status", "pull", "maintain", "disable"]:
        webhooks.add_parser(name)
    worker = webhooks.add_parser("worker")
    worker.add_argument("--interval", type=int, default=30)
    worker.add_argument("--once", action="store_true")
    return p


def profile_status(profile, live=False):
    uid = Client(profile).identity() if live else profile.config.get("account_id")
    s = profile.secrets
    store = Store(profile)
    try:
        endpoints = store.states()
    finally:
        store.close()
    permissions = all(
        (p.stat().st_mode & 0o077) == 0
        for p in [profile.path, *profile.path.glob("*.json"), profile.path / "data.sqlite3"]
        if p.exists()
    )
    return {
        "ok": True,
        "version": __version__,
        "owner": profile.owner,
        "account_id": uid,
        "timezone": str(profile.timezone),
        "auth": {
            "configured": bool(s.get("access_token")),
            "token_kind": s.get("token_kind"),
            "refresh_available": bool(s.get("refresh_token")),
            "expires_at": s.get("expires_at"),
            "refresh_uncertain": bool(s.get("refresh_uncertain")),
        },
        "endpoints": endpoints,
        "private_permissions": permissions,
        "phone_login_configured": bool(
            profile.config.get("relay_url") and s.get("client_id") and s.get("relay_key")
        ),
        "webhooks_configured": bool(profile.config.get("webhook_route")),
    }


def execute(a, common):
    if a.command == "profile":
        if a.action == "list":
            return {"ok": True, "owners": owners(common.home)}
        profile = create_profile(common.home, a.name, a.timezone)
        return {"ok": True, "owner": profile.owner, "timezone": str(profile.timezone)}
    if a.command == "skill":
        dest = Path(a.destination).expanduser() / "oura-cli"
        if dest.exists():
            raise OuraError("destination_exists", "Existing skill was preserved; choose another destination")
        source = importlib.resources.files("oura_cli").joinpath("skill/SKILL.md")
        dest.mkdir(parents=True, mode=0o755)
        (dest / "SKILL.md").write_text(source.read_text())
        return {"ok": True, "installed": str(dest)}
    if a.command == "relay":
        from .relay import serve

        serve(Path(a.config), a.listen, a.port)
        return {"ok": True}
    p = select_profile(common.home, common.owner)
    if a.command == "auth":
        if a.action == "configure":
            values = json.load(sys.stdin) if a.file == "-" else read_json(a.file)
            return auth.configure(p, values, a.relay_url)
        if a.action == "import-legacy":
            return auth.import_legacy(p, a.env_file)
        if a.action == "start":
            return auth.start(p)
        if a.action == "finish":
            return auth.finish(p)
        if a.action == "cancel":
            return auth.cancel(p)
        if a.action == "status":
            return profile_status(p)
        if a.action == "logout":
            if a.confirm_owner != p.owner:
                raise OuraError("confirmation_required", "Confirm the selected owner name")
            with p.lock("auth"):
                s = p.secrets
                p.save_secrets({k: s[k] for k in ["client_id", "client_secret", "relay_key"] if k in s})
            auth.cancel(p)
            return {"ok": True, "owner": p.owner, "local_grant_removed": True, "data_preserved": True}
        if a.action == "login":
            value = auth.start(p)
            if not a.no_browser:
                webbrowser.open(value["authorize_url"])
            print(json.dumps(value), flush=True)
            deadline = time.monotonic() + max(1, min(a.timeout, 900))
            while time.monotonic() < deadline:
                value = auth.finish(p)
                if value["status"] == "authorized":
                    return value
                time.sleep(2)
            raise OuraError("login_pending", "Login is still pending; run oura auth finish after consent", 4)
    if a.command in {"status", "doctor"}:
        result = profile_status(p, a.live)
        if a.command == "doctor":
            result["ok"] = result["private_permissions"] and result["auth"]["configured"]
            result["checks"] = {
                "credentials_present": result["auth"]["configured"],
                "private_permissions": result["private_permissions"],
                "api_checked": a.live,
            }
        return result
    if a.command == "import-archive":
        from .migration import import_archive

        return import_archive(p, a.directory)
    if a.command == "sync":
        result = sync(p, a.endpoint, a.start, a.end, a.initial_days)
        if a.legacy_output:
            from .migration import legacy_output

            target = a.end or datetime.now(p.timezone).date().isoformat()
            legacy_output(p, result, a.legacy_output, target)
        return result
    if a.command in {"read", "export"}:
        value = read(p, a.endpoint, a.date or a.start, a.date or a.end)
        if a.command == "read":
            return value
        out = Path(a.output).expanduser()
        if out.is_symlink():
            raise OuraError("unsafe_path", "Export destination must not be a symbolic link")
        if a.format == "json":
            text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            stream = io.StringIO()
            fields = sorted({k for row in value["data"] for k in row})
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in value["data"]:
                writer.writerow(
                    {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()}
                )
            text = stream.getvalue()
        fd = os.open(out, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(out, 0o600)
        return {"ok": True, "owner": p.owner, "records": len(value["data"]), "output": str(out)}
    if a.command == "webhooks":
        from . import webhooks

        if a.action == "worker":
            while True:
                result = webhooks.pull(p)
                if a.once:
                    return result
                if not result["ok"]:
                    print(json.dumps(result), file=sys.stderr, flush=True)
                time.sleep(max(5, a.interval))
        return (
            getattr(webhooks, a.action)(p, a.endpoint)
            if a.action == "enable"
            else getattr(webhooks, a.action)(p)
        )
    day = a.date or datetime.now(p.timezone).date().isoformat()
    if a.command == "sleep":
        return sleep_report(p, day)
    if a.command == "today":
        return {
            "ok": True,
            "owner": p.owner,
            "date": day,
            "metrics": {
                key: read(p, key, day, day)
                for key in ["daily_sleep", "daily_readiness", "daily_activity", "daily_stress"]
            },
        }
    return read(p, endpoint_name(a.command), day, day)


def main(argv=None):
    os.umask(0o077)
    args = sys.argv[1:] if argv is None else argv
    common_parser = Parser(add_help=False, allow_abbrev=False)
    common_parser.add_argument(
        "--home", default=os.environ.get("OURA_CLI_HOME", str(Path.home() / ".local/share/oura-cli"))
    )
    common_parser.add_argument("--owner", default=os.environ.get("OURA_OWNER"))
    common_parser.add_argument("--json", action="store_true")
    common_parser.add_argument("--quiet", action="store_true")
    common = None
    try:
        common, rest = common_parser.parse_known_args(args)
        a = command_parser().parse_args(rest)
        result = execute(a, common)
        if not (common.quiet and result.get("ok")):
            print(json.dumps(result, ensure_ascii=False, indent=None if common.json else 2))
        return 0 if result.get("ok") else 5
    except OuraError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return exc.status
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "local_state_error",
                        "message": "Invalid local configuration or unavailable storage; no secret details are printed",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
