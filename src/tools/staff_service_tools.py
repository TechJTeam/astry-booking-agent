"""READ tools (UC-AIBK-02 entity resolution): dịch tên dịch vụ/nhân viên Staff gõ tự do
thành serviceId/staffId thật trước khi gọi check_availability / create_appointment."""
from __future__ import annotations

from typing import Optional, Type

from pydantic import BaseModel, Field
from vanna.core.tool import Tool, ToolContext, ToolResult

from src.pos_client import PosApiError, get_pos_client


class LookupServiceArgs(BaseModel):
    query: str = Field(description="Tên dịch vụ Staff gõ tự do, ví dụ 'cắt tóc', 'gội đầu'")


class LookupServiceTool(Tool[LookupServiceArgs]):
    @property
    def name(self) -> str:
        return "lookup_service"

    @property
    def description(self) -> str:
        return (
            "Tìm dịch vụ theo tên gần đúng trong catalog của salon (GET /services?q=...). "
            "Dùng để chuyển tên dịch vụ Staff gõ tự do (vd 'cắt tóc') thành serviceId thật "
            "trước khi gọi check_availability hoặc create_appointment."
        )

    def get_args_schema(self) -> Type[LookupServiceArgs]:
        return LookupServiceArgs

    async def execute(self, context: ToolContext, args: LookupServiceArgs) -> ToolResult:
        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            body = await client.list_services(jwt=jwt, q=args.query, status="ACTIVE")
        except PosApiError as e:
            return ToolResult(
                success=False, result_for_llm=f"Lỗi tra cứu dịch vụ: {e.body}", error=e.code or "pos_api_error"
            )

        items = body.get("items", [])
        if not items:
            return ToolResult(
                success=True,
                result_for_llm=(
                    f"Không tìm thấy dịch vụ nào khớp với '{args.query}'. "
                    "Hãy hỏi lại Staff tên dịch vụ chính xác hơn."
                ),
            )

        lines = [
            f"- {s['name']} (id={s['id']}, {s['baseDurationMinutes']} phút, "
            f"{s['basePriceCents'] / 100:,.0f}đ)"
            for s in items[:5]
        ]
        return ToolResult(
            success=True,
            result_for_llm=f"Tìm thấy {len(items)} dịch vụ khớp '{args.query}':\n" + "\n".join(lines),
            metadata={"services": items[:5]},
        )


class LookupStaffArgs(BaseModel):
    skill_tag_id: Optional[str] = Field(
        default=None,
        description="Lọc theo skillTagId (uuid) nếu đã biết, để chỉ lấy staff đủ kỹ năng cho dịch vụ",
    )


class LookupStaffTool(Tool[LookupStaffArgs]):
    @property
    def name(self) -> str:
        return "lookup_staff"

    @property
    def description(self) -> str:
        return (
            "Liệt kê nhân viên Active, đủ điều kiện nhận lịch (GET /public/staff). "
            "Dùng khi Staff yêu cầu chỉ định nhân viên theo tên hoặc theo kỹ năng."
        )

    def get_args_schema(self) -> Type[LookupStaffArgs]:
        return LookupStaffArgs

    async def execute(self, context: ToolContext, args: LookupStaffArgs) -> ToolResult:
        salon_id = context.user.metadata.get("salon_id")
        if not salon_id:
            return ToolResult(
                success=False,
                result_for_llm="Không xác định được salon_id từ JWT của Staff.",
                error="missing_salon_id",
            )

        client = get_pos_client()
        try:
            staff = await client.list_public_staff(salon_id=salon_id, skill_tag_id=args.skill_tag_id)
        except PosApiError as e:
            return ToolResult(
                success=False, result_for_llm=f"Lỗi tra cứu nhân viên: {e.body}", error=e.code or "pos_api_error"
            )
        except RuntimeError as e:
            return ToolResult(success=False, result_for_llm=str(e), error="missing_internal_api_key")

        if not staff:
            return ToolResult(success=True, result_for_llm="Không có nhân viên nào đủ điều kiện.")

        lines = [f"- {s['fullName']} (id={s['id']}, {s['workingStatus']})" for s in staff[:10]]
        return ToolResult(
            success=True,
            result_for_llm="Nhân viên khả dụng:\n" + "\n".join(lines),
            metadata={"staff": staff[:10]},
        )
