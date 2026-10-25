import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.benchmark_answer_review import decision_verdict, load_cases, summarize


class AnswerReviewBenchmarkTests(unittest.TestCase):
    def response(self, grounding="supported", disclosure="user_facing", confidence=0.9):
        return {"answers": {
            "grounding": {"choice": grounding, "confidence": confidence},
            "disclosure": {"choice": disclosure, "confidence": confidence},
        }}

    def test_uncertain_and_low_confidence_answers_escalate(self):
        self.assertEqual(decision_verdict(self.response(), 0.6)["action"], "accept")
        for response in (self.response(confidence=0.5), self.response(grounding="uncertain"), self.response(disclosure="uncertain")):
            self.assertEqual(decision_verdict(response, 0.6)["action"], "escalate")
        self.assertEqual(decision_verdict(self.response(grounding="unsupported"), 0.6)["action"], "correct")
        self.assertEqual(decision_verdict(self.response(disclosure="internal_disclosure"), 0.6)["action"], "correct")

    def test_malformed_choices_and_confidence_cannot_accept(self):
        for response in (self.response(grounding="invented"), self.response(confidence=float("nan")), self.response(confidence=True), self.response(confidence=1.1)):
            with self.subTest(response=response), self.assertRaises(ValueError):
                decision_verdict(response, 0.6)

    def test_one_confident_violation_is_enough_to_request_correction(self):
        response = self.response(grounding="unsupported")
        response["answers"]["disclosure"] = {"choice":"uncertain", "confidence":0.1}
        self.assertEqual(decision_verdict(response, 0.6)["action"], "correct")

    def test_summary_separates_errors_escalations_and_wrong_decisions(self):
        rows = []
        for action, expected, error in (("accept", True, None), ("accept", False, None), ("correct", True, None), ("escalate", True, None), ("correct", False, "timeout")):
            rows.append({"engine":"decision", "expected_grounded":expected, "expected_safe":True,
                         "seconds":1, "error":error,
                         "verdict":{"grounded":True, "safe":True, "action":action}})
        result = summarize(rows)["decision"]
        self.assertEqual(result["false_accepts"], 1)
        self.assertEqual(result["false_rejections"], 1)
        self.assertEqual(result["escalations"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["decisive_correct"], 1)

    def test_cases_require_unique_ids_and_explicit_gold_labels(self):
        case = {"id":"example", "question":"Question", "answer":"Answer", "evidence":{}, "expected_grounded":True, "expected_safe":True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"cases.json"
            path.write_text(json.dumps([case]))
            self.assertEqual(load_cases(path), [case])
            for rows in ([case,case], [{**case, "expected_grounded":"true"}], []):
                path.write_text(json.dumps(rows))
                with self.assertRaises(ValueError):
                    load_cases(path)
