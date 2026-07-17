import os

from dotenv import load_dotenv
from vanna.servers.fastapi import VannaFastAPIServer

from src.agent_setup import build_agent
from src.auth import ApiKeyMiddleware

load_dotenv()
agent = build_agent()

_server_wrapper = VannaFastAPIServer(agent)
app = _server_wrapper.create_app()
app.add_middleware(ApiKeyMiddleware)

# VannaFastAPIServer.create_app() đã tự đăng ký GET /health riêng (trả {"service": "vanna"}).
# Route đăng ký trước thắng trong Starlette nên override bên dưới sẽ bị route đó che mất nếu
# không gỡ nó trước — xoá route cũ rồi mới thêm route /health của chính agent này.
app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != "/health"]


@app.get("/health")
async def health():
    return {"status": "ok", "service": "astry-booking-agent"}


if __name__ == "__main__":
    port = int(os.getenv("AGENT_SERVER_PORT", "8200"))
    _server_wrapper.run(host="0.0.0.0", port=port)
