import pytest
import respx
from httpx import Response

from src.tools.appointment_tools import CreateAppointmentTool, ServiceLineArgs
from src.tools.appointment_tools import CreateAppointmentArgs


@pytest.mark.asyncio
async def test_create_appointment_without_confirm_does_not_call_api(tool_context):
    tool = CreateAppointmentTool()
    args = CreateAppointmentArgs(
        booking_date="2026-07-20",
        start_time="09:00",
        services=[ServiceLineArgs(service_id="svc-1", staff_id="staff-1")],
        confirm=False,
    )

    with respx.mock:
        # không mock route nào -> nếu tool lỡ gọi HTTP thật, respx sẽ raise AssertionError
        result = await tool.execute(tool_context, args)

    assert result.success is True
    assert result.metadata["pending_confirmation"] is True
    assert "xác nhận" in result.result_for_llm.lower()


@pytest.mark.asyncio
@respx.mock
async def test_create_appointment_with_confirm_calls_api(tool_context):
    respx.post("http://pos-be.test/api/v1/appointments").mock(
        return_value=Response(
            201, json={"id": "appt-1", "status": "CONFIRMED", "startTime": "2026-07-20T09:00:00+07:00"}
        )
    )
    tool = CreateAppointmentTool()
    args = CreateAppointmentArgs(
        booking_date="2026-07-20",
        start_time="09:00",
        services=[ServiceLineArgs(service_id="svc-1", staff_id="staff-1")],
        confirm=True,
    )

    result = await tool.execute(tool_context, args)

    assert result.success is True
    assert result.metadata["appointment"]["id"] == "appt-1"


@pytest.mark.asyncio
@respx.mock
async def test_create_appointment_conflict_surfaces_alternatives(tool_context):
    respx.post("http://pos-be.test/api/v1/appointments").mock(
        return_value=Response(
            409,
            json={
                "code": "BOOKING_CONFLICT",
                "details": {
                    "conflicts": [{"code": "STAFF_CONFLICT"}],
                    "suggestions": [
                        {"type": "ALTERNATIVE_SLOT", "staffId": "staff-1", "startTime": "10:00", "endTime": "10:30"}
                    ],
                },
            },
        )
    )
    tool = CreateAppointmentTool()
    args = CreateAppointmentArgs(
        booking_date="2026-07-20",
        start_time="09:00",
        services=[ServiceLineArgs(service_id="svc-1", staff_id="staff-1")],
        confirm=True,
    )

    result = await tool.execute(tool_context, args)

    assert result.success is False
    assert result.error == "booking_conflict"
    assert "10:00" in result.result_for_llm
