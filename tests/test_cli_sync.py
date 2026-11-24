import json

from .conftest import create_owner, import_owner


def test_profile_is_private_and_rejects_path_traversal(cli):
    path = create_owner(cli)
    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700
    code, result = cli("profile", "create", "../escape")
    assert code != 0 and result["error"]["code"] == "invalid_owner"
    assert not (cli.home / "escape").exists()


def test_two_owners_require_selection_and_never_mix_documents(cli, provider):
    import_owner(cli)
    import_owner(cli, "bob")
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "d1", "day": "2024-11-24", "score": 80}]}
    assert (
        cli(
            "sync", "--endpoint", "daily_sleep", "--start", "2024-11-24", "--end", "2024-11-24", owner="alice"
        )[0]
        == 0
    )
    code, result = cli("status")
    assert code != 0 and result["error"]["code"] == "owner_required"
    code, result = cli("read", "daily_sleep", owner="bob")
    assert code == 0 and result["data"] == []
    code, result = cli("read", "daily_sleep", owner="alice")
    assert len(result["data"]) == 1 and result["owner"] == "alice"


def test_pagination_updates_existing_ids_and_preserves_history(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {
        "data": [{"id": "a", "day": "2024-11-23", "score": 70}],
        "next_token": "page2",
    }
    provider["pages"][("daily_sleep", "page2")] = {"data": [{"id": "b", "day": "2024-11-24", "score": 80}]}
    assert cli("sync", "--endpoint", "daily_sleep", "--start", "2024-11-23", "--end", "2024-11-24")[0] == 0
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "b", "day": "2024-11-24", "score": 85}]}
    assert cli("sync", "--endpoint", "daily_sleep", "--start", "2024-11-24", "--end", "2024-11-24")[0] == 0
    code, result = cli("read", "daily_sleep")
    assert code == 0 and [(r["id"], r["score"]) for r in result["data"]] == [("a", 70), ("b", 85)]
    assert result["freshness"]["latest_day"] == "2024-11-24"
    assert result["freshness"]["fetched_at"]


def test_failed_second_page_does_not_commit_partial_success(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {
        "data": [{"id": "a", "day": "2024-11-24"}],
        "next_token": "again",
    }
    provider["pages"][("daily_sleep", "again")] = {"data": [], "next_token": "again"}
    code, result = cli("sync", "--endpoint", "daily_sleep")
    assert code != 0 and result["failed"]["daily_sleep"]["code"] == "pagination_loop"
    assert cli("read", "daily_sleep")[1]["data"] == []
    assert cli("status")[1]["endpoints"]["daily_sleep"]["last_success"] is None


def test_partial_failure_keeps_working_metrics_and_has_nonzero_exit(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "a", "day": "2024-11-24"}]}
    provider["failures"]["daily_stress"] = 403
    code, result = cli("sync", "--endpoint", "daily_sleep", "--endpoint", "daily_stress")
    assert code != 0 and result["failed"]["daily_stress"]["code"] == "permission_or_membership"
    assert cli("read", "daily_sleep")[1]["data"][0]["id"] == "a"
    assert "provider message" not in json.dumps(result)


def test_rate_limit_retries_and_recovers(cli, provider):
    import_owner(cli)
    provider["failures"]["daily_sleep"] = [429, 503, 200]
    assert cli("sync", "--endpoint", "daily_sleep")[0] == 0
    requests = [x for x in provider["requests"] if x[1].endswith("daily_sleep")]
    assert len(requests) == 3


def test_incremental_sync_uses_overlap_and_not_initial_month(cli, provider):
    import_owner(cli)
    assert cli("sync", "--endpoint", "daily_sleep", "--start", "2024-11-20", "--end", "2024-11-24")[0] == 0
    assert cli("sync", "--endpoint", "daily_sleep", "--end", "2024-11-25")[0] == 0
    q = [x for x in provider["requests"] if x[1].endswith("daily_sleep")][-1][2]
    assert q["start_date"] == ["2024-11-22"] and q["end_date"] == ["2024-11-25"]


def test_heart_rate_range_includes_target_local_day(cli, provider):
    import_owner(cli)
    assert cli("sync", "--endpoint", "heartrate", "--start", "2024-11-24", "--end", "2024-11-24")[0] == 0
    q = [x for x in provider["requests"] if x[1].endswith("heartrate")][-1][2]
    assert q["start_datetime"] == ["2024-11-24T00:00:00+04:00"]
    assert q["end_datetime"] == ["2024-11-25T00:00:00+04:00"]
    assert "start_date" not in q


def test_sleep_separates_main_sleep_naps_and_missing_values(cli, provider):
    import_owner(cli)
    provider["pages"][("sleep", None)] = {
        "data": [
            {
                "id": "main",
                "day": "2024-11-24",
                "type": "long_sleep",
                "total_sleep_duration": 25000,
                "average_hrv": None,
            },
            {"id": "nap", "day": "2024-11-24", "type": "late_nap", "total_sleep_duration": 1800},
        ]
    }
    assert cli("sync", "--endpoint", "sleep", "--start", "2024-11-24", "--end", "2024-11-24")[0] == 0
    code, result = cli("sleep", "--date", "2024-11-24")
    assert code == 0 and result["main_sleep"]["id"] == "main"
    assert [x["id"] for x in result["naps"]] == ["nap"]
    assert result["main_sleep"]["average_hrv"] is None and result["units"]["duration"] == "seconds"
    assert result["source"] == "Oura API v2"


def test_invalid_date_range_and_unknown_endpoint_fail_before_network(cli, provider):
    import_owner(cli)
    n = len(provider["requests"])
    for args in [("sync", "--start", "2024-12-01", "--end", "2024-11-01"), ("read", "../secret")]:
        assert cli(*args)[0] != 0
    assert len(provider["requests"]) == n


def test_old_measurement_is_not_marked_fresh_by_recent_download(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "old", "day": "2020-01-01"}]}
    assert cli("sync", "--endpoint", "daily_sleep")[0] == 0
    result = cli("read", "daily_sleep")[1]
    assert result["freshness"]["stale"] is True
    assert result["freshness"]["latest_day"] == "2020-01-01"


def test_export_csv_and_skill_install_are_usable(cli, provider, tmp_path):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {"data": [{"id": "a", "day": "2024-11-24", "score": 80}]}
    assert cli("sync", "--endpoint", "daily_sleep")[0] == 0
    out = tmp_path / "export.csv"
    assert cli("export", "daily_sleep", "--format", "csv", "--output", str(out))[0] == 0
    assert "score" in out.read_text() and "80" in out.read_text()
    dest = tmp_path / "skills"
    assert cli("skill", "install", "--destination", str(dest))[0] == 0
    assert (dest / "oura-cli" / "SKILL.md").is_file()


def test_sync_reconciles_provider_deletions_only_inside_requested_window(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {
        "data": [{"id": "a", "day": "2024-11-23", "score": 70}, {"id": "b", "day": "2024-11-24", "score": 80}]
    }
    assert cli("sync", "--endpoint", "daily_sleep", "--start", "2024-11-23", "--end", "2024-11-24")[0] == 0
    provider["pages"][("daily_sleep", None)] = {"data": []}
    assert cli("sync", "--endpoint", "daily_sleep", "--start", "2024-11-24", "--end", "2024-11-24")[0] == 0
    assert [r["id"] for r in cli("read", "daily_sleep")[1]["data"]] == ["a"]


def test_requested_date_has_its_own_measurement_freshness(cli, provider):
    import_owner(cli)
    provider["pages"][("daily_sleep", None)] = {
        "data": [{"id": "old", "day": "2024-11-20"}, {"id": "today", "day": "2024-11-24"}]
    }
    assert cli("sync", "--endpoint", "daily_sleep")[0] == 0
    result = cli("read", "daily_sleep", "--date", "2024-11-20")[1]
    assert result["freshness"]["latest_day"] == "2024-11-20"


def test_battery_backfill_obeys_live_oura_30_day_timerange_limit(cli, provider):
    import_owner(cli)
    code, result = cli(
        "sync", "--endpoint", "ring_battery_level", "--start", "2024-10-25", "--end", "2024-11-24"
    )
    assert code == 0, result
    queries = [r[2] for r in provider["requests"] if r[1].endswith("/ring_battery_level")]
    assert len(queries) == 2
    assert queries[0]["end_datetime"] == queries[1]["start_datetime"]


def test_profile_sync_selection_limits_default_but_explicit_endpoint_can_probe_access(cli, provider):
    from .conftest import import_owner

    path = import_owner(cli)
    p = path / "profile.json"
    j = json.loads(p.read_text())
    j["sync_endpoints"] = ["sleep", "daily_readiness"]
    p.write_text(json.dumps(j))
    code, value = cli("sync")
    assert code == 0 and set(value["synced"]) == {"sleep", "daily_readiness"}
    code, value = cli("sync", "--endpoint", "heartrate")
    assert code == 0 and set(value["synced"]) == {"heartrate"}


def test_empty_profile_sync_selection_does_not_expand_back_to_every_collection(cli):
    from .conftest import import_owner

    path = import_owner(cli)
    p = path / "profile.json"
    j = json.loads(p.read_text())
    j["sync_endpoints"] = []
    p.write_text(json.dumps(j))
    code, value = cli("sync")
    assert code != 0 and value["error"]["code"] == "invalid_config"
