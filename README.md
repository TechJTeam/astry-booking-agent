# astry-booking-agent

AI Booking Agent cho Astry POS — FastAPI + `vanna` (LLM = Azure OpenAI), đứng cạnh
`astry-pos-be` (NestJS/Prisma). Xem `PLAN.md` để biết đầy đủ bối cảnh/kiến trúc.

## Nguyên tắc cốt lõi

- **Không ghi thẳng Postgres của astry-pos-be.** Mọi thao tác tạo/sửa/huỷ lịch hẹn đều đi
  qua REST API sẵn có (`POST /appointments`, `PATCH /appointments/:id`, ...) để không né qua
  state machine, `BookingConflictEngine` (Postgres `EXCLUDE` constraint), `FairTurnEngine`,
  ETag optimistic concurrency (`If-Match`), Idempotency-Key.
- **Forward nguyên JWT của Staff.** POS frontend forward `Authorization: Bearer <JWT>` khi gọi
  vào agent; agent replay chính JWT đó khi gọi astry-pos-be — không tự cấp quyền, không tự
  verify chữ ký (astry-pos-be tự verify RS256/JWKS trên từng call).
- **Không có `RunSqlTool`.** Không copy tool chạy SQL tự do từ DB-Agent — rủi ro bảo mật,
  không phù hợp domain phải đảm bảo tuyệt đối không double-booking.

## Cấu trúc

```
db/
└── setup_schema.sql     # tạo schema + role Postgres riêng cho conversation store (chạy 1 lần)
src/
├── agent_setup.py       # build_agent(): LLM Azure, ToolRegistry, JwtForwardUserResolver
├── auth.py               # ApiKeyMiddleware (AGENT_API_KEY)
├── user_resolver.py      # JwtForwardUserResolver — decode JWT (unverified) lấy salon_id/role
├── pos_client.py          # httpx client bọc REST API astry-pos-be
├── conversation_store.py  # PostgresConversationStore — lưu bền vững lịch sử hội thoại (BR1-2)
├── server.py              # FastAPI app + middleware + /health
└── tools/
    ├── common.py                # pending_confirmation_result(), generate_id()
    ├── staff_service_tools.py   # READ: lookup_service, lookup_staff
    ├── availability_tools.py    # READ: check_availability
    ├── appointment_tools.py     # WRITE: create/reschedule/cancel/confirm/decline (confirm-gate)
    └── waitlist_tools.py        # WRITE: join_waitlist
tests/                     # pytest + respx (mock pos_client, không cần astry-pos-be thật)
deploy/
├── dev.Dockerfile         # build image (docker build -f deploy/dev.Dockerfile .)
├── docker-compose.yml     # compose target đặt trên Dev VM (deploy/dev.azure-pipelines.yml)
└── dev.azure-pipelines.yml # build+push ACR -> deploy Dev VM, xem deploy/dev.azure-pipelines.yml
docs/
└── API.md                 # cách gọi API (auth, chat_sse, pattern confirm-gate)
```

Xem [docs/API.md](docs/API.md) để biết cách gọi API (header bắt buộc, format SSE, pattern
confirm-gate 2 lượt cho các thao tác ghi).

## Conversation store (Postgres, schema riêng)

Mặc định (không set `CONVERSATION_STORE_URL`) agent lưu hội thoại **in-RAM**, mất khi restart —
chỉ chấp nhận được cho dev local. Để lưu bền vững (đáp ứng BR1-2 SRS):

1. Dùng **cùng server Postgres** với `astry-pos-be` (không cần dựng infra mới) nhưng **KHÔNG**
   dùng lại `DATABASE_URL` của nó — tạo 1 schema + role riêng, không có quyền gì trên schema
   `public` mà Prisma quản lý. Có 2 cách chạy tương đương, chọn 1:

   **Cách A — không cần cài `psql`** (dùng `asyncpg` đã có sẵn trong `.venv`), chạy từ PowerShell
   ở thư mục gốc repo:
   ```powershell
   $env:ADMIN_DATABASE_URL = "postgresql://astry:***@astry-dev-pg.postgres.database.azure.com:5432/astry_pos?sslmode=require"
   .\.venv\Scripts\python.exe db\setup_schema.py
   ```
   Script tự sinh mật khẩu random, tạo/cập nhật schema `booking_agent` + role `booking_agent_svc`,
   rồi in ra sẵn dòng `CONVERSATION_STORE_URL=...` — copy dòng đó paste thẳng vào `.env`. Đã verify
   end-to-end (kể cả việc role mới bị chặn ghi vào schema `public`) bằng 1 Postgres tạm chạy local.
   Chạy lại nhiều lần vẫn an toàn (idempotent) nhưng mỗi lần chạy sẽ **rotate mật khẩu** — nhớ update
   `.env` lại sau mỗi lần chạy.

   **Cách B — nếu có sẵn `psql`**:
   ```bash
   BOOKING_AGENT_DB_PASSWORD='<mật khẩu mới, tự sinh>' \
     psql "postgresql://astry:***@astry-dev-pg.postgres.database.azure.com:5432/astry_pos?sslmode=require" \
     -f db/setup_schema.sql
   ```
2. Set `CONVERSATION_STORE_URL` trong `.env` bằng connection string của role `booking_agent_svc`
   vừa tạo (không phải role `astry`) — script Cách A đã in sẵn dòng này cho bạn.
3. `PostgresConversationStore` tự `CREATE TABLE IF NOT EXISTS` trong schema `booking_agent` ở
   lần kết nối đầu tiên — không cần migration tool riêng. Restart server (`uvicorn src.server:app`)
   để nó đọc `CONVERSATION_STORE_URL` mới.

Vì sao dùng chung server nhưng vẫn an toàn: `booking_agent_svc` bị `REVOKE` tường minh mọi
quyền trên `public`, và `search_path` của role này bị khoá về `booking_agent` — về mặt kỹ thuật
nó không thể đọc/ghi `appointments`/`customers`/`staff` dù dùng chung 1 database. Đây KHÔNG
phải ngoại lệ cho quyết định "agent không ghi thẳng DB nghiệp vụ" — schema `booking_agent` chỉ
chứa lịch sử chat, chưa từng tồn tại trong `astry-pos-be`.

## Chạy local

```bash
cp .env.example .env   # điền AZURE_*, POS_API_BASE_URL, AGENT_API_KEY, POS_INTERNAL_API_KEY
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
uvicorn src.server:app --reload --port 8200
```

- `GET /health` → `{"status": "ok", "service": "astry-booking-agent"}`
- `POST /api/chat` → endpoint chat chính (SSE), body `{"message": "...", "conversation_id": "..."}`,
  header bắt buộc `Authorization: Bearer <JWT của Staff>` (và `X-API-Key: <AGENT_API_KEY>` nếu đã set).

## Test

```bash
pytest -q   # unit tests, mock qua respx, không gọi network thật

# integration test cho PostgresConversationStore (skip mặc định, cần Postgres thật):
docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
TEST_CONVERSATION_STORE_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_conversation_store.py
```

**Đã verify end-to-end thật** (không mock) nhắm vào `astry-pos-be` chạy local với `DEV_AUTH_BYPASS=true`
+ seed fixtures: `lookup_service` → `check_availability` → `create_appointment` (preview + confirm thật,
tạo appointment `CONFIRMED`) → `cancel_appointment`. Toàn bộ pass, response shape khớp đúng những gì đã
code. 2 phát hiện thật từ lần chạy này:
- `GET /availability` mất **~20s** cho lần gọi đầu (có thể do round-trip lặp lại tới Azure Postgres) —
  đã tăng timeout mặc định của `pos_client.py` từ 15s lên **30s** để không bị `ReadTimeout` giả. Đáng
  báo lại team backend nếu latency này lặp lại ở môi trường khác.
- `lookup_staff` trả rỗng khi test với seed fixtures mặc định của `astry-pos-be` — do fixture staff
  (Marcus/Natalie/Chelsea) không có `skillTags` nào, mà `GET /public/staff` lọc theo `skills.length≥1`.
  Đây là giới hạn của seed fixture (thiết kế cho e2e test riêng của họ), không phải bug ở agent.

## Deviation so với PLAN.md — cần biết trước khi deploy

1. **`GET /public/staff` KHÔNG dùng JWT forward.** Route này ở astry-pos-be dùng
   `@Public() + InternalApiKeyGuard` (`X-Internal-Api-Key`), không phải `JwtAuthGuard`. Vì vậy
   `lookup_staff` cần thêm `POS_INTERNAL_API_KEY` riêng (khác `AGENT_API_KEY`) — đây là điểm
   khác với quyết định kiến trúc ban đầu "100% qua JWT forward" ở PLAN.md mục 0.
2. **`Idempotency-Key` thực ra là bắt buộc**, dù Swagger doc ghi `required: false` — interceptor
   thật throw `400 IDEMPOTENCY_KEY_REQUIRED` nếu thiếu header. `pos_client.py` luôn tự sinh key
   nếu caller không truyền, nên việc này trong suốt với tool.
3. **`RolesGuard` (`@Roles('MANAGER')`) hiện KHÔNG được đăng ký** ở astry-pos-be (xác nhận qua
   grep repo) — bất kỳ user đã đăng nhập nào cũng gọi được các endpoint booking hiện tại, không
   chỉ MANAGER. Không phải vấn đề của agent, nhưng nên báo lại team backend.
4. **Conversation store**: `PostgresConversationStore` đã implement (xem mục "Conversation store"
   ở trên) nhưng **tắt theo mặc định** — chỉ bật khi set `CONVERSATION_STORE_URL`. Nếu để trống,
   agent vẫn rơi về `MemoryConversationStore` in-RAM của `vanna`, **chưa đáp ứng BR1-2 SRS** —
   production BẮT BUỘC phải set biến này.
5. **Session timeout 10 phút (BR1-3/BR2-7) chưa implement.**
6. Response 409 `BOOKING_CONFLICT` của `POST /appointments` **có sẵn** `details.suggestions`
   (`ALTERNATIVE_SLOT`/`ALTERNATIVE_STAFF`) — đã xác nhận, tool `create_appointment`/
   `reschedule_appointment` đọc thẳng từ đó, không cần tự gọi lại `check_availability`.
7. Chưa viết integration test nhắm vào `astry-pos-be` thật chạy local (chỉ có unit test mock
   qua `respx`) — xem PLAN.md Phase 8.
