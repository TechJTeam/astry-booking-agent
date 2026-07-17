# Gọi API astry-booking-agent

Agent chạy trên FastAPI (framework `vanna`), mặc định port `8200`. Service này **không phải**
REST CRUD thông thường — chỉ có 1 endpoint chat chính, mọi thao tác (tra cứu dịch vụ, check lịch
trống, tạo/đổi/huỷ lịch hẹn...) đều đi qua hội thoại tự nhiên, agent tự chọn tool bên trong.

## 1. Auth — 2 lớp bắt buộc

| Header | Dùng để | Ai gọi |
|---|---|---|
| `X-API-Key: <AGENT_API_KEY>` | Xác thực service-to-service (chỉ POS frontend/BFF được gọi vào agent) | Bên gọi (POS frontend/BFF) |
| `Authorization: Bearer <JWT>` | JWT của **Staff** đang đăng nhập, forward nguyên vẹn từ POS frontend | Bên gọi, forward lại JWT Staff hiện có |

Chi tiết:

- `X-API-Key` được kiểm bởi `ApiKeyMiddleware` ([src/auth.py](../src/auth.py)) trên **mọi route**
  trừ `/health`, `/docs`, `/redoc`, `/openapi.json`, `/`. Nếu server chưa set `AGENT_API_KEY`
  (dev), middleware bỏ qua — **production bắt buộc phải set**. Thiếu/sai key → `401` JSON:
  ```json
  { "detail": "Unauthorized: thiếu hoặc sai X-API-Key." }
  ```
- `Authorization: Bearer <JWT>` **không được agent tự verify chữ ký** — agent chỉ decode phần
  payload để lấy `sub`/`email`/`salon_id`/`realm_access.roles`, rồi replay **y hệt** JWT đó khi
  gọi ngược vào `astry-pos-be` (astry-pos-be mới là nơi verify RS256/JWKS thật). Vì vậy JWT phải
  là JWT thật, còn hạn, do Keycloak cấp cho chính Staff đang thao tác — không phải service
  account riêng của agent.
- Thiếu `Authorization` header → agent **không trả HTTP 401**. Vì đây là endpoint SSE, lỗi
  `MissingJwtError` bị framework `vanna` bắt và phát ra như một **event lỗi trong stream** (xem
  mục 3.3), HTTP status vẫn là `200`. Client phải tự kiểm tra `type: "error"` trong stream, không
  thể dựa vào status code.

## 1.1 Swagger UI

FastAPI tự phát sinh, không cần cấu hình thêm. `ApiKeyMiddleware` đã whitelist các path này nên
xem được mà không cần `X-API-Key`:

- Swagger UI: `http://localhost:8200/docs`
- ReDoc: `http://localhost:8200/redoc`
- OpenAPI JSON: `http://localhost:8200/openapi.json`

Chỉ liệt kê được các route thật: `POST /api/vanna/v2/chat_sse`, `POST /api/vanna/v2/chat_poll`,
`GET /health` (route WebSocket không hiện trong OpenAPI). Nút "Try it out" gọi được thật, nhưng vì
`chat_sse` trả SSE nên response hiện nguyên khối `data: {...}` thay vì stream — đủ để test nhanh
header/body, không phải chỗ để build UI chat.

## 2. `GET /health`

Không cần auth. Dùng cho healthcheck (Docker/CI).

```bash
curl http://localhost:8200/health
```

```json
{ "status": "ok", "service": "astry-booking-agent" }
```

## 3. `POST /api/vanna/v2/chat_sse` — endpoint chính (Server-Sent Events)

### 3.1 Request

```
POST /api/vanna/v2/chat_sse
Content-Type: application/json
X-API-Key: <AGENT_API_KEY>
Authorization: Bearer <JWT của Staff>
```

Body:

```json
{
  "message": "Cắt tóc cho khách tên Lan lúc 3h chiều mai",
  "conversation_id": "c9f1b0a2-... (optional — không gửi lần đầu, agent tự sinh và trả về trong response)"
}
```

| Field | Bắt buộc | Ghi chú |
|---|---|---|
| `message` | ✔ | Câu Staff gõ, tiếng Việt tự nhiên |
| `conversation_id` | ✘ | Bỏ trống ở tin nhắn đầu tiên của 1 hội thoại; các lượt sau **phải** truyền lại đúng id đã nhận được, để agent nhớ ngữ cảnh (đặc biệt quan trọng cho pattern confirm-gate ở mục 4) |
| `request_id` | ✘ | Dùng để trace, tự sinh nếu bỏ trống |

### 3.2 Response — luồng SSE

Response là `text/event-stream`, mỗi sự kiện có dạng:

```
data: {"rich": {...}, "simple": {...}, "conversation_id": "...", "request_id": "...", "timestamp": 1737...}

```

Kết thúc bằng:

```
data: [DONE]

```

- `rich`: payload đầy đủ cho UI phong phú (component, có thể là text streaming, bảng, nút xác nhận...).
- `simple`: bản rút gọn (thường chỉ có text) cho UI đơn giản — có thể `null`.
- Client chỉ cần đọc `simple.text` (nếu có) hoặc gộp text từ `rich` để hiển thị dạng chat thường.

### 3.3 Sự kiện lỗi trong stream

Khi có exception (kể cả thiếu JWT — mục 1), server yield **một event lỗi** rồi đóng stream
**không có `[DONE]`**:

```
data: {"type": "error", "data": {"message": "Thiếu Authorization: Bearer <JWT>. POS frontend phải forward nguyên JWT của Staff đang đăng nhập khi gọi vào agent."}, "conversation_id": "", "request_id": ""}
```

Client nên: đọc từng `data:` line, nếu parse ra có `"type": "error"` → hiển thị lỗi và dừng, đừng
chờ `[DONE]`.

### 3.4 Ví dụ `curl`

```bash
curl -N -X POST http://localhost:8200/api/vanna/v2/chat_sse \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-astry-booking-to-db-agent-key" \
  -H "Authorization: Bearer $STAFF_JWT" \
  -d '{"message": "Cắt tóc cho khách tên Lan lúc 3h chiều mai"}'
```

(`-N` để curl không buffer, thấy stream đổ về theo thời gian thực.)

### 3.5 Ví dụ JavaScript (fetch + ReadableStream)

`EventSource` không hỗ trợ custom header (`X-API-Key`, `Authorization`), nên dùng `fetch` +
đọc stream thủ công thay vì `EventSource`:

```js
const res = await fetch('http://localhost:8200/api/vanna/v2/chat_sse', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-API-Key': AGENT_API_KEY,
    'Authorization': `Bearer ${staffJwt}`,
  },
  body: JSON.stringify({ message, conversation_id: conversationId }),
});

const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });

  const lines = buffer.split('\n\n');
  buffer = lines.pop(); // phần chưa trọn 1 event, giữ lại cho lần đọc sau

  for (const line of lines) {
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice('data: '.length);
    if (payload === '[DONE]') return;

    const chunk = JSON.parse(payload);
    if (chunk.type === 'error') {
      console.error('Agent error:', chunk.data.message);
      return;
    }
    conversationId = chunk.conversation_id; // nhớ lại cho lượt chat tiếp theo
    // render chunk.simple?.text hoặc chunk.rich tuỳ UI
  }
}
```

## 4. Endpoint thay thế (không phải endpoint chính, nhưng cùng framework `vanna`)

| Endpoint | Method | Ghi chú |
|---|---|---|
| `/api/vanna/v2/chat_poll` | POST | Cùng body như `chat_sse`, nhưng trả JSON đầy đủ 1 lần (không stream) — dùng khi client không tiện xử lý SSE. Request/response chậm hơn UX vì phải đợi agent trả lời xong hoàn toàn. |
| `/api/vanna/v2/chat_websocket` | WS | Tương đương `chat_sse` qua WebSocket, gửi JSON `{"message": "...", "conversation_id": "..."}` mỗi lượt. |

Body/auth giống hệt `chat_sse` (`chat_poll` nhận `X-API-Key` + `Authorization` header như bình
thường; `chat_websocket` nhận cookie/header lúc handshake).

## 5. Pattern confirm-gate — mọi thao tác GHI (tạo/đổi/huỷ lịch, thêm waitlist)

Agent **không bao giờ ghi dữ liệu ngay ở lượt gọi đầu**. Mọi tool ghi (`create_appointment`,
`reschedule_appointment`, `cancel_appointment`, `confirm_or_decline_pending_appointment`,
`join_waitlist`) có 2 bước, hoàn toàn nằm trong hội thoại tự nhiên — **client không cần biết chi
tiết tool nào đang chạy**, chỉ cần forward đúng `conversation_id` giữa các lượt:

1. **Lượt 1** — Staff gõ yêu cầu (vd "đặt lịch cắt tóc cho Lan 3h mai"). Agent tự gọi tool nội bộ
   với `confirm=false`, KHÔNG gọi API ghi nào tới `astry-pos-be`, chỉ trả lời lại bằng text hỏi
   xác nhận (vd "Xác nhận đặt lịch: khách Lan, 15:00 mai, dịch vụ Cắt tóc. Đúng không?").
2. **Lượt 2** — Staff trả lời đồng ý (vd "đúng rồi", "ok", "xác nhận") — client gửi tin nhắn này
   với **cùng `conversation_id`** ở lượt 1. Agent tự nhận diện ý xác nhận, gọi lại tool với
   `confirm=true`, lúc này mới thật sự `POST`/`PATCH` vào `astry-pos-be`.

→ Với client tích hợp: chỉ cần 1 luồng chat bình thường (gửi message, nhận reply, forward
`conversation_id`), không cần build UI xác nhận riêng — agent tự hỏi lại bằng ngôn ngữ tự nhiên.

## 6. Ví dụ luồng đầy đủ (2 lượt HTTP `chat_sse`)

```bash
# Lượt 1 — không truyền conversation_id
curl -N -X POST http://localhost:8200/api/vanna/v2/chat_sse \
  -H "Content-Type: application/json" -H "X-API-Key: $AGENT_API_KEY" \
  -H "Authorization: Bearer $STAFF_JWT" \
  -d '{"message": "Đặt lịch cắt tóc cho Lan, 3h chiều mai"}'
# -> agent hỏi lại xác nhận, trả về conversation_id="abc-123" trong mỗi chunk

# Lượt 2 — bắt buộc truyền lại conversation_id nhận được ở trên
curl -N -X POST http://localhost:8200/api/vanna/v2/chat_sse \
  -H "Content-Type: application/json" -H "X-API-Key: $AGENT_API_KEY" \
  -H "Authorization: Bearer $STAFF_JWT" \
  -d '{"message": "Xác nhận", "conversation_id": "abc-123"}'
# -> agent thật sự gọi POST /appointments vào astry-pos-be, trả lời kết quả tạo lịch
```

## 7. Lưu ý khi tích hợp

- **Không có endpoint REST rời cho từng nghiệp vụ** (không có `POST /agent/create-appointment`
  kiểu vậy) — tất cả đi qua `chat_sse`/`chat_poll`/`chat_websocket` bằng ngôn ngữ tự nhiên. Muốn
  agent làm gì thì soạn `message` tương ứng.
- **`conversation_id` là bắt buộc phải nhớ ở phía client** giữa các lượt trong cùng 1 hội thoại —
  agent không có cách nào khác để nối ngữ cảnh.
- JWT Staff hết hạn giữa chừng → lỗi verify xảy ra ở phía `astry-pos-be` khi agent gọi ngược lại
  (thường lộ ra dưới dạng tool trả `error: pos_api_error` với message 401 trong nội dung reply,
  không phải HTTP 401 ở tầng `chat_sse`). Client nên tự kiểm tra JWT còn hạn trước khi gọi.
- Mặc định (`CONVERSATION_STORE_URL` trống) lịch sử hội thoại **lưu in-RAM**, mất khi agent
  restart — client không nên coi `conversation_id` là bền vững qua deploy nếu biến này chưa set
  (xem [README.md](../README.md) mục "Conversation store").
