"""Service-to-service auth: chỉ POS frontend/BFF (client có AGENT_API_KEY) được gọi vào agent.
Copy nguyên pattern ApiKeyMiddleware từ DB-Agent/src/auth.py, đổi tên env var."""
from __future__ import annotations

import os
import secrets
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

PUBLIC_PATH_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/health")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path or "/"
        if any(path == p or path.startswith(p + "/") for p in PUBLIC_PATH_PREFIXES):
            return await call_next(request)
        if path in ("/", "/favicon.ico"):
            return await call_next(request)

        expected = (os.getenv("AGENT_API_KEY") or "").strip()
        if not expected:
            # Dev: chưa cấu hình key -> cho qua. Prod BẮT BUỘC phải set AGENT_API_KEY.
            return await call_next(request)

        provided = (
            request.headers.get("x-api-key") or request.headers.get("X-API-Key") or ""
        ).strip()
        if not provided or not secrets.compare_digest(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: thiếu hoặc sai X-API-Key."},
            )
        return await call_next(request)
