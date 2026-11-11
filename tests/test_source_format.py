import unittest
from pathlib import Path
from collector_08.adapter import decode


class CarrierWireTests(unittest.TestCase):
    def test_two_clocks_and_offset_survive_parsing(self):
        root = Path(__file__).resolve().parents[1] / "fixtures"
        rows = decode((root / "carrier.xml").read_bytes())
        self.assertTrue(rows[0]["event_at"].endswith("+09:00"))
        self.assertNotEqual(rows[1]["event_at"], rows[1]["received_at"])

    def test_correction_retains_reference(self):
        root = Path(__file__).resolve().parents[1] / "fixtures"
        self.assertEqual(decode((root / "correction.xml").read_bytes())[0]["replaces"], "E2")
