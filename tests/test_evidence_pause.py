import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
INTAKE = Actor("creator", "intake_officer")
OFFICER = Actor("officer", "case_officer")
LEGAL_REP = Actor("operator", "legal_rep")


class EvidencePauseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _submitted_record(self):
        record = self.service.create(INTAKE, "IMM-29001", CREATE_DATA)
        return self.service.act(LEGAL_REP, record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})

    def _request_evidence(self, record, request_day=115, allowed_days=10):
        return self.service.act(OFFICER, record["id"], record["version"], "request_evidence", {"evidence_request_day": request_day, "allowed_days": allowed_days, "evidence_request": "补充收入证明"})

    def _respond(self, record, response_day):
        return self.service.act(LEGAL_REP, record["id"], record["version"], "respond", {"response_day": response_day, "documents": ["income_proof"]})

    def test_pause_extends_deadline_by_actual_wait(self):
        record = self._submitted_record()
        self.assertEqual(record["payload"]["original_deadline_day"], 130)
        self.assertEqual(record["payload"]["deadline_day"], 130)
        record = self._request_evidence(record)
        self.assertEqual(record["payload"]["evidence_request_day"], 115)
        self.assertEqual(record["payload"]["evidence_due_day"], 125)
        self.assertEqual(record["payload"]["pauses"], [{"start_day": 115, "due_day": 125, "end_day": None, "days": 0, "late_days": 0}])
        record = self._respond(record, 120)
        payload = record["payload"]
        self.assertEqual(payload["deadline_day"], 135)
        self.assertEqual(payload["original_deadline_day"], 130)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertFalse(payload["overdue"])
        self.assertEqual(payload["pauses"], [{"start_day": 115, "due_day": 125, "end_day": 120, "days": 5, "late_days": 0}])
        self.assertEqual(payload["paused_days_total"], 5)

    def test_decide_blocked_while_waiting(self):
        record = self._submitted_record()
        record = self._request_evidence(record)
        self.assertEqual(record["state"], "evidence_requested")
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})

    def test_late_response_counts_from_due_day(self):
        record = self._submitted_record()
        record = self._request_evidence(record)
        record = self._respond(record, 130)
        payload = record["payload"]
        self.assertEqual(record["state"], "response_received")
        self.assertEqual(payload["deadline_day"], 140)
        self.assertEqual(payload["pauses"][0]["days"], 10)
        self.assertEqual(payload["pauses"][0]["end_day"], 130)
        self.assertEqual(payload["pauses"][0]["late_days"], 5)
        self.assertEqual(payload["evidence_late_days"], 5)
        self.assertEqual(payload["paused_days_total"], 10)

    def test_multiple_pauses_accumulate(self):
        record = self._submitted_record()
        record = self._request_evidence(record)
        record = self._respond(record, 120)
        record = self._request_evidence(record, request_day=122, allowed_days=5)
        self.assertEqual(record["state"], "evidence_requested")
        record = self._respond(record, 124)
        payload = record["payload"]
        self.assertEqual(payload["original_deadline_day"], 130)
        self.assertEqual(payload["deadline_day"], 137)
        self.assertEqual(payload["paused_days_total"], 7)
        self.assertEqual(len(payload["pauses"]), 2)
        self.assertEqual(payload["pauses"][0], {"start_day": 115, "due_day": 125, "end_day": 120, "days": 5, "late_days": 0})
        self.assertEqual(payload["pauses"][1], {"start_day": 122, "due_day": 127, "end_day": 124, "days": 2, "late_days": 0})
        self.assertEqual(payload["days_remaining"], 13)

    def test_pause_data_survives_restart(self):
        record = self._submitted_record()
        record = self._request_evidence(record)
        record = self._respond(record, 120)
        reopened = build_service(self.db_path)
        fetched = reopened.get_record(INTAKE, record["id"])
        self.assertEqual(fetched["payload"]["original_deadline_day"], 130)
        self.assertEqual(fetched["payload"]["deadline_day"], 135)
        self.assertEqual(fetched["payload"]["pauses"], [{"start_day": 115, "due_day": 125, "end_day": 120, "days": 5, "late_days": 0}])
        self.assertEqual(fetched["payload"]["paused_days_total"], 5)

    def test_stats_distinguish_paused_and_overdue(self):
        record = self._submitted_record()
        record = self._request_evidence(record)
        stats = self.service.stats(INTAKE)
        self.assertEqual(stats["evidence_requested"], 1)
        self.assertEqual(stats["paused"], 1)
        self.assertEqual(stats["overdue"], 0)
        record = self._respond(record, 120)
        stats = self.service.stats(INTAKE)
        self.assertEqual(stats["paused"], 0)
        self.assertEqual(stats["paused_days_total"], 5)
        self.assertEqual(stats["response_received"], 1)


if __name__ == "__main__":
    unittest.main()
