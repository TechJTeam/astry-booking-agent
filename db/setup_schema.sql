-- Tạo schema + role RIÊNG cho astry-booking-agent, trong CÙNG Postgres server/database với
-- astry-pos-be (vd Azure Postgres `astry_pos`) nhưng KHÔNG được cấp quyền vào schema `public`
-- mà Prisma của astry-pos-be quản lý. Mục đích: agent có thể lưu bền vững lịch sử hội thoại
-- (BR1-2 SRS) mà không có khả năng kỹ thuật đọc/ghi appointments/customers/staff.
--
-- Chạy 1 lần bởi người có quyền admin trên DB (vd role hiện tại `astry`):
--   psql "postgresql://astry:***@astry-dev-pg.postgres.database.azure.com:5432/astry_pos?sslmode=require" \
--        -f db/setup_schema.sql
--
-- Sau khi chạy xong, đổi CONVERSATION_STORE_URL trong .env thành connection string của role
-- booking_agent_svc (KHÔNG dùng lại DATABASE_URL của astry-pos-be):
--   CONVERSATION_STORE_URL=postgresql://booking_agent_svc:<password>@astry-dev-pg.postgres.database.azure.com:5432/astry_pos?options=-csearch_path%3Dbooking_agent&sslmode=require

\set booking_agent_password `echo "${BOOKING_AGENT_DB_PASSWORD:?Set BOOKING_AGENT_DB_PASSWORD env var before running this script}"`

CREATE SCHEMA IF NOT EXISTS booking_agent;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking_agent_svc') THEN
        CREATE ROLE booking_agent_svc WITH LOGIN PASSWORD :'booking_agent_password';
    ELSE
        ALTER ROLE booking_agent_svc WITH LOGIN PASSWORD :'booking_agent_password';
    END IF;
END
$$;

-- Chỉ cấp quyền trên schema booking_agent — role tự CREATE TABLE trong schema này lúc
-- PostgresConversationStore khởi động lần đầu (xem src/conversation_store.py), không cần
-- migration tool riêng.
GRANT USAGE, CREATE ON SCHEMA booking_agent TO booking_agent_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA booking_agent GRANT ALL ON TABLES TO booking_agent_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA booking_agent GRANT ALL ON SEQUENCES TO booking_agent_svc;

-- Chặn tường minh mọi quyền trên schema public (Prisma) — kể cả nếu default privilege của
-- PUBLIC pseudo-role trên Postgres < 15 có cấp sẵn USAGE/CREATE cho role mới.
REVOKE ALL ON SCHEMA public FROM booking_agent_svc;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM booking_agent_svc;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM booking_agent_svc;

-- Mọi connection dùng role này mặc định chỉ thấy schema booking_agent (+ pg_catalog) — bảng
-- không cần qualify tên schema trong code Python.
ALTER ROLE booking_agent_svc SET search_path = booking_agent;

COMMENT ON SCHEMA booking_agent IS 'Owned by astry-booking-agent — conversation history only. Không phải schema nghiệp vụ booking (đó vẫn là public, do astry-pos-be/Prisma quản lý).';
