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
AI Booking Agent for Astry POS — salon staff type booking requests in natural language, the
agent looks up services/staff/availability and creates/reschedules/cancels appointments through
`astry-pos-be`'s REST API (the agent never writes to the database directly).

**There is no separate CRUD endpoint per business action.** Every operation goes through one of
the two chat endpoints below (`/api/chat` or `/api/chat_poll`) with `message` as a natural
language sentence — the agent picks the right tool internally (lookup_service,
check_availability, create_appointment, ...).

### Auth required on every route (except `/health`, `/docs`, `/redoc`, `/openapi.json`)

| Header | Value | Purpose |
|---|---|---|
| `X-API-Key` | `AGENT_API_KEY` (configured in `.env`) | Service-to-service auth — only the POS frontend/BFF is allowed to call the agent |
| `Authorization` | `Bearer <Staff JWT>` | The real JWT Keycloak issued to the logged-in Staff member, forwarded as-is from the POS frontend — the agent **does not verify the signature itself**, it only decodes the payload then replays this JWT when calling back into `astry-pos-be` |

Missing `X-API-Key` → a normal `401` JSON. Missing/invalid `Authorization` → **does not** return
`401` (since `/api/chat` is SSE, the HTTP status is always `200`), instead an error event is sent
in the stream — see the `/api/chat` endpoint description below for details.

### Confirm-gate pattern (every WRITE operation: create/reschedule/cancel appointment, join waitlist)

The agent never writes data on the very first chat turn:

1. **Turn 1** — Staff types a request → the agent replies with a confirmation question (preview),
   **without calling** any write API on `astry-pos-be`.
2. **Turn 2** — Staff confirms (e.g. "yes", "confirm") → the client sends this message with the
   **same `conversation_id`** received in turn 1 → only then does the agent actually
   `POST`/`PATCH` `astry-pos-be`.

→ Integrating clients just need a normal chat flow, remembering to forward `conversation_id`
between turns — no need to build a separate confirmation UI.

See full `curl`/JavaScript examples in [`docs/API.md`](https://github.com/TechJTeam/astry-booking-agent/blob/master/docs/API.md).
"""
app.openapi_tags = [
    {"name": "Chat", "description": "Main endpoint — every booking operation goes through here in natural language."},
    {"name": "Health", "description": "Healthcheck, no auth required."},
]

# VannaFastAPIServer.create_app() already registers by default:
#   - GET /health (returns {"service": "vanna"}, not what this agent wants)
#   - POST/WS /api/vanna/v2/chat_sse, chat_poll, chat_websocket (the vanna framework's default paths)
# The first route registered wins in Starlette, so old routes must be removed before adding
# replacements: override /health with our own response, and shorten /api/vanna/v2/* -> /api/*
# (chat_sse also renamed to plain /api/chat) — same chat_handler underneath, no behavior change,
# only the URL changes.
#
# Note: vanna's default demo UI at GET / (CDN widget) hardcodes the old /api/vanna/v2/chat_sse
# path, so it will stop working after this rename — acceptable since this agent's real client is
# the POS frontend (calling /api/chat directly), not that demo page.
app.router.routes = [
    r
    for r in app.router.routes
    if getattr(r, "path", None) != "/health" and not (getattr(r, "path", "") or "").startswith("/api/vanna/v2")
]


@app.get("/health", tags=["Health"], summary="Check whether the service is alive")
async def health():
    """Used for Docker `HEALTHCHECK` / load balancers. No auth header required."""
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
    "/api/chat",
    tags=["Chat"],
    summary="Chat with the Booking Agent — streaming (Server-Sent Events)",
    description="""
Main endpoint. Send `message` (a natural-language sentence), receive back a stream of SSE events.

**Request body**
```json
{ "message": "Book a haircut for Lan tomorrow at 3pm", "conversation_id": "..." }
```
- `conversation_id`: omit on the first message; subsequent turns **must** pass back the exact id
  received in the previous response, so the agent keeps context (required for the confirm-gate
  pattern).

**Response** — `text/event-stream`, each line:
```
data: {"rich": {...}, "simple": {"text": "..."}, "conversation_id": "...", "request_id": "...", "timestamp": 173...}
```
terminated by `data: [DONE]`. Read `simple.text` (if present) to render a plain chat bubble.

**Error events** (including a missing `Authorization: Bearer <JWT>`) — HTTP status stays `200`,
but the stream only has 1 event and **no `[DONE]`**:
```
data: {"type": "error", "data": {"message": "..."}, "conversation_id": "", "request_id": ""}
```
Clients must check for `"type": "error"` in each chunk themselves, they cannot rely on the HTTP
status code.

`curl` example (`-N` disables buffering):
```bash
curl -N -X POST http://localhost:8200/api/chat \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: <AGENT_API_KEY>" \\
  -H "Authorization: Bearer <Staff JWT>" \\
  -d '{"message": "Book a haircut for Lan tomorrow at 3pm"}'
```

See [`docs/API.md`](https://github.com/TechJTeam/astry-booking-agent/blob/master/docs/API.md) for
the full confirm-gate 2-turn walkthrough and a JavaScript (`fetch` + `ReadableStream`) example.
""",
    responses={
        200: {
            "description": "SSE stream (see description) — Swagger UI shows it as one raw text blob, not a live stream.",
            "content": {
                "text/event-stream": {
                    "example": 'data: {"rich": {"text": "Confirm booking: customer Lan, 3:00pm tomorrow. Correct?"}, '
                    '"simple": {"text": "Confirm booking: customer Lan, 3:00pm tomorrow. Correct?"}, '
                    '"conversation_id": "conv_ab12cd34", "request_id": "..."}\n\ndata: [DONE]\n\n'
                }
            },
        }
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    # request_context/metadata intentionally omitted here — clients don't need to
                    # fill this in, the server builds the real request_context from the actual
                    # HTTP request's headers/cookies (see _build_request_context below), so
                    # anything sent for this field in the body is fully overwritten anyway.
                    "example": {
                        "message": "Book a haircut for Lan tomorrow at 3pm",
                        "conversation_id": None,
                    }
                }
            }
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
    summary="Chat with the Booking Agent — polling (no streaming)",
    description="""
Same body/auth as `/api/chat`, but waits for the agent to **finish completely** before returning
one full JSON response (no streaming) — use when the client can't easily handle SSE. Worse UX
latency than `/api/chat`.

Response: `{"chunks": [...], "conversation_id": "...", "request_id": "...", "total_chunks": N}`.
""",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "message": "Book a haircut for Lan tomorrow at 3pm",
                        "conversation_id": None,
                    }
                }
            }
        }
    },
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
    """WebSocket endpoint for real-time chat (not shown in Swagger — OpenAPI can't describe WS).

    Body sent on each turn is identical to `/api/chat`: `{"message": "...", "conversation_id": "..."}`.
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
