"""READ tool (UC-AIBK-03): check slot trống + gợi ý staff theo Fair Turn. Fair Turn đã được
tính sẵn trong response của astry-pos-be (statusBadge=FAIR_TURN_NEXT trên rankedStaff) — tool
này KHÔNG tự implement lại thuật toán Fair Turn, chỉ đọc kết quả từ FairTurnEngine."""
from __future__ import annotations

from typing import List, Optional, Type

from pydantic import BaseModel, Field
from vanna.core.tool import Tool, ToolContext, ToolResult

from src.pos_client import PosApiError, get_pos_client


class CheckAvailabilityArgs(BaseModel):
    date: str = Field(description="Ngày muốn đặt, định dạng YYYY-MM-DD")
    service_ids: List[str] = Field(description="Danh sách serviceId (uuid) đã resolve qua lookup_service")
    duration_min: int = Field(
        description="Tổng thời lượng (phút), 15-300, thường bằng tổng baseDurationMinutes các dịch vụ"
    )
    staff_id: Optional[str] = Field(
        default=None, description="Ghim theo 1 staffId cụ thể nếu khách yêu cầu đích danh nhân viên"
    )


class CheckAvailabilityTool(Tool[CheckAvailabilityArgs]):
    @property
    def name(self) -> str:
        return "check_availability"

    @property
    def description(self) -> str:
        return (
            "Kiểm tra các khung giờ trống trong ngày cho một hoặc nhiều dịch vụ (GET /availability). "
            "Trả về danh sách slot kèm nhân viên xếp hạng theo Fair Turn. PHẢI gọi tool này trước khi "
            "gọi create_appointment/reschedule_appointment để có startTime/staffId hợp lệ."
        )

    def get_args_schema(self) -> Type[CheckAvailabilityArgs]:
        return CheckAvailabilityArgs

    async def execute(self, context: ToolContext, args: CheckAvailabilityArgs) -> ToolResult:
        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            body = await client.get_availability(
                jwt=jwt,
                date=args.date,
                service_ids=args.service_ids,
                duration_min=args.duration_min,
                staff_id=args.staff_id,
            )
        except PosApiError as e:
            if e.status_code == 422:
                return ToolResult(
                    success=False, result_for_llm=f"Yêu cầu không hợp lệ: {e.body}", error=e.code or "invalid_request"
                )
            return ToolResult(
                success=False, result_for_llm=f"Lỗi kiểm tra lịch trống: {e.body}", error=e.code or "pos_api_error"
            )

        slots = body.get("slots", [])
        if not slots:
            return ToolResult(
                success=True,
                result_for_llm=(
                    f"Không còn slot trống nào ngày {args.date} cho dịch vụ đã chọn. "
                    "Hãy đề xuất ngày khác, hoặc mời khách vào Waitlist (join_waitlist)."
                ),
            )

        lines = []
        for slot in slots[:8]:
            ranked = slot.get("rankedStaff") or []
            top = ranked[0] if ranked else None
            tag = " (Fair Turn Next)" if top and top.get("statusBadge") == "FAIR_TURN_NEXT" else ""
            staff_desc = f"{top['name']}{tag}" if top else "?"
            staff_id = top["staffId"] if top else "?"
            lines.append(f"- {slot['startTime']}–{slot['endTime']}: {staff_desc} (staffId={staff_id})")

        return ToolResult(
            success=True,
            result_for_llm=f"Slot trống ngày {args.date}:\n" + "\n".join(lines),
            metadata={"slots": slots[:8]},
        )
