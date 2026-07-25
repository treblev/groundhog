import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import openclaw_deliver_outbox


class OpenClawDeliverOutboxTests(unittest.TestCase):
    def test_format_message_prefers_payload_message(self):
        item = {
            "event_type": "stock_alert_created",
            "payload": '{"message": "EXC: Golden Cross", "ticker": "EXC"}',
        }

        self.assertEqual(
            openclaw_deliver_outbox._format_message(item),
            "EXC: Golden Cross",
        )

    def test_format_message_falls_back_to_event_details(self):
        item = {
            "event_type": "stock_alert_created",
            "payload": '{"ticker": "EXC", "alert_type": "golden_cross", "date": "2026-07-23"}',
        }

        self.assertEqual(
            openclaw_deliver_outbox._format_message(item),
            "Groundhog stock_alert_created (EXC, golden_cross, 2026-07-23)",
        )


if __name__ == "__main__":
    unittest.main()
