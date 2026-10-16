# -*- coding: utf-8 -*-
"""
端到端自检脚本（不依赖 pytest）。

为什么需要它：当前机器的 Python 环境缺少 pytest / aiosqlite / slowapi，
且 PyPI 不可达。为了仍然能真实验证「应用能否导入、路由是否可用、
RAG 组件是否正常」，这里用 sqlite 内存库 + httpx ASGITransport 直接驱动应用。

用法：python verify_project.py
"""

import asyncio
import importlib.util
import os
import sys
import traceback

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_PROJECT_DIR, "backend"))
# .devshims 是本机依赖垫片目录（PyPI 不可达时的临时替代），仅在存在时加入路径
_devshims = os.path.join(_PROJECT_DIR, ".devshims")
if os.path.isdir(_devshims):
    sys.path.insert(0, _devshims)

PASS, FAIL, SKIP = [], [], []

HAVE_SLOWAPI = importlib.util.find_spec("slowapi") is not None
HAVE_AIOSQLITE = importlib.util.find_spec("aiosqlite") is not None


def record(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append((name, detail))
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  -> " + detail) if detail else ""))


def skip(name: str, reason: str):
    SKIP.append((name, reason))
    print(f"  [SKIP] {name}  -> {reason}")


def check(name):
    """装饰器：把用例结果收集起来，不让单个失败中断整轮。"""
    def wrapper(fn):
        async def runner():
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    result = await result
                detail = result if isinstance(result, str) else ""
                record(name, True, detail)
            except Exception as exc:  # noqa: BLE001
                record(name, False, f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
        RUNNERS.append(runner)
        return fn
    return wrapper


RUNNERS = []


# ---------------------------------------------------------------------- #
print("\n=== 1. 模块导入 ===")
# ---------------------------------------------------------------------- #
@check("导入 app.core.config")
def _():
    from app.core.config import settings
    return f"version={settings.APP_VERSION} embedding={settings.EMBEDDING_MODEL}"


@check("导入 app.rag.processor（原依赖 langchain_text_splitters）")
def _():
    from app.rag.processor import DocumentProcessor
    return DocumentProcessor().description


@check("导入 app.rag.embeddings")
def _():
    from app.rag.embeddings import embedding_service
    return embedding_service.dimension_hint()


@check("导入 app.rag.tools（Function Calling 工具集）")
def _():
    from app.rag.tools import ALL_TOOLS, tool_names
    return f"{len(ALL_TOOLS)} 个工具: {tool_names()}"


@check("导入 app.agents.supervisor（LangGraph 编排）")
def _():
    from app.agents.supervisor import SupervisorAgent
    agent = SupervisorAgent()
    nodes = sorted(agent.graph.get_graph().nodes)
    return f"可用（未发请求）节点={nodes}"


@check("导入 app.agents.verifier（审查 Agent）")
def _():
    from app.agents.verifier import VerifierAgent
    v = VerifierAgent()
    return f"可用，llm={type(v.llm).__name__}"


@check("导入 app.core.app（FastAPI 应用）")
def _():
    from app.core.app import app
    paths = sorted({r.path for r in app.routes if hasattr(r, "path")})
    return f"{len(paths)} 条路由（slowapi 可用={HAVE_SLOWAPI}）"

@check("导入 app.api.routes（不依赖 slowapi 的路由层）")
def _():
    from app.api.routes import router
    paths = sorted({r.path for r in router.routes})
    return f"{len(paths)} 条业务路由: {paths[:6]}..."


# ---------------------------------------------------------------------- #
print("\n=== 2. 文档分块 ===")
# ---------------------------------------------------------------------- #
@check("分块：长文本切分 + 元数据完整")
def _():
    from app.rag.processor import DocumentProcessor
    p = DocumentProcessor()
    text = "第一段内容，用于测试分块逻辑。" * 200
    chunks = p.process(text, metadata={"filename": "t.md", "user_id": "u1"})
    assert len(chunks) > 1, "长文本未切分"
    for i, c in enumerate(chunks):
        assert c.metadata["chunk_index"] == i
        assert c.metadata["total_chunks"] == len(chunks)
        assert c.metadata["filename"] == "t.md"
    return f"{len(text)} 字符 → {len(chunks)} 块，最大块 {max(len(c.page_content) for c in chunks)} 字符"


@check("分块：保留段落换行（原实现会把 \\n 压成空格导致分隔符失效）")
def _():
    from app.rag.processor import DocumentProcessor
    p = DocumentProcessor(chunk_size=40, chunk_overlap=5)
    text = "标题\n\n第一段。\n\n第二段。"
    chunks = p.process(text, metadata={})
    joined = "".join(c.page_content for c in chunks)
    assert "\n" in joined, "换行被清除，段落分隔符失效"
    return f"{len(chunks)} 块，含换行"


@check("分块：分块长度不超过 chunk_size + overlap")
def _():
    from app.rag.processor import DocumentProcessor
    p = DocumentProcessor(chunk_size=100, chunk_overlap=20)
    chunks = p.process("无分隔符的长文本" * 300, metadata={})
    limit = 100 + 20
    too_long = [len(c.page_content) for c in chunks if len(c.page_content) > limit]
    assert not too_long, f"存在超长分块: {too_long[:3]}"
    return f"{len(chunks)} 块，均 ≤ {limit}"


@check("分块：空文本返回空列表")
def _():
    from app.rag.processor import DocumentProcessor
    assert DocumentProcessor().process("", metadata={}) == []
    assert DocumentProcessor().process("   \n  ", metadata={}) == []
    return "正确返回空"


@check("分块：overlap 必须小于 chunk_size")
def _():
    from app.rag.processor import DocumentProcessor
    try:
        DocumentProcessor(chunk_size=100, chunk_overlap=100)
    except ValueError:
        return "正确抛出 ValueError"
    raise AssertionError("未校验 overlap >= chunk_size")


# ---------------------------------------------------------------------- #
print("\n=== 3. 图路由与流式文本提取 ===")
# ---------------------------------------------------------------------- #
@check("路由：按 next_worker 分发，缺字段兜底 direct_llm")
def _():
    from app.agents.supervisor import SupervisorAgent
    a = SupervisorAgent()
    for w in ("rag_worker", "tool_worker", "direct_llm"):
        assert a._route_after_supervisor({"next_worker": w}) == w
    assert a._route_after_supervisor({}) == "direct_llm"
    return "4 种输入均正确"


@check("流式：分片 content 兼容 str / list / None")
def _():
    from app.agents.supervisor import SupervisorAgent

    class C:
        def __init__(self, content):
            self.content = content

    assert SupervisorAgent._chunk_text(C("hello")) == "hello"
    assert SupervisorAgent._chunk_text(C([{"type": "text", "text": "a"}, "b"])) == "ab"
    assert SupervisorAgent._chunk_text(C(None)) == ""
    return "三种形态均正确解析"


@check("上下文组装：带来源文件名")
def _():
    from app.agents.supervisor import SupervisorAgent
    from langchain_core.documents import Document
    ctx = SupervisorAgent()._build_context([
        Document(page_content="A", metadata={"filename": "a.md"}),
        Document(page_content="B", metadata={}),
    ])
    assert "a.md" in ctx and "未知文档" in ctx
    return "含文件名与兜底名"


# ---------------------------------------------------------------------- #
print("\n=== 4. 缓存与降级 ===")
# ---------------------------------------------------------------------- #
@check("缓存：set / get / delete / miss")
async def _():
    from app.services.cache import CacheService
    c = CacheService()
    await c.clear()
    assert await c.get("未缓存") is None
    await c.set("问题", "答案")
    assert await c.get("问题") == "答案"
    await c.delete("问题")
    assert await c.get("问题") is None
    return "写入-命中-删除-未命中 均正确"


@check("缓存：确认为精确匹配（非语义缓存）")
async def _():
    from app.services.cache import CacheService
    c = CacheService()
    await c.clear()
    await c.set("如何配置 Redis", "答案")
    assert await c.get("如何配置 Redis") == "答案"
    assert await c.get("怎么配置 Redis") is None
    return "措辞不同不命中，符合 SHA-256 精确匹配语义"


@check("Redis 不可用时降级到内存缓存（无异常）")
async def _():
    from app.services.cache import CacheService, _memory_cache
    c = CacheService()
    c.redis = None            # 模拟 Redis 宕机
    _memory_cache.clear()
    await c.set("k", "v")
    got = await c.get("k")
    assert got == "v", "降级后读取失败"
    return "降级路径可用"


# ---------------------------------------------------------------------- #
print("\n=== 5. 工具（Function Calling）===")
# ---------------------------------------------------------------------- #
@check("工具 schema 可被 LLM 绑定（生成 JSON Schema）")
def _():
    from app.rag.tools import ALL_TOOLS
    for t in ALL_TOOLS:
        args_schema = getattr(t, "args_schema", None)
        assert t.name and t.description
        if args_schema is not None:
            args_schema.model_json_schema()
    return f"{[t.name for t in ALL_TOOLS]}"


@check("工具：未知工具名返回可读错误而非抛异常")
async def _():
    from app.rag.tools import execute_tool
    out = await execute_tool("not_exist", {})
    assert "不存在" in out
    return out[:60]


@check("工具：未初始化上下文时给出明确报错")
async def _():
    from app.rag.tools import clear_tool_context, execute_tool
    clear_tool_context()
    out = await execute_tool("list_user_documents", {})
    assert "工具错误" in out
    return out[:70]


# ---------------------------------------------------------------------- #
print("\n=== 6. 向量库与 Embedding ===")
# ---------------------------------------------------------------------- #
@check("Chroma 初始化 + embedding 绑定")
def _():
    from app.rag.retriever import RAGRetriever
    r = RAGRetriever()
    assert r.collection is not None, "collection 初始化失败"
    return f"collection={r.collection.name} embedding={r.embedding_source}"


@check("Cross-Encoder 重排（含降级路径）")
def _():
    from langchain_core.documents import Document
    from app.rag.reranker import Reranker
    docs = [
        Document(page_content="Python 是一种编程语言。", metadata={}),
        Document(page_content="今天天气不错，适合出门散步。", metadata={}),
        Document(page_content="FastAPI 是基于 Python 的异步 Web 框架。", metadata={}),
    ]
    out = Reranker().rerank("Python Web 框架", docs, top_k=2)
    assert len(out) == 2, f"重排后数量异常: {len(out)}"
    scores = [d.metadata.get("rerank_score") for d in out]
    return f"保留 {len(out)} 条，分数={scores}"


@check("向量写入 / 检索 / 按 doc_id 删除 闭环")
def _():
    from app.rag.processor import DocumentProcessor
    from app.rag.retriever import RAGRetriever
    from app.rag.reranker import Reranker

    r = RAGRetriever()
    chunks = DocumentProcessor(chunk_size=200, chunk_overlap=20).process(
        "CodePilot 使用 LangGraph 编排多 Agent。RAG 检索先用向量召回再用 Cross-Encoder 重排。" * 6,
        metadata={"filename": "loop.md"},
    )
    doc_id = "verify_doc_001"
    r.delete_by_doc_id(doc_id)
    written = r.add_chunks(chunks, doc_id=doc_id, user_id="verify_user")
    assert written == len(chunks), f"写入数不一致 {written} != {len(chunks)}"

    hits = r.search("多 Agent 编排", top_k=5, user_id="verify_user")
    assert hits, "检索无结果"
    assert all(h.metadata.get("user_id") == "verify_user" for h in hits), "用户隔离失效"

    top = Reranker().rerank("多 Agent 编排", hits, top_k=3)
    removed = r.delete_by_doc_id(doc_id)
    after = r.search("多 Agent 编排", top_k=5, user_id="verify_user")
    assert not after, f"删除后仍能检索到 {len(after)} 条"
    return f"写入 {written} 块 → 召回 {len(hits)} → 重排 {len(top)} → 删除 {removed} 条，删除后 0 命中"


# ---------------------------------------------------------------------- #
print("\n=== 7. 端到端 API（内存 SQLite）===")
# ---------------------------------------------------------------------- #
@check("数据模型：四张表元数据完整（字段 / 主键 / 索引）")
def _():
    from app.core.database import Base
    from app.models import ChatHistory, Conversation, Document, User  # noqa: F401

    tables = sorted(Base.metadata.tables)
    assert tables == ["chat_history", "conversations", "documents", "users"], f"表名异常: {tables}"

    user_cols = {c.name for c in Base.metadata.tables["users"].columns}
    assert {"id", "username", "password_hash", "avatar", "created_at"} <= user_cols, user_cols

    chat_cols = {c.name for c in Base.metadata.tables["chat_history"].columns}
    assert {"id", "user_id", "conversation_id", "message", "response"} <= chat_cols, chat_cols

    return f"表={tables}"


@check("密码哈希：bcrypt 加密且可校验（不落明文）")
def _():
    from app.core.auth import hash_password, verify_password
    raw = "my-secret-pw"
    hashed = hash_password(raw)
    assert hashed != raw and hashed.startswith("$2"), "未使用 bcrypt"
    assert verify_password(raw, hashed) is True
    assert verify_password("wrong-pw", hashed) is False
    return "哈希与校验正确（含错误密码拒绝）"


@check("JWT：签发 → 解码 → 篡改拒绝")
def _():
    from app.core.auth import create_token, decode_token
    token = create_token("user-123")
    assert decode_token(token) == "user-123"
    assert decode_token(token + "x") is None, "篡改后的 token 竟然通过校验"
    return "正常签发可解码，篡改被拒绝"


# ---------------------------------------------------------------------- #
print("\n=== 7. 端到端 API ===")
# ---------------------------------------------------------------------- #
PG_URL = os.environ.get(
    "VERIFY_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/codepilot_verify"
)


async def _e2e_api():
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.app import app
    from app.core.database import Base, get_db

    # 使用真实 PostgreSQL：与生产同一套驱动（asyncpg）和方言，
    # 比 SQLite 更能暴露 DDL / 类型 / 事务层面的问题。
    engine = create_async_engine(PG_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    steps = []
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/api/v1/auth/register", json={"username": "u_verify", "password": "pw123456"})
            assert r.status_code == 200, f"注册失败 {r.status_code}: {r.text[:200]}"
            token = r.json()["token"]
            steps.append("注册OK")

            r = await ac.post("/api/v1/auth/register", json={"username": "u_verify", "password": "pw123456"})
            assert r.status_code == 400, "重复用户名未被拒绝"
            steps.append("重名400")

            r = await ac.post("/api/v1/auth/login", json={"username": "u_verify", "password": "pw123456"})
            assert r.status_code == 200, f"登录失败 {r.text[:200]}"
            steps.append("登录OK")

            r = await ac.post("/api/v1/auth/login", json={"username": "u_verify", "password": "WRONG"})
            assert r.status_code == 401, "错误密码竟然登录成功"
            steps.append("错密码401")

            r = await ac.get("/api/v1/conversations")
            assert r.status_code == 401, "未鉴权竟然可访问"
            steps.append("未鉴权401")

            ac.headers["Authorization"] = f"Bearer {token}"
            r = await ac.get("/api/v1/auth/me")
            assert r.status_code == 200, f"me 失败 {r.text[:200]}"
            steps.append("鉴权OK")

            r = await ac.post("/api/v1/conversations")
            assert r.status_code == 200
            conv_id = r.json()["id"]
            steps.append("建会话OK")

            r = await ac.get("/api/v1/conversations")
            assert any(c["id"] == conv_id for c in r.json())
            steps.append("列会话OK")

            r = await ac.get("/health")
            assert r.status_code == 200 and r.json()["status"] == "healthy"
            steps.append(f"健康检查({r.json()['vector_store']})")

            r = await ac.get("/api/v1/documents/stats")
            assert r.status_code == 200
            steps.append("知识库统计OK")

            # 上传真实文本，走完整分块 + 向量写入链路
            body = ("CodePilot 使用 LangGraph 实现 Supervisor-Worker 多 Agent 编排。"
                    "RAG 检索先向量召回再用 Cross-Encoder 重排。") * 8
            r = await ac.post("/api/v1/documents",
                              files={"file": ("verify.md", body.encode("utf-8"), "text/markdown")})
            assert r.status_code == 200, f"上传失败 {r.status_code}: {r.text[:200]}"
            doc_id = r.json()["id"]
            chunk_count = r.json()["chunk_count"]
            assert chunk_count > 0, "分块数为 0"
            steps.append(f"上传文档({chunk_count}块)")

            r = await ac.get("/api/v1/documents")
            assert any(d["id"] == doc_id for d in r.json())
            steps.append("列文档OK")

            r = await ac.get("/api/v1/documents/stats")
            stats = r.json()
            steps.append(f"统计(doc={stats['document_count']},vec={stats['chunk_count_in_vector_store']})")

            r = await ac.delete(f"/api/v1/documents/{doc_id}")
            assert r.status_code == 200
            steps.append(f"删文档(清向量{r.json()['removed_vectors']})")

            r = await ac.post("/api/v1/documents", files={"file": ("empty.txt", b"", "text/plain")})
            assert r.status_code == 400, "空文件未被拒绝"
            steps.append("空文件400")

            r = await ac.get("/api/v1/avatar/..%2F..%2Frequirements.txt")
            assert r.status_code == 404, f"路径穿越未阻断: {r.status_code}"
            steps.append("路径穿越404")

            r = await ac.delete(f"/api/v1/conversations/{conv_id}")
            assert r.status_code == 200
            r = await ac.get("/api/v1/conversations")
            assert all(c["id"] != conv_id for c in r.json())
            steps.append("删会话OK")
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
    return " → ".join(steps)


async def run_e2e():
    name = f"端到端（真实 PostgreSQL）：注册→鉴权→会话→上传文档→检索→删除"
    try:
        detail = await _e2e_api()
        record(name, True, detail)
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ---------------------------------------------------------------------- #
print("\n=== 8. 多 Agent 协作与审查 Agent ===")
# ---------------------------------------------------------------------- #
@check("图结构：worker → verifier → revise → worker 构成真实回路")
def _():
    from app.agents.supervisor import SupervisorAgent
    g = SupervisorAgent().graph.get_graph()
    nodes = set(g.nodes)
    assert {"supervisor", "rag_worker", "tool_worker", "direct_llm",
            "verifier", "revise", "finalize"} <= nodes, nodes
    edges = {(e.source, e.target) for e in g.edges}
    for w in ("rag_worker", "tool_worker", "direct_llm"):
        assert (w, "verifier") in edges, f"{w} 未接入 verifier"
        assert ("revise", w) in edges, f"revise 无法回到 {w}"
    assert ("verifier", "revise") in edges
    assert ("verifier", "finalize") in edges
    return f"节点={len(nodes)} 边={len(edges)}，存在反馈回路"


@check("审查解析：通过 / 不通过 / Markdown 包裹 / 带解释文字")
def _():
    from app.agents.verifier import VerifierAgent
    cases = [
        ('{"pass": true, "reason": "有依据"}', True),
        ('{"pass": false, "reason": "无依据"}', False),
        ('```json\n{"pass": false, "reason": "答非所问"}\n```', False),
        ('判定如下：\n{"pass": true, "reason": "OK"}\n以上', True),
    ]
    for raw, expected in cases:
        v = VerifierAgent._parse(raw)
        assert v.passed is expected, f"{raw} → {v.passed}"
    return f"{len(cases)} 种输出形态均正确解析"


@check("审查解析：失败时放行但显式标记（不隐藏问题）")
def _():
    from app.agents.verifier import VerifierAgent
    for raw in ["完全不是 JSON", "", "{非法JSON}"]:
        v = VerifierAgent._parse(raw)
        assert v.passed is True, "应放行"
        assert v.parse_failed is True, "必须标记解析失败"
        assert v.label == "解析失败（放行）"
    return "3 种非法输入均放行且被标记"


@check("路由：通过→交付；不通过→重做；达上限→放弃重试")
def _():
    from app.agents.supervisor import MAX_REVISIONS, SupervisorAgent
    a = SupervisorAgent()
    assert a._route_after_verifier({"verdict_passed": True}) == "finalize"
    assert a._route_after_verifier({"verdict_passed": False, "revision_count": 0}) == "revise"
    assert a._route_after_verifier(
        {"verdict_passed": False, "revision_count": MAX_REVISIONS - 1}
    ) == "finalize"
    return f"三种分支正确（上限 {MAX_REVISIONS} 轮）"


@check("修订回到原分支，而不是重新分类")
def _():
    from app.agents.supervisor import SupervisorAgent
    a = SupervisorAgent()
    for w in ("rag_worker", "tool_worker", "direct_llm"):
        assert a._route_to_worker({"next_worker": w}) == w
    return "三条分支均正确回送"


@check("★ 最终保证：审查不通过时输出的必须是拒答，绝不能是原草稿")
async def _():
    from app.agents.supervisor import SupervisorAgent
    a = SupervisorAgent()
    fabricated = "知识库中有 3 份文档（这是编造的）"
    result = await a._finalize_node({
        "verdict_passed": False,
        "draft": fabricated,
        "verdict_reason": "文档数量在参考资料中找不到依据",
    })
    text = result["response_text"]
    assert "无法可靠地回答" in text, "未输出拒答话术"
    assert "3 份文档" not in text, "编造内容被泄漏给用户！"
    assert "找不到依据" in text, "未说明拒答原因"
    return "已拦截编造内容，并告知用户原因"


@check("拒答话术在无 reason 时也有兜底")
def _():
    from app.agents.verifier import Verdict, VerifierAgent
    text = VerifierAgent.build_refusal(Verdict(passed=False))
    assert "无法可靠地回答" in text and len(text) > 20
    return text.split("\n")[0]


@check("审查：草稿为空 → 不通过；无 Key → 跳过但不假装审查")
async def _():
    from app.agents import verifier as vm
    from app.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    original = vm.settings.DEEPSEEK_API_KEY
    try:
        vm.settings.DEEPSEEK_API_KEY = None
        v = await agent.review("问题", "草稿", "证据")
        assert v.passed is True and "未配置" in v.reason

        vm.settings.DEEPSEEK_API_KEY = "test-key"
        v2 = await agent.review("问题", "   ", "证据")
        assert v2.passed is False and "空" in v2.reason
    finally:
        vm.settings.DEEPSEEK_API_KEY = original
    return "未配置跳过 / 空草稿不通过 均正确"


@check("定稿分片可无损还原（前端打字机不丢字）")
def _():
    from app.agents.supervisor import SupervisorAgent
    text = "这是一段用于验证分片还原的文本，长度足够。"
    chunks = SupervisorAgent._iter_chunks(text, size=2)
    assert "".join(chunks) == text, "分片还原后文本不一致"
    assert all(len(c) <= 2 for c in chunks)
    return f"{len(text)} 字 → {len(chunks)} 片，无损还原"


# ---------------------------------------------------------------------- #
async def main():
    for runner in RUNNERS:
        await runner()
    await run_e2e()

    print("\n" + "=" * 74)
    print(f"通过 {len(PASS)} 项 ｜ 失败 {len(FAIL)} 项 ｜ 跳过 {len(SKIP)} 项")
    if SKIP:
        print("-" * 74)
        for name, reason in SKIP:
            print(f"  SKIPPED: {name}\n           {reason}")
    if FAIL:
        print("-" * 74)
        for name, detail in FAIL:
            print(f"  FAILED: {name}\n          {detail}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
