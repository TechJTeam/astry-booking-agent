import json
import os
import traceback
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from vanna.core.user.request_context import RequestContext
from vanna.servers.base import ChatRequest, ChatResponse
from vanna.servers.fastapi import VannaFastAPIServer

from src.agent_setup import build_agent
from src.auth import ApiKeyMiddleware

load_dotenv()
agent = build_agent()

_server_wrapper = VannaFastAPIServer(agent)
app = _server_wrapper.create_app()
app.add_middleware(ApiKeyMiddleware)

app.title = "Astry Booking Agent API"
app.version = "1.0.0"
app.description = """
AI Booking Agent cho Astry POS — nhân viên salon gõ yêu cầu đặt lịch bằng ngôn ngữ tự nhiên,
agent tự tra cứu dịch vụ/nhân viên/lịch trống và tạo/đổi/huỷ lịch hẹn qua REST API của
`astry-pos-be` (agent không ghi thẳng database).

**Không có endpoint CRUD rời cho từng nghiệp vụ.** Mọi thao tác đều đi qua 1 trong 2 endpoint chat
bên dưới (`/api/chat_sse` hoặc `/api/chat_poll`) bằng `message` dạng câu tiếng Việt tự nhiên —
agent tự chọn tool bên trong (lookup_service, check_availability, create_appointment, ...).

### Auth bắt buộc trên mọi route (trừ `/health`, `/docs`, `/redoc`, `/openapi.json`)

| Header | Giá trị | Mục đích |
|---|---|---|
| `X-API-Key` | `AGENT_API_KEY` (cấu hình ở `.env`) | Xác thực service-to-service — chỉ POS frontend/BFF được gọi vào agent |
| `Authorization` | `Bearer <JWT của Staff>` | JWT thật do Keycloak cấp cho Staff đang đăng nhập, forward nguyên vẹn từ POS frontend — agent **không tự verify chữ ký**, chỉ decode payload rồi replay JWT này khi gọi ngược vào `astry-pos-be` |

Thiếu `X-API-Key` → `401` JSON bình thường. Thiếu/sai `Authorization` → **không** trả `401` (vì
`/api/chat_sse` là SSE, HTTP status luôn `200`) mà trả về 1 event lỗi trong stream, xem mô tả chi
tiết ở endpoint `/api/chat_sse` bên dưới.

### Pattern confirm-gate (mọi thao tác GHI: tạo/đổi/huỷ lịch, thêm waitlist)

Agent không bao giờ ghi dữ liệu ngay ở lượt chat đầu tiên:

1. **Lượt 1** — Staff gõ yêu cầu → agent trả lời bằng câu hỏi xác nhận (preview), **chưa gọi** API
   ghi nào tới `astry-pos-be`.
2. **Lượt 2** — Staff xác nhận đồng ý (vd "đúng rồi", "ok") → client gửi tin nhắn này với **cùng
   `conversation_id`** đã nhận ở lượt 1 → agent mới thật sự `POST`/`PATCH` vào `astry-pos-be`.

→ Client tích hợp chỉ cần 1 luồng chat bình thường, nhớ forward `conversation_id` giữa các lượt —
không cần tự dựng UI xác nhận riêng.

Xem thêm ví dụ `curl`/JavaScript đầy đủ tại [`docs/API.md`](https://github.com/TechJTeam/astry-booking-agent/blob/master/docs/API.md).
"""
app.openapi_tags = [
    {"name": "Chat", "description": "Endpoint chính — mọi nghiệp vụ đặt lịch đi qua đây bằng ngôn ngữ tự nhiên."},
    {"name": "Health", "description": "Healthcheck, không cần auth."},
]

# VannaFastAPIServer.create_app() đã tự đăng ký sẵn:
#   - GET /health (trả {"service": "vanna"}, không đúng ý agent này)
#   - POST/WS /api/vanna/v2/chat_sse, chat_poll, chat_websocket (path mặc định của framework vanna)
# Route đăng ký trước thắng trong Starlette nên phải xoá route cũ trước khi thêm route thay thế:
# đổi /health theo response riêng, và rút gọn path /api/vanna/v2/* -> /api/* (giữ nguyên
# chat_handler bên dưới, không đổi hành vi, chỉ đổi URL).
#
# Lưu ý: trang demo UI mặc định của vanna ở GET / (widget CDN) hardcode gọi path
# /api/vanna/v2/chat_sse cũ nên sẽ không hoạt động sau khi đổi path — chấp nhận được vì client
# thật của agent này là POS frontend (gọi thẳng /api/chat_sse), không phải trang demo đó.
app.router.routes = [
    r
    for r in app.router.routes
    if getattr(r, "path", None) != "/health" and not (getattr(r, "path", "") or "").startswith("/api/vanna/v2")
]


@app.get("/health", tags=["Health"], summary="Kiểm tra service còn sống")
async def health():
    """Dùng cho Docker `HEALTHCHECK` / load balancer. Không cần header auth nào."""
    return {"status": "ok", "service": "astry-booking-agent"}


chat_handler = _server_wrapper.chat_handler


def _build_request_context(chat_request: ChatRequest, http_request: Request) -> RequestContext:
    return RequestContext(
        cookies=dict(http_request.cookies),
        headers=dict(http_request.headers),
        remote_addr=http_request.client.host if http_request.client else None,
        query_params=dict(http_request.query_params),
        metadata=chat_request.metadata,
    )


@app.post(
    "/api/chat_sse",
    tags=["Chat"],
    summary="Chat với Booking Agent — streaming (Server-Sent Events)",
    description="""
Endpoint chính. Gửi `message` (câu tiếng Việt tự nhiên), nhận về stream các sự kiện SSE.

**Request body**
```json
{ "message": "Cắt tóc cho khách tên Lan lúc 3h chiều mai", "conversation_id": "..." }
```
- `conversation_id`: bỏ trống ở tin nhắn đầu tiên; các lượt sau **phải** truyền lại đúng id đã
  nhận được ở response trước đó, để agent nhớ ngữ cảnh (bắt buộc cho pattern confirm-gate).

**Response** — `text/event-stream`, mỗi dòng:
```
data: {"rich": {...}, "simple": {"text": "..."}, "conversation_id": "...", "request_id": "...", "timestamp": 173...}
```
kết thúc bằng `data: [DONE]`. Đọc `simple.text` (nếu có) để hiển thị dạng chat thường.

**Sự kiện lỗi** (kể cả thiếu `Authorization: Bearer <JWT>`) — HTTP status vẫn `200`, nhưng stream
chỉ có 1 event và **không có `[DONE]`**:
```
data: {"type": "error", "data": {"message": "..."}, "conversation_id": "", "request_id": ""}
```
Client phải tự kiểm tra `"type": "error"` trong từng chunk, không thể dựa vào HTTP status code.

Ví dụ `curl` (`-N` để không buffer):
```bash
curl -N -X POST http://localhost:8200/api/chat_sse \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: <AGENT_API_KEY>" \\
  -H "Authorization: Bearer <JWT của Staff>" \\
  -d '{"message": "Cắt tóc cho khách tên Lan lúc 3h chiều mai"}'
```

Chi tiết pattern confirm-gate 2 lượt + ví dụ JavaScript (`fetch` + `ReadableStream`) xem
[`docs/API.md`](https://github.com/TechJTeam/astry-booking-agent/blob/master/docs/API.md).
""",
    responses={
        200: {
            "description": "SSE stream (xem mô tả) — Swagger UI hiện nguyên khối text, không stream trực quan.",
            "content": {
                "text/event-stream": {
                    "example": 'data: {"rich": {"text": "Xác nhận đặt lịch: khách Lan, 15:00 mai. Đúng không?"}, '
                    '"simple": {"text": "Xác nhận đặt lịch: khách Lan, 15:00 mai. Đúng không?"}, '
                    '"conversation_id": "conv_ab12cd34", "request_id": "..."}\n\ndata: [DONE]\n\n'
                }
            },
        }
    },
)
async def chat_sse(chat_request: ChatRequest, http_request: Request) -> StreamingResponse:
    """Server-Sent Events endpoint for streaming chat."""
    chat_request.request_context = _build_request_context(chat_request, http_request)

    async def generate() -> AsyncGenerator[str, None]:
        try:
            async for chunk in chat_handler.handle_stream(chat_request):
                yield f"data: {chunk.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            traceback.print_exc()
            error_data = {
                "type": "error",
                "data": {"message": str(e)},
                "conversation_id": chat_request.conversation_id or "",
                "request_id": chat_request.request_id or "",
            }
            yield f"data: {json.dumps(error_data)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/chat_poll",
    tags=["Chat"],
    summary="Chat với Booking Agent — polling (không stream)",
    description="""
Cùng body/auth như `/api/chat_sse`, nhưng đợi agent trả lời **xong hoàn toàn** rồi mới trả về 1
JSON đầy đủ (không stream) — dùng khi client không tiện xử lý SSE. Chậm hơn UX so với `chat_sse`.

Response: `{"chunks": [...], "conversation_id": "...", "request_id": "...", "total_chunks": N}`.
""",
)
async def chat_poll(chat_request: ChatRequest, http_request: Request) -> ChatResponse:
    """Polling endpoint for chat."""
    chat_request.request_context = _build_request_context(chat_request, http_request)
    try:
        return await chat_handler.handle_poll(chat_request)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")


@app.websocket("/api/chat_websocket")
async def chat_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time chat (không hiện trong Swagger — OpenAPI không mô tả WS).

    Body mỗi lượt gửi lên giống hệt `/api/chat_sse`: `{"message": "...", "conversation_id": "..."}`.
    """
    await websocket.accept()

    try:
        while True:
            try:
                data = await websocket.receive_json()
                metadata = data.get("metadata", {})
                data["request_context"] = RequestContext(
                    cookies=dict(websocket.cookies),
                    headers=dict(websocket.headers),
                    remote_addr=websocket.client.host if websocket.client else None,
                    query_params=dict(websocket.query_params),
                    metadata=metadata,
                )
                chat_request = ChatRequest(**data)
            except Exception as e:
                traceback.print_exc()
                await websocket.send_json({"type": "error", "data": {"message": f"Invalid request: {str(e)}"}})
                continue

            try:
                async for chunk in chat_handler.handle_stream(chat_request):
                    await websocket.send_json(chunk.model_dump())

                await websocket.send_json(
                    {
                        "type": "completion",
                        "data": {"status": "done"},
                        "conversation_id": chunk.conversation_id if "chunk" in locals() else "",
                        "request_id": chunk.request_id if "chunk" in locals() else "",
                    }
                )
            except Exception as e:
                traceback.print_exc()
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {"message": str(e)},
                        "conversation_id": chat_request.conversation_id or "",
                        "request_id": chat_request.request_id or "",
                    }
                )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        traceback.print_exc()
        try:
            await websocket.send_json({"type": "error", "data": {"message": f"WebSocket error: {str(e)}"}})
        except Exception:
            pass
        finally:
            await websocket.close()


if __name__ == "__main__":
    port = int(os.getenv("AGENT_SERVER_PORT", "8200"))
    _server_wrapper.run(host="0.0.0.0", port=port)
