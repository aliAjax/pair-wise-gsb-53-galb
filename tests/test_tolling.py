import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'applicant_id': 'A-901', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport']}


class TollingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _to_submitted(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-30001", CREATE_DATA)
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport"]})
        return record

    def test_clock_freezes_while_waiting_for_evidence(self):
        record = self._to_submitted()
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"},
        )
        view = record["deadline"]
        self.assertEqual(record["state"], "evidence_requested")
        self.assertEqual(view["clock_state"], "paused")
        # 时钟冻结在暂停开始日115，剩余天数固定为15，不因等待而减少
        self.assertEqual(view["clock_day"], 115)
        self.assertEqual(view["days_remaining"], 15)
        self.assertFalse(view["overdue"])
        segment = view["pauses"][-1]
        self.assertEqual(segment["request_day"], 115)
        self.assertEqual(segment["due_day"], 125)
        self.assertIsNone(segment["received_day"])
        # 等待期间经办人不能作决定
        with self.assertRaises(Conflict):
            self.service.act(
                Actor("officer", "case_officer"), record["id"], record["version"],
                "decide", {"decision": "granted", "decision_reason": "材料充分"},
            )

    def test_on_time_response_extends_by_actual_wait(self):
        record = self._to_submitted()
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"},
        )
        record = self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"],
            "respond", {"response_day": 120, "documents": ["income_proof"]},
        )
        view = record["deadline"]
        self.assertEqual(view["clock_state"], "running")
        self.assertEqual(view["original_deadline_day"], 130)
        self.assertEqual(view["current_deadline_day"], 135)
        self.assertEqual(view["total_tolled_days"], 5)
        self.assertEqual(view["days_remaining"], 15)
        segment = view["pauses"][-1]
        self.assertFalse(segment["late"])
        self.assertEqual(segment["wait_days"], 5)
        self.assertEqual(segment["tolled_days"], 5)
        # 恢复计时后可以决定
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "decide", {"decision": "granted", "decision_reason": "材料充分"},
        )
        self.assertEqual(record["state"], "decided")

    def test_late_response_tolls_only_until_due_day(self):
        record = self._to_submitted()
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"},
        )
        # 截止日125，材料第128天才收到，逾期3天
        record = self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"],
            "respond", {"response_day": 128, "documents": ["income_proof"]},
        )
        view = record["deadline"]
        self.assertEqual(view["original_deadline_day"], 130)
        self.assertEqual(view["current_deadline_day"], 140)
        segment = view["pauses"][-1]
        self.assertTrue(segment["late"])
        self.assertEqual(segment["wait_days"], 13)
        self.assertEqual(segment["tolled_days"], 10)
        # 截止日后继续消耗决定天数：140 - 128 = 12
        self.assertEqual(view["days_remaining"], 12)
        stats = self.service.stats(Actor("creator", "intake_officer"))
        self.assertEqual(stats["overdue_evidence_responses"], 1)
        self.assertEqual(stats["total_tolled_days"], 10)

    def test_multiple_pauses_accumulate_and_keep_original_deadline(self):
        record = self._to_submitted()
        # 第一段：115发出，窗口10天，120收到 -> 顺延5天，决定日135
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "第一次补件"},
        )
        record = self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"],
            "respond", {"response_day": 120, "documents": ["doc_a"]},
        )
        # 第二段：126发出，窗口8天，130收到 -> 顺延4天，决定日139
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 126, "allowed_days": 8, "evidence_request": "第二次补件"},
        )
        self.assertEqual(record["deadline"]["pause_count"], 2)
        self.assertEqual(record["deadline"]["clock_state"], "paused")
        record = self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"],
            "respond", {"response_day": 130, "documents": ["doc_b"]},
        )
        view = record["deadline"]
        self.assertEqual(view["original_deadline_day"], 130)
        self.assertEqual(view["current_deadline_day"], 139)
        self.assertEqual(view["total_tolled_days"], 9)
        self.assertEqual([seg["sequence"] for seg in view["pauses"]], [1, 2])
        stats = self.service.stats(Actor("creator", "intake_officer"))
        self.assertEqual(stats["multi_pause_cases"], 1)
        self.assertEqual(stats["paused_for_evidence"], 0)

    def test_details_survive_service_restart(self):
        record = self._to_submitted()
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"},
        )
        record = self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"],
            "respond", {"response_day": 120, "documents": ["income_proof"]},
        )
        record_id = record["id"]
        # 服务重开
        reopened = build_service(self.db_path)
        loaded = reopened.get_record(Actor("officer", "case_officer"), record_id)
        view = loaded["deadline"]
        self.assertEqual(view["original_deadline_day"], 130)
        self.assertEqual(view["current_deadline_day"], 135)
        self.assertEqual(view["pauses"][0]["request_day"], 115)
        self.assertEqual(view["pauses"][0]["due_day"], 125)
        self.assertEqual(view["pauses"][0]["received_day"], 120)
        timeline = reopened.timeline(Actor("officer", "case_officer"), record_id)
        tolling_events = [event for event in timeline if event["action"] in {"request_evidence", "respond"}]
        self.assertEqual(tolling_events[0]["details"]["deadline"]["clock_state"], "paused")
        self.assertEqual(tolling_events[1]["details"]["deadline"]["current_deadline_day"], 135)

    def test_request_day_before_current_clock_rejected(self):
        record = self._to_submitted()
        record = self.service.act(
            Actor("officer", "case_officer"), record["id"], record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补件"},
        )
        record = self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"],
            "respond", {"response_day": 120, "documents": ["doc_a"]},
        )
        with self.assertRaises(ValidationError):
            self.service.act(
                Actor("officer", "case_officer"), record["id"], record["version"],
                "request_evidence", {"evidence_request_day": 110, "allowed_days": 5, "evidence_request": "日期倒灌"},
            )
