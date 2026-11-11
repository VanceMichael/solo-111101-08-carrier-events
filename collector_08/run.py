"""--run 工作流：按场景采集多来源轨迹，投影为可审计时间线并派生履约状态。

- 原始记录幂等归档（fingerprint 去重），重抓与进程重启不产生重复证据。
- 投影按 mapping_version 隔离持久化；replay=true 只写自己版本的投影，
  绝不写通知队列，也不改变原始接收证据。
- 终态通知使用持久化去重键，普通采集首次到达终态仅发一条。
"""
import json
import sqlite3
from hashlib import sha256
from pathlib import Path

from .adapter import decode
from .projection import MAPPINGS, TERMINAL_STATES, build_projection
from .sources import FixtureTransport
from .store import RecordStore


def _fingerprint(source, payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(f"{source}\0{canonical}".encode()).hexdigest()


def load_scenario(path):
    scenario = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(scenario, dict) or not scenario.get("sources"):
        raise ValueError("场景文件缺少 sources")
    return scenario


def _capture_sources(scenario, run_id, store):
    """按顺序抓取来源并幂等归档；返回 (raw_rows, raw_count)。"""
    transport = FixtureTransport(scenario.get("fixture_root", "fixtures"))
    with store.connect() as connection:
        for route in scenario["sources"]:
            response = transport.request(route)
            if response.status != 200:
                raise ValueError(f"source {route} status {response.status}")
            for value in decode(response.body):
                connection.execute(
                    """INSERT OR IGNORE INTO raw_events
                       (source, run_id, authority, event_id, parcel, code, event_at, received_at, replaces, payload, fingerprint)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        route,
                        run_id,
                        str(value.get("authority", "")),
                        str(value.get("id", "")),
                        str(value.get("parcel", "")),
                        str(value.get("code", "")),
                        str(value.get("event_at", "")),
                        str(value.get("received_at", "")),
                        str(value.get("replaces", "")),
                        json.dumps(value, ensure_ascii=False, sort_keys=True),
                        _fingerprint(route, value),
                    ),
                )
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id AS rowid, source, authority, event_id, parcel, code, event_at, received_at, replaces"
                " FROM raw_events ORDER BY id"
            )
        ]
        raw_count = connection.execute("SELECT count(*) AS total FROM raw_events").fetchone()["total"]
    return rows, raw_count


def _persist_projection(store, projection, mapping_version, run_id):
    """投影按 (parcel, mapping_version) 隔离持久化，版本之间互不覆写。"""
    with store.connect() as connection:
        for parcel in projection["parcels"]:
            connection.execute(
                "INSERT OR REPLACE INTO projections(parcel, mapping_version, state, run_id) VALUES(?,?,?,?)",
                (parcel["id"], mapping_version, parcel["state"], run_id),
            )


def _notify(scenario, database, projection, run_id):
    """终态通知写入通知队列；持久化去重键保证重复输入与进程重启不重复发送。"""
    portal = scenario.get("portal_database")
    target = str(portal) if portal else str(database)
    created = 0
    connection = sqlite3.connect(target)
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY,
            dedup_key TEXT NOT NULL UNIQUE,
            parcel TEXT NOT NULL,
            state TEXT NOT NULL,
            run_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        for parcel in projection["parcels"]:
            if parcel["state"] in TERMINAL_STATES:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO notifications(dedup_key, parcel, state, run_id) VALUES(?,?,?,?)",
                    (f'{parcel["id"]}:{parcel["state"]}', parcel["id"], parcel["state"], run_id),
                )
                created += cursor.rowcount
        connection.commit()
    finally:
        connection.close()
    return created


def run(scenario_path, database, output):
    scenario = load_scenario(scenario_path)
    name = str(scenario.get("name", ""))
    run_id = str(scenario.get("run_id") or name or "run")
    options = dict(scenario.get("options") or {})
    replay = bool(options.get("replay", False))
    mapping_version = int(options.get("mapping_version", 1))
    if mapping_version not in MAPPINGS:
        raise ValueError(f"未注册的 mapping_version: {mapping_version!r}")
    weights = options.get("weights") or {}

    store = RecordStore(database)
    store.initialize()
    rows, raw_count = _capture_sources(scenario, run_id, store)
    projection = build_projection(rows, mapping_version, weights)
    _persist_projection(store, projection, mapping_version, run_id)
    # replay 只生成独立投影，绝不写通知队列；普通采集才对终态去重通知。
    notifications = 0 if replay else _notify(scenario, database, projection, run_id)

    result = {
        "run_id": run_id,
        "replay": replay,
        "mapping_version": mapping_version,
        "parcels": projection["parcels"],
        "timeline": projection["timeline"],
        "conflicts": projection["conflicts"],
        "superseded": projection["superseded"],
        "isolated": projection["isolated"],
        "notifications": notifications,
        "raw_count": raw_count,
    }
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
