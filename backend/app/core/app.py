"""FastAPI 应用装配：中间件、路由、异常处理与前端静态托管。"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

import asyncio
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


async def _reconcile_in_background() -> None:
    """
    后台校验并修复知识库索引一致性。

    关键约束：**必须在创建 engine 的同一个事件循环里运行**。
    asyncpg 的连接绑定在创建它的事件循环上；若另起线程用 asyncio.run() 执行，
    会抛 `got Future attached to a different loop`（本项目实测踩到）。
    因此这里用 `asyncio.create_task` 留在原循环，
    而把其中**阻塞的部分**（embedding 模型加载、向量写入）在 reindex 内部
    用 `asyncio.to_thread` 下放，做到既不阻塞事件循环、又不跨循环使用连接。

    为什么必须放到后台：该校验会加载 embedding 模型并可能重建整库索引，
    实测约 28 秒。若在 lifespan 里同步执行，服务会一直卡在
    `Waiting for application startup.`，日志上看像是进程挂了。

    向量库可能因 embedding 模型变更、chroma_data 被删除、写入中断等原因被清空，
    而数据库仍保有文档记录与原文。此时检索会全部落空，用户表现为
    「知识库里有文档却问什么都答不出来」；引入审查 Agent 后更会以
    "诚实拒答"的形式出现，看起来像功能行为而非故障，因此必须自动修复。
    """
    try:
        from app.core.database import async_session
        from app.services.reindex import reconcile_knowledge_base

        async with async_session() as session:
            report = await reconcile_knowledge_base(session)
        if report:
            logger.warning(
                "后台重建知识库索引完成：修复 %s 份文档 / %s 个分块"
                "（修复前 数据库=%s 向量库=%s，修复后=%s）",
                report["documents"], report["chunks"],
                report["expected_before"], report["actual_before"], report["total_after"],
            )
        else:
            logger.info("知识库索引一致性校验通过，无需重建")
    except Exception as exc:  # noqa: BLE001 - 校验失败不影响服务
        logger.error("知识库索引一致性校验失败（不影响服务运行）: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # 以后台任务方式启动校验：不阻塞事件循环，启动日志与端口立即可用。
    reconcile_task = asyncio.create_task(_reconcile_in_background())
    logger.info("%s v%s 启动完成（索引校验在后台进行）", settings.APP_NAME, settings.APP_VERSION)
    try:
        yield
    finally:
        # 关闭时若后台任务仍在跑，等待其结束，避免索引写一半被中断
        if not reconcile_task.done():
            logger.info("等待后台索引校验完成……")
            try:
                await asyncio.wait_for(reconcile_task, timeout=60)
            except asyncio.TimeoutError:
                logger.warning("后台索引校验超时，已放弃等待")
            except Exception:  # noqa: BLE001
                pass


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
        "http://localhost:5173", "http://127.0.0.1:5173",   # Vite 默认端口
        "http://localhost:3000", "http://127.0.0.1:3000",   # 本项目 vite.config.ts 指定的端口
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
    """
    健康检查：暴露向量库 / embedding / LLM 配置状态，便于部署排查。

    注意这里**刻意不去实例化 RAGRetriever**：它的构造函数会加载 embedding 模型
    （首次约 28 秒），若在此触发，健康检查会长时间无响应，
    部署平台的 readiness/liveness 探针会误判服务不可用并反复重启容器。
    因此只在「检索器已就绪」时报告其状态，未就绪则如实标记为 warming_up。
    """
    from app.rag.embeddings import embedding_service
    from app.rag.retriever import RAGRetriever

    existing = RAGRetriever._instance  # 单例；为 None 表示尚未初始化
    if existing is None:
        vector_store, embedding = "warming_up", embedding_service.dimension_hint()
    else:
        vector_store = "ok" if existing.collection is not None else "unavailable"
        embedding = existing.embedding_source

    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "vector_store": vector_store,
        "embedding": embedding,
        "llm_configured": bool(settings.DEEPSEEK_API_KEY),
    }
