"""build_agent(): đăng ký LLM Azure OpenAI, tool registry (chỉ REST-backed booking tools,
KHÔNG copy RunSqlTool từ DB-Agent — rủi ro bảo mật, không phù hợp domain chống double-booking),
JwtForwardUserResolver."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from vanna import Agent
from vanna.core.registry import ToolRegistry
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.integrations.azureopenai import AzureOpenAILlmService
from vanna.integrations.local.agent_memory import DemoAgentMemory

from src.conversation_store import PostgresConversationStore
from src.tools.appointment_tools import (
    CancelAppointmentTool,
    ConfirmOrDeclinePendingTool,
    CreateAppointmentTool,
    RescheduleAppointmentTool,
)
from src.tools.availability_tools import CheckAvailabilityTool
from src.tools.staff_service_tools import LookupServiceTool, LookupStaffTool
from src.tools.waitlist_tools import JoinWaitlistTool
from src.user_resolver import JwtForwardUserResolver

load_dotenv()

SYSTEM_PROMPT = """Bạn là trợ lý đặt lịch hẹn (AI Booking Agent) cho nhân viên salon (Manager/Lễ tân)
đang thao tác trên hệ thống POS. Nhiệm vụ: dịch yêu cầu đặt lịch bằng ngôn ngữ tự nhiên của Staff
thành các lời gọi tool tương ứng.

Quy tắc bắt buộc:
- Luôn xác định đủ 3 thông tin trước khi kiểm tra lịch trống: Dịch vụ (dùng lookup_service để lấy
  serviceId), Ngày, Khung giờ mong muốn. Nếu thiếu, hỏi lại Staff — KHÔNG tự đoán hoặc kết thúc hội
  thoại (BR1-4, BR2-2).
- KHÔNG đề xuất ngày trong quá khứ (BR2-4).
- Luôn gọi check_availability trước khi gọi create_appointment/reschedule_appointment để có
  startTime/staffId hợp lệ.
- create_appointment/reschedule_appointment/cancel_appointment/join_waitlist đều theo cơ chế xác
  nhận 2 bước: gọi tool lần đầu (confirm=false) để xem preview, đọc lại cho Staff nghe, chỉ gọi lại
  với confirm=true khi Staff xác nhận đồng ý bằng lời.
- Nếu check_availability không còn slot phù hợp, hoặc tạo/đổi lịch bị lỗi conflict (409), đề xuất
  tối đa 3 slot/nhân viên thay thế (UC-AIBK-04); nếu vẫn không có, chủ động mời Staff thêm khách vào
  Waitlist bằng join_waitlist thay vì kết thúc hội thoại không có kết quả.
- Không tự bịa serviceId/staffId — luôn resolve qua lookup_service/lookup_staff hoặc lấy từ kết quả
  check_availability trước đó.
- Tránh lặp lại số điện thoại khách hàng không cần thiết trong câu trả lời.
"""


def build_agent() -> Agent:
    llm = AzureOpenAILlmService(
        model=os.environ["AZURE_GPT_5_2_DEPLOYMENT"],
        api_key=os.environ["AZURE_API_KEY"],
        azure_endpoint=os.environ["AZURE_ENDPOINT"],
        api_version=os.environ.get("AZURE_API_VERSION", "2024-10-21"),
    )

    tools = ToolRegistry()
    for tool in (
        LookupServiceTool(),
        LookupStaffTool(),
        CheckAvailabilityTool(),
        CreateAppointmentTool(),
        RescheduleAppointmentTool(),
        CancelAppointmentTool(),
        ConfirmOrDeclinePendingTool(),
        JoinWaitlistTool(),
    ):
        tools.register_local_tool(tool, access_groups=tool.access_groups)

    agent_memory = DemoAgentMemory(max_items=1000)

    # conversation_store: nếu CONVERSATION_STORE_URL được set (Postgres, schema riêng của agent
    # — xem db/setup_schema.sql), lưu bền vững để đáp ứng BR1-2 SRS (Audit & Training AI). Nếu
    # không, rơi về MemoryConversationStore mặc định của vanna (in-RAM, mất khi restart) — chấp
    # nhận được cho dev local, KHÔNG dùng cho production.
    conversation_store = (
        PostgresConversationStore(dsn=os.environ["CONVERSATION_STORE_URL"])
        if os.getenv("CONVERSATION_STORE_URL")
        else None
    )

    return Agent(
        llm_service=llm,
        tool_registry=tools,
        user_resolver=JwtForwardUserResolver(),
        agent_memory=agent_memory,
        conversation_store=conversation_store,
        system_prompt_builder=DefaultSystemPromptBuilder(base_prompt=SYSTEM_PROMPT),
    )
