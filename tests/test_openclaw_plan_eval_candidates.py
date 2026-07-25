import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.export_openclaw_plan_eval_candidates import export


class OpenClawPlanEvalCandidateTests(unittest.TestCase):
    def test_exports_imported_plans_once_but_not_activities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = root / "plan.jpg"
            activity = root / "activity.jpg"
            plan.write_bytes(b"plan")
            activity.write_bytes(b"activity")
            state_path = root / "state.json"
            state_path.write_text(json.dumps({
                "plan-id": {"status": "imported", "kind": "plan", "path": str(plan)},
                "activity-id": {"status": "imported", "kind": "activity", "path": str(activity)},
            }))
            output = root / "candidates"

            self.assertEqual(export(state_path, output), 1)
            self.assertEqual(export(state_path, output), 0)
            manifest = json.loads((output / "manifest.json").read_text())

            self.assertEqual(manifest["examples"][0]["upload_id"], "plan-id")
            self.assertEqual(manifest["examples"][0]["label_status"], "pending_review")
            self.assertEqual((output / "plan-id.jpg").read_bytes(), b"plan")
