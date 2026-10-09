"""
工具（Function Calling）实现。

替换原先 `supervisor._tool_worker` 里的占位实现：
    tool_result = f"[模拟工具调用] 查询结果: {message}"

这里的工具都操作真实数据（知识库分块 / 数据库文档与会话表），
通过 LangChain 的 `@tool` 装饰器暴露 JSON Schema，
由 LLM 自行决定调用哪个工具、传什么参数。
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from sqlalchemy import func, select

from app.models.chat import ChatHistory
from app.models.conversation import Conversation
from app.models.document import Document
from app.rag.retriever import RAGRetriever

logger = logging.getLogger(__name__)


# 运行期上下文（数据库会话 / 当前用户）通过 contextvar 传递：
# · 避免把 AsyncSession 塞进工具的 JSON Schema
# · contextvar 对每个 asyncio Task 独立，并发的 SSE 请求之间不会串用户数据
#   —— 这一点很关键：若用模块级全局变量，两个用户同时提问会互相读到对方的 user_id。
class ToolContext:
    """一次工具调用所需的运行期依赖。"""

    def __init__(self, db: Any, user_id: str) -> None:
        self.db = db
        self.user_id = user_id


_tool_context: contextvars.ContextVar[Optional[ToolContext]] = contextvars.ContextVar(
    "codepilot_tool_context", default=None
)


def set_tool_context(db: Any, user_id: str) -> None:
    _tool_context.set(ToolContext(db, user_id))


def clear_tool_context() -> None:
    _tool_context.set(None)


def _ctx() -> ToolContext:
    ctx = _tool_context.get()
    if ctx is None:
        raise RuntimeError("工具上下文未初始化：请先调用 set_tool_context(db, user_id)")
    return ctx


# ---------------------------------------------------------------------- #
# 工具定义
# ----------------------------------------------------------------------
@tool
async def list_user_documents() -> str:
    """列出当前用户知识库中的全部文档，包含文件名与分块数量。当用户询问"我上传了哪些文档""知识库里有什么"时使用。"""
    ctx = _ctx()
    result = await ctx.db.execute(
        select(Document)
        .where(Document.user_id == ctx.user_id)
        .order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()
    if not docs:
        return "当前用户的知识库为空，还没有上传任何文档。"

    lines = [f"共 {len(docs)} 份文档："]
    for index, doc in enumerate(docs, 1):
        lines.append(f"{index}. {doc.filename}（{doc.chunk_count} 个分块）")
    return "\n".join(lines)


@tool
async def get_knowledge_base_stats() -> str:
    """获取当前用户知识库的统计数据，包含文档总数、向量分块总数、会话总数与问答总次数。当用户询问"知识库有多少内容""我总共问了多少问题"等统计类问题时使用。"""
    ctx = _ctx()

    doc_count = await ctx.db.scalar(
        select(func.count()).select_from(Document).where(Document.user_id == ctx.user_id)
    )
    chunk_sum = await ctx.db.scalar(
        select(func.coalesce(func.sum(Document.chunk_count), 0)).where(Document.user_id == ctx.user_id)
    )
    conv_count = await ctx.db.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == ctx.user_id)
    )
    chat_count = await ctx.db.scalar(
        select(func.count()).select_from(ChatHistory).where(ChatHistory.user_id == ctx.user_id)
    )

    # 向量库实际分块数（与数据库记录可能存在偏差，正好用于体现一致性检查）
    vector_count = RAGRetriever().count(user_id=ctx.user_id)

    return (
        f"知识库统计：\n"
        f"· 文档数量：{doc_count or 0} 份\n"
        f"· 数据库记录分块数：{chunk_sum or 0}\n"
        f"· 向量库实际分块数：{vector_count}\n"
        f"· 会话数量：{conv_count or 0} 个\n"
        f"· 历史问答次数：{chat_count or 0} 次"
    )


@tool
async def search_document_by_filename(keyword: str) -> str:
    """按关键词模糊搜索当前用户知识库中的文档文件名，返回匹配的文档及其分块数。参数 keyword 为要匹配的文件名关键词。当用户询问"有没有关于 XX 的文档""某个文件在不在知识库里"时使用。"""
    ctx = _ctx()
    result = await ctx.db.execute(
        select(Document)
        .where(Document.user_id == ctx.user_id, Document.filename.ilike(f"%{keyword}%"))
        .order_by(Document.created_at.desc())
        .limit(20)
    )
    docs = result.scalars().all()
    if not docs:
        return f"没有找到文件名包含「{keyword}」的文档。"

    lines = [f"找到 {len(docs)} 份文件名包含「{keyword}」的文档："]
    for index, doc in enumerate(docs, 1):
        lines.append(f"{index}. {doc.filename}（{doc.chunk_count} 个分块）")
    return "\n".join(lines)


@tool
async def search_document_content(keyword: str) -> str:
    """在知识库全文（原始文档内容）中检索关键词，返回命中的文档与片段摘要。参数 keyword 为要检索的关键词。当用户问"哪份文档提到了 XX"这类需要跨文档定位内容的问题时使用。"""
    ctx = _ctx()
    result = await ctx.db.execute(
        select(Document)
        .where(Document.user_id == ctx.user_id, Document.content.ilike(f"%{keyword}%"))
        .limit(10)
    )
    docs = result.scalars().all()
    if not docs:
        return f"知识库的文档内容中没有检索到「{keyword}」。"

    lines = [f"文档内容中检索到「{keyword}」的命中情况："]
    for index, doc in enumerate(docs, 1):
        content = doc.content or ""
        pos = content.find(keyword)
        start = max(0, pos - 60)
        snippet = content[start:pos + 120].replace("\n", " ").strip()
        lines.append(f"{index}. 来自《{doc.filename}》：…{snippet}…")
    return "\n".join(lines)


# 暴露给 LLM 的工具列表
ALL_TOOLS: List[Any] = [
    list_user_documents,
    get_knowledge_base_stats,
    search_document_by_filename,
    search_document_content,
]

_TOOL_MAP: Dict[str, Any] = {t.name: t for t in ALL_TOOLS}


async def execute_tool(name: str, args: Dict[str, Any]) -> str:
    """按名称执行工具，返回文本结果。异常统一转为可读文本，交给 LLM 继续组织回答。"""
    tool_obj = _TOOL_MAP.get(name)
    if tool_obj is None:
        return f"[工具错误] 不存在名为 {name} 的工具。可用工具：{', '.join(_TOOL_MAP)}"
    try:
        result = await tool_obj.ainvoke(args or {})
        return str(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("工具 %s 执行失败: %s", name, exc, exc_info=True)
        return f"[工具错误] {name} 执行失败：{exc}"


def tool_names() -> List[str]:
    return list(_TOOL_MAP)
