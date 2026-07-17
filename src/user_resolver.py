"""JwtForwardUserResolver — lấy Bearer JWT do POS frontend forward, KHÔNG tự verify chữ ký
(astry-pos-be tự verify RS256/JWKS trên từng call REST, xem shared/auth/jwt.strategy.ts).
Agent chỉ decode phần payload (unverified) để lấy salon_id/role/tên hiển thị cho system prompt
và audit; JWT gốc được giữ nguyên trong User.metadata["jwt"] để pos_client forward lại y hệt
khi gọi astry-pos-be — đây là điểm khác `HrmForwardUserResolver` của DB-Agent (nó tin header do
backend forward, không phải JWT thật)."""
from __future__ import annotations

import base64
import json
from typing import Any, Dict

from vanna.core.user import RequestContext, User, UserResolver


def _decode_jwt_payload_unverified(token: str) -> Dict[str, Any]:
    try:
        _, payload_b64, _ = token.split(".")
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


class MissingJwtError(PermissionError):
    pass


class JwtForwardUserResolver(UserResolver):
    async def resolve_user(self, request_context: RequestContext) -> User:
        auth_header = request_context.get_header("Authorization") or ""
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        if not token:
            raise MissingJwtError(
                "Thiếu Authorization: Bearer <JWT>. POS frontend phải forward nguyên JWT "
                "của Staff đang đăng nhập khi gọi vào agent."
            )

        claims = _decode_jwt_payload_unverified(token)
        realm_roles = (claims.get("realm_access") or {}).get("roles", [])

        return User(
            id=claims.get("sub", "unknown"),
            email=claims.get("email"),
            username=claims.get("preferred_username"),
            group_memberships=realm_roles or ["STAFF"],
            metadata={
                "jwt": token,
                "salon_id": claims.get("salon_id"),
                "roles": realm_roles,
            },
        )
