"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested', 'response_received': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}
ACTIVE_DECISION_STATES = {'submitted', 'response_received'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["original_deadline_day"] = p["deadline_day"]
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["pauses"] = []
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        if action == "decide" and record["state"] == "evidence_requested":
            raise Conflict("补件等待期间不能作出决定")
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    @staticmethod
    def _latest_clock_day(p: Dict[str, Any]) -> int:
        """返回目前已知最晚的业务日：有补件记录取材料收到日，否则取当前处理日。"""
        pauses = p.get("pauses") or []
        if pauses:
            last = pauses[-1]
            if last.get("received_day") is not None:
                return int(last["received_day"])
            return int(last["request_day"])
        return int(p["response_day"])

    @classmethod
    def deadline_view(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """汇总原决定日、各段暂停和当前决定日；等待期间时钟冻结在暂停开始日。"""
        p = payload
        pauses = p.get("pauses") or []
        current = pauses[-1] if pauses and pauses[-1].get("received_day") is None else None
        total_tolled = sum(int(seg.get("tolled_days", 0)) for seg in pauses if seg.get("received_day") is not None)
        total_waited = sum(int(seg.get("wait_days", 0)) for seg in pauses if seg.get("received_day") is not None)
        if current is not None:
            clock_day = int(current["request_day"])
            clock_state = "paused"
            current_deadline = int(p["deadline_day"])
        else:
            clock_day = cls._latest_clock_day(p)
            clock_state = "running"
            current_deadline = int(p["deadline_day"])
        days_remaining = current_deadline - clock_day
        return {
            "original_deadline_day": int(p.get("original_deadline_day", p["deadline_day"])),
            "current_deadline_day": current_deadline,
            "deadline_extended_days": current_deadline - int(p.get("original_deadline_day", p["deadline_day"])),
            "clock_day": clock_day,
            "clock_state": clock_state,
            "days_remaining": days_remaining,
            "overdue": days_remaining < 0,
            "total_tolled_days": total_tolled,
            "total_waited_days": total_waited,
            "pause_count": len(pauses),
            "pauses": pauses,
        }

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", self._latest_clock_day(p))
            if request_day < self._latest_clock_day(p):
                raise ValidationError("暂停开始日不能早于当前处理日")
            allowed_days = integer(data, "allowed_days", 1)
            due_day = request_day + allowed_days
            pauses = list(p.get("pauses") or [])
            if pauses and pauses[-1].get("received_day") is None:
                raise Conflict("上一次补件仍在等待中")
            segment = {
                "sequence": len(pauses) + 1,
                "request_day": request_day,
                "due_day": due_day,
                "allowed_days": allowed_days,
                "received_day": None,
                "wait_days": None,
                "tolled_days": 0,
                "late": False,
                "request": text(data, "evidence_request"),
            }
            pauses.append(segment)
            changes["pauses"] = pauses
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = due_day
            summary = "补件要求已发出，决定期限自第%s天暂停，补件截止第%s天" % (request_day, due_day)
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            response_day = integer(data, "response_day")
            pauses = list(p.get("pauses") or [])
            if not pauses or pauses[-1].get("received_day") is not None:
                raise Conflict("没有等待中的补件通知")
            segment = dict(pauses[-1])
            request_day = int(segment["request_day"])
            if response_day < request_day:
                raise ValidationError("材料收到日不能早于补件通知发出日")
            due_day = int(segment["due_day"])
            wait_days = response_day - request_day
            late = response_day > due_day
            # 实际等待天数顺延决定日；补件逾期时只顺延到截止日，截止日后继续消耗决定天数
            tolled_days = min(wait_days, int(segment["allowed_days"]))
            segment.update({"received_day": response_day, "wait_days": wait_days, "tolled_days": tolled_days, "late": late})
            pauses[-1] = segment
            new_deadline = int(p["deadline_day"]) + tolled_days
            changes["pauses"] = pauses
            changes["deadline_day"] = new_deadline
            changes["response_day"] = response_day
            changes["evidence_documents"] = docs
            changes["days_remaining"] = new_deadline - response_day
            changes["overdue"] = changes["days_remaining"] < 0
            if late:
                summary = "补件逾期%s天收到，决定期限顺延%s天至第%s天，逾期天数继续计时" % (response_day - due_day, tolled_days, new_deadline)
            else:
                summary = "补件已收到，实际等待%s天，决定期限顺延至第%s天并继续计时" % (wait_days, new_deadline)
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
