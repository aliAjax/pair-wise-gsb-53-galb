"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested', 'response_received': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}


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
        p["pauses"] = []
        p["paused_days_total"] = 0
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

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
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            pauses = list(p.get("pauses", []))
            pauses.append({"start_day": request_day, "due_day": request_day + allowed_days, "end_day": None, "days": 0, "late_days": 0})
            changes["pauses"] = pauses
            summary = "补件要求已发出，期限计时暂停"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if "response_day" in data:
                response_day = integer(data, "response_day", 0)
            else:
                response_day = int(p["response_day"])
            request_day = int(p["evidence_request_day"])
            due_day = int(p["evidence_due_day"])
            if response_day < request_day:
                raise ValidationError("补件回应日不能早于补件要求日")
            # 等待期间不计入期限：按实际等待天数顺延决定日；补件逾期则从截止日往下算，逾期部分不顺延
            wait_days = min(response_day, due_day) - request_day
            late_days = max(0, response_day - due_day)
            pauses = list(p.get("pauses", []))
            if pauses:
                last = dict(pauses[-1])
                last["end_day"] = response_day
                last["days"] = wait_days
                last["late_days"] = late_days
                pauses[-1] = last
            changes["pauses"] = pauses
            changes["paused_days_total"] = int(p.get("paused_days_total", 0)) + wait_days
            changes["deadline_day"] = int(p["deadline_day"]) + wait_days
            changes["response_day"] = response_day
            changes["days_remaining"] = changes["deadline_day"] - response_day
            changes["overdue"] = changes["days_remaining"] < 0
            changes["evidence_documents"] = docs
            changes["evidence_late_days"] = late_days
            if late_days > 0:
                summary = "补件已回应（逾期%s天），决定期限顺延%s天" % (late_days, wait_days)
            else:
                summary = "补件已回应，决定期限顺延%s天" % wait_days
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
