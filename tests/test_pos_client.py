import pytest
import respx
from httpx import Response

from src.pos_client import PosApiError, PosClient


@pytest.mark.asyncio
@respx.mock
async def test_create_appointment_success():
    client = PosClient(base_url="http://pos-be.test", internal_api_key="ik")
    route = respx.post("http://pos-be.test/api/v1/appointments").mock(
        return_value=Response(201, json={"id": "appt-1", "status": "CONFIRMED", "startTime": "2026-07-20T09:00:00+07:00"})
    )

    appt = await client.create_appointment(jwt="jwt-token", payload={"bookingDate": "2026-07-20"})

    assert appt["id"] == "appt-1"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer jwt-token"
    assert "Idempotency-Key" in sent.headers


@pytest.mark.asyncio
@respx.mock
async def test_create_appointment_conflict_raises_with_suggestions():
    client = PosClient(base_url="http://pos-be.test", internal_api_key="ik")
    conflict_body = {
        "code": "BOOKING_CONFLICT",
        "message": "Booking conflict",
        "details": {
            "conflicts": [{"code": "STAFF_CONFLICT", "staffId": "s1", "conflictingAppointmentId": "a2"}],
            "suggestions": [{"type": "ALTERNATIVE_SLOT", "staffId": "s1", "startTime": "10:00", "endTime": "10:30"}],
        },
    }
    respx.post("http://pos-be.test/api/v1/appointments").mock(return_value=Response(409, json=conflict_body))

    with pytest.raises(PosApiError) as exc_info:
        await client.create_appointment(jwt="jwt-token", payload={})

    err = exc_info.value
    assert err.status_code == 409
    assert err.code == "BOOKING_CONFLICT"
    assert err.body["details"]["suggestions"][0]["type"] == "ALTERNATIVE_SLOT"


@pytest.mark.asyncio
@respx.mock
async def test_list_public_staff_uses_internal_api_key_not_jwt():
    client = PosClient(base_url="http://pos-be.test", internal_api_key="ik-secret")
    route = respx.get("http://pos-be.test/api/v1/public/staff").mock(
        return_value=Response(200, json=[{"id": "st1", "fullName": "An", "workingStatus": "AVAILABLE"}])
    )

    staff = await client.list_public_staff(salon_id="salon-123")

    assert staff[0]["id"] == "st1"
    sent = route.calls.last.request
    assert sent.headers["X-Internal-Api-Key"] == "ik-secret"
    assert "Authorization" not in sent.headers
    assert sent.url.params["salonId"] == "salon-123"


@pytest.mark.asyncio
async def test_list_public_staff_without_internal_key_raises(monkeypatch):
    monkeypatch.delenv("POS_INTERNAL_API_KEY", raising=False)
    client = PosClient(base_url="http://pos-be.test", internal_api_key=None)
    with pytest.raises(RuntimeError):
        await client.list_public_staff(salon_id="salon-123")


@pytest.mark.asyncio
@respx.mock
async def test_patch_appointment_sends_if_match():
    client = PosClient(base_url="http://pos-be.test", internal_api_key="ik")
    route = respx.patch("http://pos-be.test/api/v1/appointments/appt-1").mock(
        return_value=Response(200, json={"id": "appt-1", "status": "CONFIRMED", "startTime": "2026-07-20T10:00:00+07:00"})
    )

    await client.patch_appointment(
        jwt="jwt-token", appointment_id="appt-1", payload={"modifyReason": "CUSTOMER_REQUEST"}, if_match="2026-07-01T00:00:00.000Z"
    )

    sent = route.calls.last.request
    assert sent.headers["If-Match"] == "2026-07-01T00:00:00.000Z"


@pytest.mark.asyncio
@respx.mock
async def test_patch_appointment_stale_resource_raises_412():
    client = PosClient(base_url="http://pos-be.test", internal_api_key="ik")
    respx.patch("http://pos-be.test/api/v1/appointments/appt-1").mock(
        return_value=Response(412, json={"code": "STALE_RESOURCE", "message": "stale"})
    )

    with pytest.raises(PosApiError) as exc_info:
        await client.patch_appointment(
            jwt="jwt-token", appointment_id="appt-1", payload={"modifyReason": "OTHER"}, if_match="old-etag"
        )

    assert exc_info.value.status_code == 412
    assert exc_info.value.code == "STALE_RESOURCE"
