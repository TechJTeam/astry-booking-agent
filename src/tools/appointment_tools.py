"""WRITE tools (UC-AIBK-03 final / UC-AIBK-05): tạo, đổi lịch, huỷ, xác nhận/từ chối
appointment. Pattern confirm-gate 2 lượt gọi (giống DB-Agent): lần đầu (confirm=false) chỉ
preview, KHÔNG gọi API ghi; LLM phải hỏi lại Staff; lần gọi thứ 2 (confirm=true) mới thật sự
POST/PATCH vào astry-pos-be.

Final conflict check KHÔNG cần tool tự làm — Postgres EXCLUDE constraint ở astry-pos-be là
nguồn chống double-booking authoritative tại thời điểm ghi; tool chỉ bắt lỗi 409/412 và trả lời
tự nhiên để LLM quay lại đề xuất slot khác (UC-AIBK-04). Khái niệm "Soft Hold 2 phút" trong SRS
không có cơ chế tương ứng ở astry-pos-be — được thay thế bởi gọi POST /appointments ngay khi
Staff xác nhận (tương tác chat vốn tuần tự, 1 turn)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field
from vanna.core.tool import Tool, ToolContext, ToolResult

from src.pos_client import PosApiError, get_pos_client
from src.tools.common import pending_confirmation_result


def _format_conflict(e: PosApiError) -> str:
    details = e.body.get("details") or {}
    suggestions = details.get("suggestions") or []
    if not suggestions:
        return f"Lịch bị trùng (conflict): {details.get('conflicts')}. Hãy chọn khung giờ khác."
    lines: List[str] = []
    for s in suggestions[:3]:
        if s.get("type") == "ALTERNATIVE_SLOT":
            lines.append(f"- Đổi giờ: {s['startTime']}–{s['endTime']} (cùng staffId={s['staffId']})")
        elif s.get("type") == "ALTERNATIVE_STAFF":
            lines.append(f"- Đổi nhân viên staffId={s['staffId']}: {s['startTime']}–{s['endTime']}")
    return "Khung giờ vừa chọn đã bị trùng lịch. Gợi ý thay thế:\n" + "\n".join(lines)


class ServiceLineArgs(BaseModel):
    service_id: str = Field(description="serviceId (uuid) đã resolve qua lookup_service")
    staff_id: str = Field(description="staffId (uuid) được chọn cho dịch vụ này")
    parallel_group: Optional[str] = None


def _service_lines_payload(services: List[ServiceLineArgs]) -> List[Dict[str, Any]]:
    return [
        {"serviceId": s.service_id, "staffId": s.staff_id, "parallelGroup": s.parallel_group}
        for s in services
    ]


# --------------------------------------------------------------------------- create


class CreateAppointmentArgs(BaseModel):
    booking_date: str = Field(description="YYYY-MM-DD")
    start_time: str = Field(description="HH:mm, giờ local salon")
    services: List[ServiceLineArgs] = Field(description="1-6 dịch vụ, mỗi dịch vụ 1 staff")
    source: str = Field(default="CALL_IN", description="WALK_IN|CALL_IN|ONLINE|WAITLIST_CONVERTED|MOBILE")
    customer_id: Optional[str] = None
    guest_name: Optional[str] = None
    guest_phone: Optional[str] = None
    notes: Optional[str] = None
    from_waitlist_entry_id: Optional[str] = None
    confirm: bool = Field(default=False, description="True = xác nhận thật, gọi POST /appointments")


class CreateAppointmentTool(Tool[CreateAppointmentArgs]):
    @property
    def name(self) -> str:
        return "create_appointment"

    @property
    def description(self) -> str:
        return (
            "Tạo lịch hẹn mới (POST /appointments). Luôn gọi check_availability trước để có "
            "startTime/staffId hợp lệ. Lần gọi đầu (confirm=false) chỉ trả bản preview, KHÔNG ghi gì — "
            "phải hỏi lại Staff xác nhận rồi mới gọi lại với confirm=true."
        )

    def get_args_schema(self) -> Type[CreateAppointmentArgs]:
        return CreateAppointmentArgs

    async def execute(self, context: ToolContext, args: CreateAppointmentArgs) -> ToolResult:
        if not args.confirm:
            return pending_confirmation_result(
                "Tạo lịch hẹn mới",
                {
                    "Ngày": args.booking_date,
                    "Giờ": args.start_time,
                    "Số dịch vụ": len(args.services),
                    "Khách": args.guest_name or args.customer_id,
                    "Ghi chú": args.notes,
                },
            )

        payload: Dict[str, Any] = {
            "source": args.source,
            "bookingDate": args.booking_date,
            "startTime": args.start_time,
            "services": _service_lines_payload(args.services),
        }
        if args.customer_id:
            payload["customerId"] = args.customer_id
        if args.guest_name or args.guest_phone:
            payload["guest"] = {"name": args.guest_name, "phone": args.guest_phone}
        if args.notes:
            payload["notes"] = args.notes
        if args.from_waitlist_entry_id:
            payload["fromWaitlistEntryId"] = args.from_waitlist_entry_id

        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            appt = await client.create_appointment(jwt=jwt, payload=payload)
        except PosApiError as e:
            if e.status_code == 409 and e.code == "BOOKING_CONFLICT":
                return ToolResult(
                    success=False,
                    result_for_llm=_format_conflict(e),
                    error="booking_conflict",
                    metadata={"details": e.body.get("details")},
                )
            if e.status_code == 422:
                return ToolResult(
                    success=False, result_for_llm=f"Dữ liệu không hợp lệ: {e.body}", error=e.code or "invalid_request"
                )
            return ToolResult(
                success=False, result_for_llm=f"Lỗi tạo lịch hẹn: {e.body}", error=e.code or "pos_api_error"
            )

        return ToolResult(
            success=True,
            result_for_llm=f"Đã tạo lịch hẹn id={appt['id']} lúc {appt['startTime']} (trạng thái {appt['status']}).",
            metadata={"appointment": appt},
        )


# ------------------------------------------------------------------------ reschedule


class RescheduleAppointmentArgs(BaseModel):
    appointment_id: str
    modify_reason: str = Field(description="CUSTOMER_REQUEST|STAFF_BUSY|SYSTEM_ERROR|OTHER")
    modify_reason_note: Optional[str] = None
    booking_date: Optional[str] = None
    start_time: Optional[str] = None
    services: Optional[List[ServiceLineArgs]] = None
    notes: Optional[str] = None
    confirm: bool = Field(default=False)


class RescheduleAppointmentTool(Tool[RescheduleAppointmentArgs]):
    @property
    def name(self) -> str:
        return "reschedule_appointment"

    @property
    def description(self) -> str:
        return (
            "Đổi giờ/ngày/dịch vụ/nhân viên của lịch hẹn đã tồn tại (PATCH /appointments/:id). "
            "Tự động GET lịch hẹn trước để lấy ETag (updatedAt) cho If-Match — nếu lịch đã bị đổi "
            "bởi nơi khác giữa lúc đọc và lúc ghi, backend trả 412 và tool sẽ báo Staff thử lại."
        )

    def get_args_schema(self) -> Type[RescheduleAppointmentArgs]:
        return RescheduleAppointmentArgs

    async def execute(self, context: ToolContext, args: RescheduleAppointmentArgs) -> ToolResult:
        if not args.confirm:
            return pending_confirmation_result(
                f"Đổi lịch hẹn {args.appointment_id}",
                {"Ngày mới": args.booking_date, "Giờ mới": args.start_time, "Lý do": args.modify_reason},
            )

        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            _current, etag = await client.get_appointment(jwt=jwt, appointment_id=args.appointment_id)
        except PosApiError as e:
            return ToolResult(
                success=False,
                result_for_llm=f"Không tìm thấy lịch hẹn {args.appointment_id}: {e.body}",
                error=e.code or "appointment_not_found",
            )

        payload: Dict[str, Any] = {"modifyReason": args.modify_reason}
        if args.modify_reason_note:
            payload["modifyReasonNote"] = args.modify_reason_note
        if args.booking_date:
            payload["bookingDate"] = args.booking_date
        if args.start_time:
            payload["startTime"] = args.start_time
        if args.services:
            payload["services"] = _service_lines_payload(args.services)
        if args.notes is not None:
            payload["notes"] = args.notes

        try:
            appt = await client.patch_appointment(
                jwt=jwt, appointment_id=args.appointment_id, payload=payload, if_match=etag
            )
        except PosApiError as e:
            if e.status_code == 412:
                return ToolResult(
                    success=False,
                    result_for_llm="Lịch hẹn vừa bị người khác thay đổi, hãy thử lại thao tác.",
                    error="stale_resource",
                )
            if e.status_code == 409 and e.code == "BOOKING_CONFLICT":
                return ToolResult(
                    success=False,
                    result_for_llm=_format_conflict(e),
                    error="booking_conflict",
                    metadata={"details": e.body.get("details")},
                )
            if e.status_code == 409:
                return ToolResult(
                    success=False, result_for_llm=f"Không thể đổi lịch: {e.body}", error=e.code or "invalid_state_transition"
                )
            if e.status_code == 422:
                return ToolResult(
                    success=False, result_for_llm=f"Dữ liệu không hợp lệ: {e.body}", error=e.code or "invalid_request"
                )
            return ToolResult(
                success=False, result_for_llm=f"Lỗi đổi lịch hẹn: {e.body}", error=e.code or "pos_api_error"
            )

        return ToolResult(
            success=True,
            result_for_llm=f"Đã đổi lịch hẹn {appt['id']} sang {appt['startTime']}.",
            metadata={"appointment": appt},
        )


# ----------------------------------------------------------------------------- cancel


CANCELLATION_REASON_CODES = (
    "CUSTOMER_NO_SHOW",
    "CLIENT_CHANGED_MIND",
    "SCHEDULE_CONFLICT",
    "EMERGENCY",
    "INCORRECT_BOOKING_DETAILS",
    "STAFF_UNAVAILABLE",
    "OTHER",
)


class CancelAppointmentArgs(BaseModel):
    appointment_id: str
    reason_code: Optional[str] = Field(
        default=None, description="CUSTOMER_NO_SHOW|CLIENT_CHANGED_MIND|SCHEDULE_CONFLICT|EMERGENCY|"
        "INCORRECT_BOOKING_DETAILS|STAFF_UNAVAILABLE|OTHER"
    )
    reason_note: Optional[str] = None
    confirm: bool = Field(default=False)


class CancelAppointmentTool(Tool[CancelAppointmentArgs]):
    @property
    def name(self) -> str:
        return "cancel_appointment"

    @property
    def description(self) -> str:
        return (
            "Huỷ lịch hẹn (POST /appointments/:id/cancel). Bắt buộc reasonCode hoặc reasonNote "
            "(nếu reasonCode='OTHER' kèm reasonNote thì reasonNote phải >= 5 ký tự)."
        )

    def get_args_schema(self) -> Type[CancelAppointmentArgs]:
        return CancelAppointmentArgs

    async def execute(self, context: ToolContext, args: CancelAppointmentArgs) -> ToolResult:
        if not args.reason_code and not (args.reason_note and args.reason_note.strip()):
            return ToolResult(
                success=False,
                result_for_llm="Cần lý do huỷ (reasonCode hoặc reasonNote) trước khi huỷ lịch — hãy hỏi Staff lý do.",
                error="cancellation_reason_required",
            )
        if args.reason_code and args.reason_code not in CANCELLATION_REASON_CODES:
            return ToolResult(
                success=False,
                result_for_llm=f"reasonCode không hợp lệ. Chọn 1 trong: {', '.join(CANCELLATION_REASON_CODES)}.",
                error="invalid_reason_code",
            )

        if not args.confirm:
            return pending_confirmation_result(
                f"Huỷ lịch hẹn {args.appointment_id}",
                {"Lý do": args.reason_code, "Ghi chú": args.reason_note},
            )

        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            appt = await client.cancel_appointment(
                jwt=jwt, appointment_id=args.appointment_id, reason_code=args.reason_code, reason_note=args.reason_note
            )
        except PosApiError as e:
            if e.status_code == 422:
                return ToolResult(
                    success=False, result_for_llm=f"Lý do huỷ không hợp lệ: {e.body}", error=e.code or "invalid_request"
                )
            if e.status_code == 409:
                return ToolResult(
                    success=False,
                    result_for_llm=f"Không thể huỷ ở trạng thái hiện tại: {e.body}",
                    error=e.code or "invalid_state_transition",
                )
            return ToolResult(
                success=False, result_for_llm=f"Lỗi huỷ lịch hẹn: {e.body}", error=e.code or "pos_api_error"
            )

        return ToolResult(success=True, result_for_llm=f"Đã huỷ lịch hẹn {appt['id']}.", metadata={"appointment": appt})


# --------------------------------------------------------------- confirm / decline pending


class ConfirmOrDeclinePendingArgs(BaseModel):
    appointment_id: str
    decision: str = Field(description="'CONFIRM' hoặc 'DECLINE'")
    decline_reason: Optional[str] = None
    confirm: bool = Field(default=False)


class ConfirmOrDeclinePendingTool(Tool[ConfirmOrDeclinePendingArgs]):
    @property
    def name(self) -> str:
        return "confirm_or_decline_pending_appointment"

    @property
    def description(self) -> str:
        return (
            "Xác nhận (POST /appointments/:id/confirm) hoặc từ chối (POST /appointments/:id/decline) "
            "một lịch hẹn đang ở trạng thái PENDING_CONFIRMATION (thường đến từ Mobile)."
        )

    def get_args_schema(self) -> Type[ConfirmOrDeclinePendingArgs]:
        return ConfirmOrDeclinePendingArgs

    async def execute(self, context: ToolContext, args: ConfirmOrDeclinePendingArgs) -> ToolResult:
        if args.decision not in ("CONFIRM", "DECLINE"):
            return ToolResult(
                success=False, result_for_llm="decision phải là 'CONFIRM' hoặc 'DECLINE'.", error="invalid_decision"
            )

        if not args.confirm:
            return pending_confirmation_result(
                f"{args.decision} lịch hẹn {args.appointment_id}",
                {"Lý do từ chối (nếu có)": args.decline_reason},
            )

        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            if args.decision == "CONFIRM":
                appt = await client.confirm_appointment(jwt=jwt, appointment_id=args.appointment_id)
            else:
                appt = await client.decline_appointment(
                    jwt=jwt, appointment_id=args.appointment_id, reason=args.decline_reason
                )
        except PosApiError as e:
            if e.status_code == 409:
                return ToolResult(
                    success=False,
                    result_for_llm=f"Không thể {args.decision.lower()} ở trạng thái hiện tại: {e.body}",
                    error=e.code or "invalid_status_transition",
                )
            return ToolResult(
                success=False, result_for_llm=f"Lỗi xử lý lịch hẹn: {e.body}", error=e.code or "pos_api_error"
            )

        return ToolResult(
            success=True,
            result_for_llm=f"Lịch hẹn {appt['id']} hiện ở trạng thái {appt['status']}.",
            metadata={"appointment": appt},
        )
