import unittest
from collector_08.projection import MAPPINGS, build_projection, format_instant, parse_instant


def row(rowid, event_id, code, event_at, received_at, authority="carrier", parcel="PK1", replaces="", source="/carrier"):
    return {
        "rowid": rowid,
        "source": source,
        "authority": authority,
        "event_id": event_id,
        "parcel": parcel,
        "code": code,
        "event_at": event_at,
        "received_at": received_at,
        "replaces": replaces,
    }


class InstantTests(unittest.TestCase):
    def test_offsets_normalize_to_utc(self):
        self.assertEqual(format_instant(parse_instant("2026-11-11T12:00:00+09:00")), "2026-11-11T03:00:00Z")

    def test_naive_instant_treated_as_utc(self):
        self.assertEqual(format_instant(parse_instant("2026-11-11T03:00:00")), "2026-11-11T03:00:00Z")

    def test_bad_instant_rejected(self):
        with self.assertRaises(ValueError):
            parse_instant("not-a-time")


class ProjectionTests(unittest.TestCase):
    def test_event_time_beats_receipt_order(self):
        rows = [
            row(1, "E2", "DELIVERED", "2026-11-11T12:00:00+09:00", "2026-11-11T04:00:00Z"),
            row(2, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-12T01:00:00Z"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "delivered"}])
        self.assertEqual([entry["event"] for entry in result["timeline"]], ["E1", "E2"])
        self.assertEqual(result["timeline"][1]["event_at"], "2026-11-11T03:00:00Z")
        self.assertEqual(result["timeline"][1]["received_at"], "2026-11-11T04:00:00Z")

    def test_same_authority_same_instant_terminal_conflict(self):
        rows = [
            row(1, "E2", "DELIVERED", "2026-11-11T12:00:00+09:00", "2026-11-11T04:00:00Z"),
            row(2, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-12T01:00:00Z"),
            row(3, "E3", "RETURNED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "conflict"}])
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["parcel"], "PK1")
        self.assertEqual(conflict["events"], ["E2", "E3"])
        self.assertEqual(conflict["reason"], "terminal_disagreement")
        # 冲突事件保留在时间线中作为证据，不被丢弃
        self.assertEqual(len(result["timeline"]), 3)

    def test_conflict_not_decided_by_input_order(self):
        first = build_projection([
            row(1, "E2", "DELIVERED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z"),
            row(2, "E3", "RETURNED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z"),
        ], 1)
        second = build_projection([
            row(1, "E3", "RETURNED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z"),
            row(2, "E2", "DELIVERED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z"),
        ], 1)
        self.assertEqual(first["parcels"], second["parcels"])
        self.assertEqual(first["conflicts"], second["conflicts"])

    def test_correction_supersedes_same_authority_event(self):
        rows = [
            row(1, "E2", "DELIVERED", "2026-11-11T12:00:00+09:00", "2026-11-11T04:00:00Z"),
            row(2, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-12T01:00:00Z"),
            row(3, "E4", "TRANSIT", "2026-11-11T03:00:00Z", "2026-11-13T01:00:00Z", replaces="E2"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])
        self.assertEqual(result["superseded"], ["E2"])
        self.assertEqual([entry["event"] for entry in result["timeline"]], ["E1", "E4"])

    def test_correction_chain_supersedes_each_hop(self):
        rows = [
            row(1, "E2", "DELIVERED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z"),
            row(2, "E4", "RETURNED", "2026-11-11T05:00:00Z", "2026-11-11T06:00:00Z", replaces="E2"),
            row(3, "E5", "TRANSIT", "2026-11-11T07:00:00Z", "2026-11-11T08:00:00Z", replaces="E4"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["superseded"], ["E2", "E4"])
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])

    def test_correction_cycle_isolated(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E5", "DELIVERED", "2026-11-11T04:00:00Z", "2026-11-11T05:00:00Z", replaces="E6"),
            row(3, "E6", "RETURNED", "2026-11-11T04:30:00Z", "2026-11-11T06:00:00Z", replaces="E5"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["superseded"], [])
        self.assertEqual({entry["event"] for entry in result["isolated"]}, {"E5", "E6"})
        self.assertTrue(all(entry["reason"] == "replaces_cycle" for entry in result["isolated"]))
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])

    def test_self_correction_is_cycle(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E5", "DELIVERED", "2026-11-11T04:00:00Z", "2026-11-11T05:00:00Z", replaces="E5"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["superseded"], [])
        self.assertEqual([entry["reason"] for entry in result["isolated"]], ["replaces_cycle"])
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])

    def test_correction_to_missing_target_isolated(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E7", "DELIVERED", "2026-11-11T04:00:00Z", "2026-11-11T05:00:00Z", replaces="E9"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["superseded"], [])
        self.assertEqual([entry["reason"] for entry in result["isolated"]], ["replaces_missing"])
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])

    def test_correction_cannot_cross_authority(self):
        rows = [
            row(1, "E2", "DELIVERED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z", authority="carrier"),
            row(2, "E8", "TRANSIT", "2026-11-11T05:00:00Z", "2026-11-11T06:00:00Z", authority="platform", replaces="E2"),
        ]
        result = build_projection(rows, 1, {"carrier": 1, "platform": 9})
        self.assertEqual(result["superseded"], [])
        self.assertEqual([entry["reason"] for entry in result["isolated"]], ["replaces_missing"])
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "delivered"}])

    def test_authority_weight_adjudicates_cross_authority(self):
        rows = [
            row(1, "E2", "DELIVERED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z", authority="carrier"),
            row(2, "E3", "RETURNED", "2026-11-11T03:00:00Z", "2026-11-11T04:00:00Z", authority="platform"),
        ]
        weighted = build_projection(rows, 1, {"carrier": 1, "platform": 9})
        self.assertEqual(weighted["parcels"], [{"id": "PK1", "state": "returned"}])
        self.assertEqual(weighted["conflicts"], [])
        tied = build_projection(rows, 1, {"carrier": 5, "platform": 5})
        self.assertEqual(tied["parcels"], [{"id": "PK1", "state": "conflict"}])
        self.assertEqual(tied["conflicts"][0]["reason"], "authority_tie")

    def test_duplicate_scan_merges_without_double_count(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:30:00Z", source="/carrier2"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(len(result["timeline"]), 1)
        self.assertEqual(result["timeline"][0]["copies"], 2)
        self.assertEqual(result["timeline"][0]["received_at"], "2026-11-11T03:00:00Z")

    def test_divergent_versions_flagged_and_kept(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E1", "DELIVERED", "2026-11-11T04:00:00Z", "2026-11-11T05:00:00Z"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(len(result["timeline"]), 2)
        self.assertEqual(result["conflicts"][0]["reason"], "divergent_versions")
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "conflict"}])

    def test_terminal_beats_transit_at_same_instant(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E2", "DELIVERED", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "delivered"}])
        self.assertEqual(result["conflicts"], [])

    def test_mapping_versions_semantics_and_registration(self):
        rows = [row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z")]
        self.assertIn(1, MAPPINGS)
        self.assertIn(2, MAPPINGS)
        self.assertEqual(build_projection(rows, 1), build_projection(rows, 2))
        with self.assertRaises(ValueError):
            build_projection(rows, 99)

    def test_unmapped_code_isolated(self):
        rows = [
            row(1, "E1", "TRANSIT", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"),
            row(2, "E9", "LOST", "2026-11-11T04:00:00Z", "2026-11-11T05:00:00Z"),
        ]
        result = build_projection(rows, 1)
        self.assertEqual(result["parcels"], [{"id": "PK1", "state": "transit"}])
        self.assertEqual([entry["reason"] for entry in result["isolated"]], ["unmapped_code"])


if __name__ == "__main__":
    unittest.main()
