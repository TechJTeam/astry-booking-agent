import pytest
import respx
from httpx import Response

from src.tools.availability_tools import CheckAvailabilityArgs, CheckAvailabilityTool


@pytest.mark.asyncio
@respx.mock
async def test_check_availability_returns_slots_with_fair_turn(tool_context):
    route = respx.get("http://pos-be.test/api/v1/availability").mock(
        return_value=Response(
            200,
            json={
                "date": "2026-07-20",
                "timezone": "Asia/Ho_Chi_Minh",
                "slots": [
                    {
                        "startTime": "2026-07-20T09:00:00+07:00",
                        "endTime": "2026-07-20T09:30:00+07:00",
                        "rankedStaff": [
                            {
                                "staffId": "staff-1",
                                "statusBadge": "FAIR_TURN_NEXT",
                                "dailyTurnPoints": 1,
                                "name": "An",
                                "avatar": None,
                            }
                        ],
                    }
                ],
            },
        )
    )
    tool = CheckAvailabilityTool()
    args = CheckAvailabilityArgs(date="2026-07-20", service_ids=["svc-1"], duration_min=30)

    result = await tool.execute(tool_context, args)

    assert result.success is True
    assert "Fair Turn Next" in result.result_for_llm
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer " + tool_context.user.metadata["jwt"]
    assert sent.url.params["serviceIds"] == "svc-1"
    assert sent.url.params["durationMin"] == "30"


@pytest.mark.asyncio
@respx.mock
async def test_check_availability_no_slots_suggests_waitlist(tool_context):
    respx.get("http://pos-be.test/api/v1/availability").mock(
        return_value=Response(200, json={"date": "2026-07-20", "timezone": "Asia/Ho_Chi_Minh", "slots": []})
    )
    tool = CheckAvailabilityTool()
    args = CheckAvailabilityArgs(date="2026-07-20", service_ids=["svc-1"], duration_min=30)

    result = await tool.execute(tool_context, args)

    assert result.success is True
    assert "Waitlist" in result.result_for_llm
