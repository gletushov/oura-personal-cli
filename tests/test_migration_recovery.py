import json

from .conftest import import_owner


def archive(root, uid="user-alice"):
    for day, score in [("2024-10-01", 60), ("2024-10-02", 70)]:
        p = root / day
        p.mkdir(parents=True)
        (p / "personal_info.json").write_text(json.dumps({"id": uid}))
        (p / "daily_sleep.json").write_text(
            json.dumps({"ok": True, "data": [{"id": day, "day": day, "score": score}]})
        )


def test_old_archive_import_keeps_history_and_rejects_another_owner(cli, tmp_path):
    import_owner(cli)
    src = tmp_path / "archive"
    archive(src)
    code, value = cli("import-archive", "--directory", str(src))
    assert code == 0, value
    assert [r["score"] for r in cli("read", "daily_sleep")[1]["data"]] == [60, 70]
    assert cli("import-archive", "--directory", str(src))[0] == 0
    assert len(cli("read", "daily_sleep")[1]["data"]) == 2
    bad = tmp_path / "wrong"
    archive(bad, uid="user-bob")
    code, value = cli("import-archive", "--directory", str(bad))
    assert code != 0 and value["error"]["code"] == "account_mismatch"
    assert len(cli("read", "daily_sleep")[1]["data"]) == 2


def test_legacy_output_is_compatible_without_overwriting_existing_personal_info(cli, provider, tmp_path):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "a", "day": "2024-11-24", "score": 80}]}
    out = tmp_path / "legacy"
    (out / "2024-11-24").mkdir(parents=True)
    original = '{"id":"user-alice","keep":"original"}'
    (out / "2024-11-24/personal_info.json").write_text(original)
    code, value = cli(
        "sync",
        "--endpoint",
        "daily_sleep",
        "--start",
        "2024-11-24",
        "--end",
        "2024-11-24",
        "--legacy-output",
        str(out),
    )
    assert code == 0, value
    saved = json.loads((out / "2024-11-24/daily_sleep.json").read_text())
    assert saved["ok"] is True and saved["data"][0]["score"] == 80
    assert json.loads((out / "2024-11-24/manifest.json").read_text())["summary"][0]["status"] == 200
    assert (out / "2024-11-24/personal_info.json").read_text() == original


def test_invalid_identity_blocks_sync_before_reading_biometric_documents(cli, provider):
    import_owner(cli)
    provider["identities"]["access-alice"] = "user-bob"
    code, value = cli("sync", "--endpoint", "sleep")
    assert code != 0 and value["error"]["code"] == "account_mismatch"
    assert all(not r[1].endswith("/sleep") for r in provider["requests"])


def test_invalid_record_does_not_leave_a_half_committed_collection(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "valid", "day": "2024-11-24"}, None]}
    code, value = cli("sync", "--endpoint", "daily_sleep")
    assert code != 0 and value["failed"]["daily_sleep"]["code"] == "invalid_response"
    assert cli("read", "daily_sleep")[1]["data"] == []


def test_server_side_expiry_refreshes_even_when_local_expiry_is_future(cli, provider):
    import time

    from .conftest import configure

    path = import_owner(cli)
    configure(cli)
    p = path / "secrets.json"
    j = json.loads(p.read_text())
    j.update(access_token="expired", refresh_token="refresh-old", expires_at=time.time() + 3600)
    p.write_text(json.dumps(j))
    assert cli("status", "--live")[0] == 0
    assert provider["refreshes"] == 1


def test_large_heart_rate_backfill_is_chunked_and_has_no_overlap(cli, provider):
    import_owner(cli)
    assert cli("sync", "--endpoint", "heartrate", "--start", "2024-09-01", "--end", "2024-11-24")[0] == 0
    queries = [r[2] for r in provider["requests"] if r[1].endswith("/heartrate")]
    assert len(queries) == 3
    assert queries[0]["end_datetime"] == queries[1]["start_datetime"]
    assert queries[1]["end_datetime"] == queries[2]["start_datetime"]
    assert queries[-1]["end_datetime"] == ["2024-11-25T00:00:00+04:00"]


def test_verified_historical_id_alias_applies_only_to_archive_not_live_account(cli, tmp_path):
    path = import_owner(cli)
    cfg = path / "profile.json"
    value = json.loads(cfg.read_text())
    value["historical_account_ids"] = [value["account_id"]]
    value["account_id"] = "new-user-alice"
    cfg.write_text(json.dumps(value))
    src = tmp_path / "archive"
    archive(src)
    code, value = cli("import-archive", "--directory", str(src))
    assert code == 0, value
    code, value = cli("status", "--live")
    assert code != 0 and value["error"]["code"] == "account_mismatch"


def test_incremental_sync_legacy_export_keeps_cached_month_for_existing_reports(cli, provider, tmp_path):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {
        "data": [
            {"id": "early", "day": "2024-11-01", "score": 70},
            {"id": "recent", "day": "2024-11-24", "score": 80},
        ]
    }
    assert cli("sync", "--endpoint", "daily_sleep", "--start", "2024-11-01", "--end", "2024-11-24")[0] == 0
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "recent", "day": "2024-11-24", "score": 85}]}
    out = tmp_path / "legacy"
    assert (
        cli("sync", "--endpoint", "daily_sleep", "--end", "2024-11-25", "--legacy-output", str(out))[0] == 0
    )
    values = json.loads((out / "2024-11-25/daily_sleep.json").read_text())["data"]
    assert [(r["id"], r["score"]) for r in values] == [("early", 70), ("recent", 85)]
