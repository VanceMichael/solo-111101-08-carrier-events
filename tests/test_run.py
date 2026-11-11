import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from collector_08.run import run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def write_scenario(directory, scenario):
    manifest = Path(directory) / "scenario.json"
    manifest.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    return manifest


class RunWorkflowTests(unittest.TestCase):
    def scenario(self, **overrides):
        base = {
            "name": "late_scan",
            "run_id": "run-late_scan",
            "sources": ["/carrier"],
            "options": {"replay": False, "mapping_version": 1},
            "fixture_root": str(FIXTURES),
        }
        base.update(overrides)
        return base

    def test_end_to_end_projection_and_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = write_scenario(work, self.scenario(portal_database=str(work / "portal.sqlite3")))
            result = run(manifest, work / "state.sqlite3", work / "out.json")
            self.assertEqual(result["parcels"], [{"id": "PK1", "state": "delivered"}])
            self.assertEqual(result["notifications"], 1)
            self.assertEqual(result["raw_count"], 2)
            saved = json.loads((work / "out.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["timeline"][-1]["event_at"], "2026-11-11T03:00:00Z")
            self.assertEqual(saved["timeline"][-1]["received_at"], "2026-11-11T04:00:00Z")

    def test_restart_keeps_projection_and_dedups_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = write_scenario(work, self.scenario(portal_database=str(work / "portal.sqlite3")))
            first = run(manifest, work / "state.sqlite3", work / "out.json")
            second = run(manifest, work / "state.sqlite3", work / "out.json")
            self.assertEqual(first["notifications"], 1)
            self.assertEqual(second["notifications"], 0)
            self.assertEqual(first["parcels"], second["parcels"])
            self.assertEqual(first["timeline"], second["timeline"])
            self.assertEqual(second["raw_count"], 2)

    def test_process_restart_dedup(self):
        # 两个独立进程共享同一状态库：验证持久化去重键跨进程重启生效
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = write_scenario(work, self.scenario(portal_database=str(work / "portal.sqlite3")))
            command = [
                sys.executable, "-m", "collector_08.cli",
                "--run", str(manifest),
                "--database", str(work / "state.sqlite3"),
                "--output", str(work / "out.json"),
            ]
            first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads((work / "out.json").read_text(encoding="utf-8"))["notifications"], 1)
            second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(second.returncode, 0, second.stderr)
            repeated = json.loads((work / "out.json").read_text(encoding="utf-8"))
            self.assertEqual(repeated["notifications"], 0)
            self.assertEqual(repeated["parcels"], [{"id": "PK1", "state": "delivered"}])

    def test_correction_cycle_scenario(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fixtures = work / "fixtures"
            fixtures.mkdir()
            (fixtures / "routes.json").write_text(json.dumps({
                "/base": [{"status": 200, "file": "base.xml"}],
                "/cycle": [{"status": 200, "file": "cycle.xml"}],
            }), encoding="utf-8")
            (fixtures / "base.xml").write_text(
                '<events><record id="E1" parcel="PK1"><code>TRANSIT</code>'
                "<event_at>2026-11-11T02:00:00Z</event_at>"
                "<received_at>2026-11-11T03:00:00Z</received_at>"
                "<authority>carrier</authority></record></events>",
                encoding="utf-8",
            )
            (fixtures / "cycle.xml").write_text(
                "<events>"
                '<record id="E5" parcel="PK1"><code>DELIVERED</code>'
                "<event_at>2026-11-11T04:00:00Z</event_at>"
                "<received_at>2026-11-11T05:00:00Z</received_at>"
                "<authority>carrier</authority><replaces>E6</replaces></record>"
                '<record id="E6" parcel="PK1"><code>RETURNED</code>'
                "<event_at>2026-11-11T04:30:00Z</event_at>"
                "<received_at>2026-11-11T06:00:00Z</received_at>"
                "<authority>carrier</authority><replaces>E5</replaces></record>"
                "</events>",
                encoding="utf-8",
            )
            manifest = write_scenario(work, self.scenario(
                name="cycle",
                run_id="run-cycle",
                sources=["/base", "/cycle"],
                fixture_root=str(fixtures),
                portal_database=str(work / "portal.sqlite3"),
            ))
            result = run(manifest, work / "state.sqlite3", work / "out.json")
            self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])
            self.assertEqual(result["superseded"], [])
            self.assertEqual({entry["event"] for entry in result["isolated"]}, {"E5", "E6"})
            self.assertTrue(all(entry["reason"] == "replaces_cycle" for entry in result["isolated"]))
            self.assertEqual(result["notifications"], 0)
            # 原始证据全部保留，循环更正不被覆写也不生效
            self.assertEqual(result["raw_count"], 3)

    def test_replay_does_not_notify_or_touch_raw_and_versions_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            portal = work / "portal.sqlite3"
            state = work / "state.sqlite3"
            live = write_scenario(work, self.scenario(portal_database=str(portal)))
            live_result = run(live, state, work / "live.json")
            self.assertEqual(live_result["notifications"], 1)
            with sqlite3.connect(state) as connection:
                raw_before = connection.execute("SELECT payload FROM raw_events ORDER BY id").fetchall()
            replay = write_scenario(work, self.scenario(
                name="replay",
                run_id="run-replay",
                options={"replay": True, "mapping_version": 2},
                portal_database=str(portal),
            ))
            replay_result = run(replay, state, work / "replay.json")
            self.assertEqual(replay_result["parcels"], [{"id": "PK1", "state": "delivered"}])
            self.assertEqual(replay_result["notifications"], 0)
            self.assertEqual(replay_result["raw_count"], 2)
            # 通知队列没有新增
            with sqlite3.connect(portal) as connection:
                count = connection.execute("SELECT count(*) FROM notifications").fetchone()[0]
            self.assertEqual(count, 1)
            # 原始接收证据未改变，两个映射版本的投影隔离共存
            with sqlite3.connect(state) as connection:
                raw_after = connection.execute("SELECT payload FROM raw_events ORDER BY id").fetchall()
                versions = connection.execute("SELECT DISTINCT mapping_version FROM projections").fetchall()
            self.assertEqual(raw_before, raw_after)
            self.assertEqual(sorted(version[0] for version in versions), [1, 2])

    def test_unknown_mapping_version_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = write_scenario(work, self.scenario(options={"replay": True, "mapping_version": 99}))
            with self.assertRaises(ValueError):
                run(manifest, work / "state.sqlite3", work / "out.json")


if __name__ == "__main__":
    unittest.main()
