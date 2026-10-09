"""FastAPI 应用装配：中间件、路由、异常处理与前端静态托管。"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

import logging
import time

from app.api.auth import router as auth_router
from app.api.routes import router
from app.core.config import settings
from app.core.database import init_db
from app.models import ChatHistory, Conversation, Document, User  # noqa: F401 注册模型

# slowapi 为可选依赖：未安装时禁用限流，但服务仍可正常启动
# （原实现顶层强依赖，缺包时应用直接 import 失败）。
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    HAVE_SLOWAPI = True
except ImportError:  # pragma: no cover
    HAVE_SLOWAPI = False

logger = logging.getLogger(__name__)

# backend/app/core/app.py → 上溯三层得到项目根目录（backend 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _read_frontend_html() -> str:
    """
    读取前端入口 HTML。

    原实现直接在模块顶层读取，路径写死为“上溯四层”，一旦启动目录不对
    就会在 import 阶段抛 FileNotFoundError，导致整个应用起不来。
    这里改为多候选路径 + 优雅降级，并在缺少构建产物时给出明确提示。
    """
    candidates = [
        PROJECT_ROOT / "frontend" / "dist" / "index.html",   # 生产构建产物
        PROJECT_ROOT / "frontend" / "index.html",            # 开发入口
    ]
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取前端入口失败 %s: %s", path, exc)

    logger.warning(
        "未找到前端入口文件（已查找 %s）。后端接口正常可用，"
        "请先在 frontend 目录执行 npm run build 生成 dist。",
        "、".join(str(p) for p in candidates),
    )
    return (
        "<html><head><meta charset='utf-8'><title>CodePilot</title></head><body>"
        "<h2>CodePilot 后端已启动</h2>"
        "<p>未检测到前端构建产物。</p>"
        "<p>请在 <code>frontend</code> 目录执行 <code>npm install &amp;&amp; npm run build</code>，"
        "或使用 <code>npm run dev</code> 启动开发服务器（默认 http://localhost:5173）。</p>"
        "<p>接口文档：<a href='/docs'>/docs</a></p>"
        "</body></html>"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("%s v%s 启动完成", settings.APP_NAME, settings.APP_VERSION)
    yield


limiter = None
if HAVE_SLOWAPI:
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"],
    )
else:
    logger.warning("未安装 slowapi，全局限流已禁用（pip install slowapi 可启用）")

app = FastAPI(
    title=settings.APP_NAME,
    description="基于 LangGraph 多 Agent 编排 + RAG 检索增强生成的研发知识助手",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

if HAVE_SLOWAPI:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",   # Vite 开发服务器
        "http://localhost:8080", "http://127.0.0.1:8080",   # 后端自身托管前端时
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/v1/auth", tags=["认证"])
app.include_router(router, prefix="/api/v1")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000, 2)
    logger.info("%s %s → %s (%sms)", request.method, request.url.path, response.status_code, duration)
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("未处理异常: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请稍后重试"})


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return _read_frontend_html()


@app.get("/health")
async def health_check():
    """健康检查：额外暴露向量库与缓存依赖状态，便于部署排查。"""
    from app.rag.retriever import RAGRetriever

    retriever = RAGRetriever()
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "vector_store": "ok" if retriever.collection is not None else "unavailable",
        "embedding": retriever.embedding_source,
        "llm_configured": bool(settings.DEEPSEEK_API_KEY),
    }
