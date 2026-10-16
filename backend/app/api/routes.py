from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.supervisor import SupervisorAgent
from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.chat import ChatHistory
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.user import User
from app.rag.processor import DocumentProcessor
from app.rag.retriever import RAGRetriever
from app.services.cache import CacheService

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# slowapi 是可选依赖：缺失时降级为「不限流」，而不是让整个路由层无法导入。
# 原实现在模块顶层直接 import slowapi 并创建 Limiter，
# 一旦该包不可用，app.api.routes 导入失败 → 整个服务起不来。
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    limiter: Optional[Limiter] = Limiter(key_func=get_remote_address)
    CHAT_RATE_LIMIT = settings.CHAT_RATE_LIMIT

    def rate_limited(limit: str):
        """给路由挂限流装饰器；slowapi 缺失时退化为空装饰器。"""
        return limiter.limit(limit)

except ImportError:  # pragma: no cover - 仅在未安装 slowapi 的环境触发
    limiter = None
    CHAT_RATE_LIMIT = ""
    logger.warning("未安装 slowapi，接口限流已禁用（pip install slowapi 可启用）")

    def rate_limited(limit: str):
        def decorator(func):
            return func
        return decorator


router = APIRouter()


# ---------------------------------------------------------------------- #
# 请求 / 响应模型
# ---------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    stream: bool = True


class ChatResponse(BaseModel):
    run_id: str
    response: Optional[str] = None
    conversation_id: str
    status: str = "pending"
    cached: bool = False


class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class HistoryResponse(BaseModel):
    id: str
    message: str
    response: str
    created_at: str


class DocumentResponse(BaseModel):
    id: str
    filename: str
    chunk_count: int
    created_at: str


class AvatarResponse(BaseModel):
    avatar: str


class UpdateProfileRequest(BaseModel):
    username: Optional[str] = None


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #
DEFAULT_CONV_TITLE = "新会话"
TITLE_MAX_LEN = 50


def _derive_title(message: str) -> str:
    """由首条消息推导会话标题。"""
    title = " ".join((message or "").split()).strip()
    return title[:TITLE_MAX_LEN] or DEFAULT_CONV_TITLE


async def _resolve_conversation(
    db: AsyncSession, user: User, conv_id: Optional[str], first_message: str
) -> str:
    """
    解析本次问答所属会话，并保证「未命名的会话」被首条消息命名。

    原实现写成：
        if conv_id:
            return conv_id          # ← 早退，标题永远不更新
    而前端「新建会话」的流程是**先建会话、再发消息**，因此 conv_id 总是已存在，
    导致新建的会话标题永远停留在占位符，用户看到的就是「新会话」。
    正确做法是：无论会话是新建还是复用，只要它还是占位标题就补上命名。
    """
    if conv_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id, Conversation.user_id == user.id
            )
        )
        conv = result.scalar_one_or_none()
        if conv is None:
            # 会话不存在或不属于该用户：新建一个，避免把消息写进别人的会话
            conv = Conversation(user_id=user.id, title=_derive_title(first_message))
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            return conv.id

        # 复用已有会话：仍是占位标题则用首条消息命名
        if conv.title in (None, "", DEFAULT_CONV_TITLE):
            conv.title = _derive_title(first_message)
            await db.commit()
        return conv.id

    conv = Conversation(user_id=user.id, title=_derive_title(first_message))
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv.id


async def _load_history(db: AsyncSession, conv_id: str, limit: int = 20):
    result = await db.execute(
        select(ChatHistory)
        .where(ChatHistory.conversation_id == conv_id)
        .order_by(ChatHistory.created_at.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


# 从 agent 事件转发到 SSE 帧的**可选字段白名单**。
#
# 这里曾经是硬编码的两个字段（tool_calls），结果 agent 新增的
# answer_source / source_label 被静默丢弃——前端拿不到来源，
# 只能回落到默认值，导致所有回答都显示同一个标签。
# 改为集中维护的字段表，新增字段只需在此登记，避免再次漏转发。
SSE_PASSTHROUGH_FIELDS = ("tool_calls", "answer_source", "source_label")


def _build_sse_frame(event: dict) -> dict:
    """把 agent 内部事件转成对外的 SSE 帧（保留白名单内的可选字段）。"""
    frame = {"type": event.get("type"), "content": event.get("content")}
    for field in SSE_PASSTHROUGH_FIELDS:
        value = event.get(field)
        if value not in (None, "", [], {}):
            frame[field] = value
    return frame


# ---------------------------------------------------------------------- #
# 聊天
# ---------------------------------------------------------------------- #
@router.post("/chat", response_model=ChatResponse)
@rate_limited(settings.CHAT_RATE_LIMIT)
async def chat(
    request: Request,
    chat_request: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """非流式聊天接口：等待完整回答后一次性返回。"""
    run_id = str(uuid.uuid4())
    conv_id = await _resolve_conversation(db, user, chat_request.conversation_id, chat_request.message)
    history_records = await _load_history(db, conv_id)

    # 缓存键需包含 user_id，否则不同用户会命中彼此的答案
    cache = CacheService()
    cache_key = f"{user.id}:{chat_request.message}"
    cached_result = await cache.get(cache_key)
    if cached_result:
        logger.info("缓存命中 user=%s", user.id)
        return ChatResponse(
            run_id=run_id, response=cached_result, conversation_id=conv_id,
            status="completed", cached=True,
        )

    agent = SupervisorAgent()
    # 同步接口也传入 db：工具调用需要数据库会话，且同样走完整审查流程
    response = await agent.run(chat_request.message, user.id, history_records, db=db)
    await cache.set(cache_key, response)

    db.add(ChatHistory(
        id=run_id, user_id=user.id, conversation_id=conv_id,
        message=chat_request.message, response=response,
    ))
    await db.commit()

    return ChatResponse(run_id=run_id, response=response, conversation_id=conv_id, status="completed")


@router.post("/chat/stream")
@rate_limited(settings.CHAT_RATE_LIMIT)
async def chat_stream(
    request: Request,
    chat_request: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    SSE 流式聊天接口。

    事件帧（每帧 `data: {json}\\n\\n`）：
      {"type": "trace", "content": [...], "tool_calls": [...]}  执行轨迹 / 工具调用
      {"type": "token", "content": "增量文本"}                   逐 token 输出
      {"type": "conv_id", "content": "<会话ID>"}
      {"type": "error", "content": "错误信息"}
      {"type": "done"}                                          结束标记
    """
    run_id = str(uuid.uuid4())

    async def event_generator():
        full_response: list[str] = []
        conv_id = chat_request.conversation_id or ""
        try:
            conv_id = await _resolve_conversation(
                db, user, chat_request.conversation_id, chat_request.message
            )
            history_records = await _load_history(db, conv_id)

            agent = SupervisorAgent()
            async for event in agent.stream_with_trace(
                chat_request.message, user.id, history_records, db=db
            ):
                event_type = event.get("type")
                if event_type == "token":
                    full_response.append(event.get("content", ""))
                elif event_type == "done":
                    continue

                frame = _build_sse_frame(event)
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"

            complete_response = "".join(full_response)
            if complete_response:
                cache = CacheService()
                await cache.set(f"{user.id}:{chat_request.message}", complete_response)

                db.add(ChatHistory(
                    id=run_id, user_id=user.id, conversation_id=conv_id,
                    message=chat_request.message, response=complete_response,
                ))
                await db.commit()

        except Exception as exc:  # noqa: BLE001
            logger.error("流式接口异常: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type': 'conv_id', 'content': conv_id}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 关闭 Nginx 缓冲，否则流式会被攒批
        },
    )


# ---------------------------------------------------------------------- #
# 会话
# ---------------------------------------------------------------------- #
@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [
        ConversationResponse(
            id=c.id, title=c.title,
            created_at=c.created_at.isoformat(), updated_at=c.updated_at.isoformat(),
        )
        for c in result.scalars().all()
    ]


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = Conversation(user_id=user.id, title="新会话")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return ConversationResponse(
        id=conv.id, title=conv.title,
        created_at=conv.created_at.isoformat(), updated_at=conv.updated_at.isoformat(),
    )


@router.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == user.id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 批量删除，避免逐条 delete 产生 N 次 SQL
    msgs = await db.execute(select(ChatHistory).where(ChatHistory.conversation_id == conv_id))
    for msg in msgs.scalars().all():
        await db.delete(msg)
    await db.delete(conv)
    await db.commit()
    return {"detail": "已删除"}


@router.get("/conversations/{conv_id}/history", response_model=list[HistoryResponse])
async def get_conversation_history(
    conv_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatHistory)
        .where(ChatHistory.conversation_id == conv_id, ChatHistory.user_id == user.id)
        .order_by(ChatHistory.created_at.asc())
    )
    return [
        HistoryResponse(
            id=r.id, message=r.message, response=r.response,
            created_at=r.created_at.isoformat(),
        )
        for r in result.scalars().all()
    ]


# ---------------------------------------------------------------------- #
# 知识库文档
# ---------------------------------------------------------------------- #
@router.post("/documents", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    上传文档：解码 → 分块 → 写入向量库 → 落库元数据。

    注意：当前仅支持 UTF-8 文本类文件（txt / md / 代码等）。
    PDF / Word 需要额外接入解析库。
    """
    raw = await file.read()
    max_bytes = 5 * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="文件过大，请上传 5MB 以内的文本文件")

    text = raw.decode("utf-8", errors="ignore")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空或不是 UTF-8 文本")

    processor = DocumentProcessor()
    chunks = processor.process(text, metadata={"filename": file.filename, "user_id": user.id})
    if not chunks:
        raise HTTPException(status_code=400, detail="文档分块后为空，请检查文件内容")

    # 先确认向量库可用：否则会出现「数据库有记录、向量库没有」的假成功，
    # 而用户看到的只是"上传成功"，之后无论怎么提问都检索不到内容。
    retriever = RAGRetriever()
    if retriever.collection is None:
        raise HTTPException(
            status_code=503,
            detail="向量库当前不可用，文档未入库。请检查后端日志中的 Chroma / Embedding 错误。",
        )

    doc = Document(
        user_id=user.id,
        filename=file.filename,
        content=text,
        chunk_count=len(chunks),
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    written = retriever.add_chunks(chunks, doc_id=doc.id, user_id=user.id)

    # 向量写入不完整时**不要静默成功**：纠正记录值并如实告知，
    # 避免留下「chunk_count 说有 N 块、实际检索不到」的不一致状态。
    if written != len(chunks):
        logger.error(
            "向量写入不完整: written=%s chunks=%s doc_id=%s filename=%s",
            written, len(chunks), doc.id, doc.filename,
        )
        if doc.chunk_count != written:
            doc.chunk_count = written
            await db.commit()
        if written == 0:
            raise HTTPException(
                status_code=500,
                detail="文档已保存，但向量化失败，检索将无法命中。请查看后端日志。",
            )

    return DocumentResponse(
        id=doc.id, filename=doc.filename,
        chunk_count=doc.chunk_count, created_at=doc.created_at.isoformat(),
    )


@router.get("/documents", response_model=list[DocumentResponse])
async def list_documents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document)
        .where(Document.user_id == user.id)
        .order_by(Document.created_at.desc())
    )
    return [
        DocumentResponse(
            id=d.id, filename=d.filename,
            chunk_count=d.chunk_count, created_at=d.created_at.isoformat(),
        )
        for d in result.scalars().all()
    ]


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 先删向量再删元数据；向量删除失败不阻断（避免出现孤儿记录无法清理）
    removed = RAGRetriever().delete_by_doc_id(doc_id, chunk_count=doc.chunk_count)

    await db.delete(doc)
    await db.commit()
    return {"detail": "已删除", "removed_vectors": removed}


@router.get("/documents/stats")
async def knowledge_base_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """知识库统计：文档数、数据库分块数、向量库实际分块数（可用于一致性检查）。"""
    doc_count = await db.scalar(
        select(func.count()).select_from(Document).where(Document.user_id == user.id)
    )
    chunk_sum = await db.scalar(
        select(func.coalesce(func.sum(Document.chunk_count), 0)).where(Document.user_id == user.id)
    )
    return {
        "document_count": doc_count or 0,
        "chunk_count_in_db": chunk_sum or 0,
        "chunk_count_in_vector_store": RAGRetriever().count(user_id=user.id),
        "embedding": RAGRetriever().embedding_source,
    }


# ---------------------------------------------------------------------- #
# 个人中心
# ---------------------------------------------------------------------- #
@router.post("/upload-avatar", response_model=AvatarResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="头像请控制在 2MB 以内")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
    if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
        raise HTTPException(status_code=400, detail="仅支持 png / jpg / gif / webp 格式")

    upload_dir = Path(__file__).resolve().parent.parent.parent / "uploads" / "avatars"
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 用 user_id 命名，天然避免路径穿越（不使用用户传入的文件名）
    filename = f"{user.id}.{ext}"
    (upload_dir / filename).write_bytes(content)

    avatar_url = f"/api/v1/avatar/{filename}"
    result = await db.execute(select(User).where(User.id == user.id))
    u = result.scalar_one_or_none()
    if u:
        u.avatar = avatar_url
        await db.commit()
    return AvatarResponse(avatar=avatar_url)


@router.get("/profile")
async def get_profile(user: User = Depends(get_current_user)):
    return {"user_id": user.id, "username": user.username, "avatar": user.avatar or ""}


@router.put("/profile")
async def update_profile(
    req: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user.id))
    u = result.scalar_one_or_none()
    if u and req.username:
        u.username = req.username
        await db.commit()
    return {"detail": "已更新"}


@router.get("/avatar/{filename}")
async def get_avatar(filename: str):
    avatar_dir = Path(__file__).resolve().parent.parent.parent / "uploads" / "avatars"
    filepath = (avatar_dir / filename).resolve()

    # 防止 ../ 路径穿越读取任意文件
    if not str(filepath).startswith(str(avatar_dir.resolve())) or not filepath.exists():
        raise HTTPException(status_code=404, detail="头像不存在")
    return FileResponse(filepath)
