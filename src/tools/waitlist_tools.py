"""WRITE tool (nhánh Waitlist của UC-AIBK-04): khi không còn slot phù hợp, mời khách vào
danh sách chờ thay vì kết thúc hội thoại không có kết quả (BR2-2 no dead-end)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field
from vanna.core.tool import Tool, ToolContext, ToolResult

from src.pos_client import PosApiError, get_pos_client
from src.tools.common import pending_confirmation_result


class JoinWaitlistArgs(BaseModel):
    requested_service_ids: List[str] = Field(description="serviceId(s) khách muốn, tối thiểu 1")
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    preferred_staff_id: Optional[str] = None
    preferred_date_from: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    preferred_date_to: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    preferred_time_from: Optional[str] = Field(default=None, description="HH:mm")
    preferred_time_to: Optional[str] = Field(default=None, description="HH:mm")
    priority: str = Field(default="NORMAL", description="HIGH|NORMAL|LOW")
    confirm: bool = Field(default=False)


_FIELD_TO_PAYLOAD_KEY = {
    "customer_id": "customerId",
    "customer_name": "customerName",
    "customer_phone": "customerPhone",
    "preferred_staff_id": "preferredStaffId",
    "preferred_date_from": "preferredDateFrom",
    "preferred_date_to": "preferredDateTo",
    "preferred_time_from": "preferredTimeFrom",
    "preferred_time_to": "preferredTimeTo",
}


class JoinWaitlistTool(Tool[JoinWaitlistArgs]):
    @property
    def name(self) -> str:
        return "join_waitlist"

    @property
    def description(self) -> str:
        return (
            "Thêm khách vào danh sách chờ (POST /waitlist) khi không còn slot phù hợp trong ngày "
            "khách muốn. Dùng thay cho việc kết thúc hội thoại không có kết quả."
        )

    def get_args_schema(self) -> Type[JoinWaitlistArgs]:
        return JoinWaitlistArgs

    async def execute(self, context: ToolContext, args: JoinWaitlistArgs) -> ToolResult:
        if not args.confirm:
            return pending_confirmation_result(
                "Thêm khách vào Waitlist",
                {
                    "Dịch vụ": args.requested_service_ids,
                    "Khách": args.customer_name or args.customer_id,
                    "Khoảng ngày mong muốn": f"{args.preferred_date_from} - {args.preferred_date_to}",
                },
            )

        payload: Dict[str, Any] = {
            "requestedServiceIds": args.requested_service_ids,
            "priority": args.priority,
        }
        for field, key in _FIELD_TO_PAYLOAD_KEY.items():
            value = getattr(args, field)
            if value is not None:
                payload[key] = value

        jwt = context.user.metadata.get("jwt")
        client = get_pos_client()
        try:
            entry = await client.create_waitlist_entry(jwt=jwt, payload=payload)
        except PosApiError as e:
            return ToolResult(
                success=False, result_for_llm=f"Lỗi thêm vào waitlist: {e.body}", error=e.code or "pos_api_error"
            )

        return ToolResult(
            success=True,
            result_for_llm=f"Đã thêm vào waitlist, id={entry['id']}, priority={entry['priority']}.",
            metadata={"waitlist_entry": entry},
        )
