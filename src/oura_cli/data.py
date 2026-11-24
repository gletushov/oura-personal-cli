from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .api import ENDPOINTS, Client, endpoint_name
from .errors import OuraError
from .store import Store


def parse_day(value):
    try:
        return date.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        raise OuraError("invalid_date", "Use dates in YYYY-MM-DD format") from None


def date_range(start=None, end=None):
    a, b = parse_day(start), parse_day(end)
    if a and b and a > b:
        raise OuraError("invalid_date_range", "Start date must not follow end date")
    return a, b


def params_for(endpoint, start, end, tz):
    if endpoint in {"heartrate", "ring_battery_level"}:
        return {
            "start_datetime": datetime.combine(start, time.min, tzinfo=tz).isoformat(),
            "end_datetime": datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).isoformat(),
        }
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def sync(profile, endpoints=None, start=None, end=None, initial_days=30):
    selected = endpoints if endpoints is not None else profile.config.get("sync_endpoints", ENDPOINTS)
    if (
        not isinstance(selected, (list, tuple))
        or not selected
        or any(not isinstance(x, str) for x in selected)
    ):
        raise OuraError("invalid_config", "sync_endpoints must be a nonempty list of collection names")
    names = list(dict.fromkeys(endpoint_name(x) for x in selected))
    first, last = date_range(start, end)
    last = last or datetime.now(profile.timezone).date()
    if initial_days < 1 or initial_days > 3660:
        raise OuraError("invalid_range", "Initial window must be between 1 and 3660 days")
    if first and first > last:
        raise OuraError("invalid_date_range", "Start date must not follow end date")
    result = {"ok": True, "owner": profile.owner, "synced": {}, "failed": {}}
    client = Client(profile)
    client.identity()
    with profile.lock("sync"):
        store = Store(profile)
        try:
            for name in names:
                previous = store.states().get(name, {}).get("end_date")
                begin = first or (
                    parse_day(previous) - timedelta(days=2)
                    if previous
                    else last - timedelta(days=initial_days)
                )
                begin = min(begin, last)
                # Oura limits heart-rate and battery time-series ranges to 30 days. Fetch multiple non-overlapping chunks.
                windows = []
                if name in {"heartrate", "ring_battery_level"}:
                    day = begin
                    while day <= last:
                        until = min(day + timedelta(days=29), last)
                        windows.append((day, until))
                        day = until + timedelta(days=1)
                else:
                    windows = [(begin, last)]
                try:
                    rows = []
                    for a, b in windows:
                        rows.extend(client.collection(name, params_for(name, a, b, profile.timezone)))
                    fetched = store.ingest(name, rows, last.isoformat(), begin.isoformat())
                    result["synced"][name] = {
                        "received": len(rows),
                        "fetched_at": fetched,
                        "start": begin.isoformat(),
                        "end": last.isoformat(),
                    }
                except OuraError as exc:
                    store.fail(name, exc.as_dict())
                    result["failed"][name] = exc.as_dict()
            result["ok"] = not result["failed"]
            return result
        finally:
            store.close()


def read(profile, endpoint, start=None, end=None):
    endpoint = endpoint_name(endpoint)
    date_range(start, end)
    store = Store(profile)
    try:
        return {
            "ok": True,
            "schema_version": 1,
            "owner": profile.owner,
            "account_id": profile.config.get("account_id"),
            "source": "Oura API v2",
            "timezone": str(profile.timezone),
            "endpoint": endpoint,
            "data": store.read(endpoint, start, end),
            "freshness": store.freshness(endpoint, start, end),
        }
    finally:
        store.close()


def sleep_report(profile, day):
    result = read(profile, "sleep", day, day)
    rows = result["data"]
    main_candidates = [r for r in rows if r.get("type") == "long_sleep"]
    main = max(main_candidates, key=lambda r: r.get("total_sleep_duration") or 0, default=None)
    naps = [r for r in rows if r.get("type") in {"late_nap", "rest", "nap"}]
    result.update(
        main_sleep=main,
        naps=naps,
        other_sleep=[r for r in rows if r is not main and r not in naps],
        date=day,
        units={"duration": "seconds", "heart_rate": "bpm", "hrv": "ms", "temperature_deviation": "celsius"},
    )
    return result
