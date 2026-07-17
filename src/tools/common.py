"""Helper dùng chung cho các write tool — pattern confirm-gate 2 lượt gọi, copy từ
DB-Agent/src/write_tools/common.py. generate_id() ở đây chỉ dùng cho Idempotency-Key khi
caller không truyền sẵn — KHÔNG dùng để tự sinh primary key ghi DB (agent không ghi DB)."""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from vanna.core.tool import ToolResult


def generate_id() -> str:
    return uuid.uuid4().hex


def pending_confirmation_result(action_description: str, args_summary: Dict[str, Any]) -> ToolResult:
    """Bước 1 của thao tác ghi dữ liệu: KHÔNG gọi API ghi, chỉ trả lại bản xem trước và
    hướng dẫn LLM hỏi lại Staff trước khi thực thi thật (confirm=true ở lượt gọi tiếp theo)."""
    lines = "\n".join(f"- {k}: {v}" for k, v in args_summary.items() if v is not None)
    text = (
        f"Cần xác nhận trước khi thực hiện: **{action_description}**\n\n"
        f"Chi tiết:\n{lines}\n\n"
        "Hãy hỏi lại Staff có chắc chắn muốn thực hiện không. Nếu Staff xác nhận đồng ý "
        "(vd 'có', 'đồng ý', 'xác nhận'), hãy gọi lại ĐÚNG tool này với confirm=true và giữ "
        "nguyên toàn bộ tham số khác như cũ. Nếu Staff từ chối, không gọi lại tool nữa."
    )
    return ToolResult(
        success=True,
        result_for_llm=text,
        metadata={"pending_confirmation": True, **args_summary},
    )
