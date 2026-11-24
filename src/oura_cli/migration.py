from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from .api import ENDPOINTS, verify_identity
from .config import private_dir, read_json, write_json
from .errors import OuraError
from .store import Store, now_iso


def import_archive(profile, directory):
    root = Path(directory).expanduser()
    if not profile.config.get("account_id"):
        raise OuraError("login_required", "Pin this profile to an Oura account before importing an archive")
    folders = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10)
    checked = []
    for path in folders:
        try:
            datetime.strptime(path.name, "%Y-%m-%d")
        except ValueError:
            continue
        if not (path / "personal_info.json").is_file():
            raise OuraError("invalid_identity", "Every imported snapshot must include personal_info.json")
        identity = read_json(path / "personal_info.json")
        historical = profile.config.get("historical_account_ids", [])
        if not isinstance(historical, list) or any(not isinstance(x, str) for x in historical):
            raise OuraError("invalid_state", "Historical account aliases must be a list of verified IDs")
        if identity.get("id") not in historical:
            verify_identity(profile, identity)
        checked.append(path)
    count = 0
    with profile.lock("sync"):
        store = Store(profile)
        try:
            for path in checked:
                for name in ENDPOINTS:
                    file = path / (name + ".json")
                    if not file.is_file():
                        continue
                    value = read_json(file)
                    if not isinstance(value, dict) or value.get("ok") is False:
                        continue
                    rows = value.get("data")
                    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
                        raise OuraError("invalid_archive", "Archive contains an invalid collection")
                    previous = store.states().get(name, {}).get("end_date")
                    until = max(path.name, previous or path.name)
                    store.ingest(name, rows, until)
                    count += len(rows)
        finally:
            store.close()
    return {
        "ok": True,
        "owner": profile.owner,
        "snapshots": len(checked),
        "records_processed": count,
        "originals_preserved": True,
    }


def legacy_output(profile, result, directory, target):
    folder = private_dir(Path(directory).expanduser() / target)
    store = Store(profile)
    summary = []
    try:
        for name, state in result["synced"].items():
            if name == "ring_configuration":
                rows = store.read(name)
            else:
                begin = min(state["start"], (date.fromisoformat(target) - timedelta(days=30)).isoformat())
                rows = store.read(name, begin, state["end"])
            write_json(
                folder / (name + ".json"),
                {"endpoint": name, "ok": True, "statuses": [200], "count": len(rows), "data": rows},
            )
            summary.append({"endpoint": name, "ok": True, "count": len(rows), "status": 200})
        for name, failure in result["failed"].items():
            summary.append({"endpoint": name, "ok": False, "count": None, "status": None, "error": failure})
        if not (folder / "personal_info.json").exists():
            write_json(folder / "personal_info.json", {"id": profile.config.get("account_id")})
        write_json(
            folder / "manifest.json",
            {
                "fetched_at": now_iso(),
                "target_date": target,
                "timezone": str(profile.timezone),
                "summary": summary,
            },
        )
    finally:
        store.close()
