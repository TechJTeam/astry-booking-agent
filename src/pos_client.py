"""HTTP client bọc REST API của astry-pos-be. Agent KHÔNG bao giờ ghi thẳng Postgres của
astry-pos-be — mọi write đều đi qua các endpoint đã có sẵn để không né qua state machine,
BookingConflictEngine (Postgres EXCLUDE constraint), FairTurnEngine, ETag optimistic
concurrency (If-Match) và Idempotency-Key đã nằm trong service layer NestJS.

Route/DTO ở đây được chốt từ việc đọc trực tiếp source astry-pos-be (không đoán):
appointment.controller.ts, calendar.controller.ts, waitlist.controller.ts,
services.controller.ts, staff-public.controller.ts, idempotency-key.interceptor.ts,
if-match.interceptor.ts.

Lưu ý quan trọng (deviation so với giả định ban đầu trong PLAN.md mục 0):
GET /public/staff KHÔNG chấp nhận JWT forward — route này dùng @Public() + InternalApiKeyGuard
(X-Internal-Api-Key), không phải JwtAuthGuard. Vì vậy list_public_staff() dùng
POS_INTERNAL_API_KEY riêng, không forward JWT của Staff.
"""
from __future__ import annotations

import os
import uuid
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import httpx

API_PREFIX = "/api/v1"


class PosApiError(Exception):
    """Bọc mọi lỗi HTTP >=400 từ astry-pos-be, giữ nguyên status_code + body gốc
    (bao gồm details.conflicts/details.suggestions của lỗi 409 BOOKING_CONFLICT) để tool
    phía trên tự quyết định cách trả lời Staff."""

    def __init__(self, status_code: int, body: Dict[str, Any]):
        self.status_code = status_code
        self.body = body if isinstance(body, dict) else {"message": str(body)}
        detail = self.body.get("detail")
        if isinstance(detail, dict) and "code" in detail:
            # một số lỗi NestJS mặc định wrap thêm 1 lớp {"detail": {...}}
            self.code = detail.get("code")
            self.body = detail
        else:
            self.code = self.body.get("code")
        super().__init__(f"PosApiError {status_code} {self.code}: {self.body}")


class PosClient:
    def __init__(self, base_url: Optional[str] = None, internal_api_key: Optional[str] = None):
        base = (base_url or os.environ["POS_API_BASE_URL"]).rstrip("/")
        self._base_url = base + API_PREFIX
        self._internal_api_key = internal_api_key if internal_api_key is not None else os.getenv(
            "POS_INTERNAL_API_KEY"
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        jwt: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Tuple[Optional[Dict[str, Any]], httpx.Headers]:
        req_headers = dict(headers or {})
        if jwt:
            req_headers["Authorization"] = f"Bearer {jwt}"

        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            resp = await client.request(method, path, headers=req_headers, **kwargs)

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {"code": "UNKNOWN_ERROR", "message": resp.text}
            raise PosApiError(resp.status_code, body)

        if resp.status_code == 204 or not resp.content:
            return None, resp.headers
        return resp.json(), resp.headers

    @staticmethod
    def _idempotency_headers(idempotency_key: Optional[str]) -> Dict[str, str]:
        # Idempotency-Key có Swagger doc là optional nhưng interceptor thực tế bắt buộc
        # (400 IDEMPOTENCY_KEY_REQUIRED nếu thiếu) — luôn tự sinh nếu caller không truyền.
        return {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}

    # ---------------------------------------------------------------- reads

    async def list_services(
        self,
        jwt: str,
        q: Optional[str] = None,
        status: str = "ACTIVE",
        service_type: str = "MAIN",
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"status": status, "serviceType": service_type, "pageSize": page_size}
        if q:
            params["q"] = q
        body, _ = await self._request("GET", "/services", jwt=jwt, params=params)
        return body or {}

    async def get_availability(
        self,
        jwt: str,
        date: str,
        service_ids: List[str],
        duration_min: int,
        staff_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "date": date,
            "serviceIds": ",".join(service_ids),
            "durationMin": duration_min,
        }
        if staff_id:
            params["staffId"] = staff_id
        body, _ = await self._request("GET", "/availability", jwt=jwt, params=params)
        return body or {}

    async def list_public_staff(
        self, salon_id: str, skill_tag_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if not self._internal_api_key:
            raise RuntimeError(
                "POS_INTERNAL_API_KEY chưa cấu hình. GET /public/staff dùng "
                "X-Internal-Api-Key (InternalApiKeyGuard), KHÔNG chấp nhận JWT forward."
            )
        path = f"/public/staff/by-skill/{skill_tag_id}" if skill_tag_id else "/public/staff"
        headers = {"X-Internal-Api-Key": self._internal_api_key}
        body, _ = await self._request("GET", path, headers=headers, params={"salonId": salon_id})
        return body or []

    # ------------------------------------------------------------ appointment

    async def get_appointment(self, jwt: str, appointment_id: str) -> Tuple[Dict[str, Any], str]:
        body, headers = await self._request("GET", f"/appointments/{appointment_id}", jwt=jwt)
        etag = headers.get("etag") or headers.get("ETag") or (body or {}).get("updatedAt")
        return body or {}, etag

    async def create_appointment(
        self, jwt: str, payload: Dict[str, Any], idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        headers = self._idempotency_headers(idempotency_key)
        body, _ = await self._request("POST", "/appointments", jwt=jwt, json=payload, headers=headers)
        return body or {}

    async def patch_appointment(
        self, jwt: str, appointment_id: str, payload: Dict[str, Any], if_match: str
    ) -> Dict[str, Any]:
        headers = {"If-Match": if_match}
        body, _ = await self._request(
            "PATCH", f"/appointments/{appointment_id}", jwt=jwt, json=payload, headers=headers
        )
        return body or {}

    async def confirm_appointment(
        self, jwt: str, appointment_id: str, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        headers = self._idempotency_headers(idempotency_key)
        body, _ = await self._request(
            "POST", f"/appointments/{appointment_id}/confirm", jwt=jwt, headers=headers
        )
        return body or {}

    async def decline_appointment(
        self,
        jwt: str,
        appointment_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._idempotency_headers(idempotency_key)
        payload = {"reason": reason} if reason else {}
        body, _ = await self._request(
            "POST", f"/appointments/{appointment_id}/decline", jwt=jwt, json=payload, headers=headers
        )
        return body or {}

    async def cancel_appointment(
        self,
        jwt: str,
        appointment_id: str,
        reason_code: Optional[str] = None,
        reason_note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._idempotency_headers(idempotency_key)
        payload: Dict[str, Any] = {}
        if reason_code:
            payload["reasonCode"] = reason_code
        if reason_note:
            payload["reasonNote"] = reason_note
        body, _ = await self._request(
            "POST", f"/appointments/{appointment_id}/cancel", jwt=jwt, json=payload, headers=headers
        )
        return body or {}

    # ---------------------------------------------------------------- waitlist

    async def create_waitlist_entry(
        self, jwt: str, payload: Dict[str, Any], idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        headers = self._idempotency_headers(idempotency_key)
        body, _ = await self._request("POST", "/waitlist", jwt=jwt, json=payload, headers=headers)
        return body or {}


@lru_cache(maxsize=1)
def get_pos_client() -> PosClient:
    return PosClient()
