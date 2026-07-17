"""Chạy 1 lần để tạo schema + role Postgres RIÊNG cho astry-booking-agent, dùng chung server
với astry-pos-be nhưng tách biệt hoàn toàn với schema `public` (do Prisma quản lý). Thay thế
db/setup_schema.sql cho máy không có sẵn client `psql`.

Cách chạy (PowerShell, từ thư mục gốc repo):
    $env:ADMIN_DATABASE_URL = "postgresql://astry:***@astry-dev-pg.postgres.database.azure.com:5432/astry_pos?sslmode=require"
    .\.venv\Scripts\python.exe db\setup_schema.py

Script dùng chính role admin hiện tại (vd `astry`) để CREATE ROLE/SCHEMA + GRANT/REVOKE, rồi in
ra sẵn dòng CONVERSATION_STORE_URL của role mới (`booking_agent_svc`) để paste vào .env. Idempotent
— chạy lại nhiều lần không lỗi (role/schema đã tồn tại thì chỉ đổi mật khẩu + re-apply grants).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from urllib.parse import urlparse, urlunparse

import asyncpg

SCHEMA_NAME = "booking_agent"
ROLE_NAME = "booking_agent_svc"


async def main() -> None:
    # Console Windows mặc định dùng cp1252, không encode được tiếng Việt có dấu -> ép utf-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    admin_dsn = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not admin_dsn:
        print(
            "Thiếu ADMIN_DATABASE_URL (hoặc DATABASE_URL) — set biến này trỏ tới connection "
            "string admin (vd role `astry`) trước khi chạy script.",
            file=sys.stderr,
        )
        sys.exit(1)

    # token_urlsafe() chỉ sinh ký tự [A-Za-z0-9_-] — không có dấu nháy đơn nên an toàn để nối
    # thẳng vào SQL literal bên dưới (CREATE/ALTER ROLE PASSWORD không nhận $n qua extended
    # protocol một cách nhất quán giữa các version Postgres, nên tránh dùng tham số hoá ở đây).
    password = secrets.token_urlsafe(24)
    assert password.isalnum() or all(c.isalnum() or c in "-_" for c in password)

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')

        role_exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE_NAME)
        if role_exists:
            await conn.execute(f"ALTER ROLE \"{ROLE_NAME}\" WITH LOGIN PASSWORD '{password}'")
        else:
            await conn.execute(f"CREATE ROLE \"{ROLE_NAME}\" WITH LOGIN PASSWORD '{password}'")

        await conn.execute(f'GRANT USAGE, CREATE ON SCHEMA "{SCHEMA_NAME}" TO "{ROLE_NAME}"')
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{SCHEMA_NAME}" GRANT ALL ON TABLES TO "{ROLE_NAME}"'
        )
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{SCHEMA_NAME}" GRANT ALL ON SEQUENCES TO "{ROLE_NAME}"'
        )

        await conn.execute(f'REVOKE ALL ON SCHEMA public FROM "{ROLE_NAME}"')
        await conn.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{ROLE_NAME}"')
        await conn.execute(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM "{ROLE_NAME}"')

        await conn.execute(f'ALTER ROLE "{ROLE_NAME}" SET search_path = "{SCHEMA_NAME}"')
    finally:
        await conn.close()

    parsed = urlparse(admin_dsn)
    new_netloc = f"{ROLE_NAME}:{password}@{parsed.hostname}"
    if parsed.port:
        new_netloc += f":{parsed.port}"
    new_dsn = urlunparse(parsed._replace(netloc=new_netloc))

    print(f'Đã tạo/cập nhật schema "{SCHEMA_NAME}" và role "{ROLE_NAME}".\n')
    print("Paste dòng sau vào .env (thay cho CONVERSATION_STORE_URL= đang trống):")
    print(f"CONVERSATION_STORE_URL={new_dsn}")


if __name__ == "__main__":
    asyncio.run(main())
