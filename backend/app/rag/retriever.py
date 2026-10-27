"""
RAG 向量检索器（pgvector 实现）。

与旧版（嵌入式 Chroma）的差异
------------------------------
· 向量存进 PostgreSQL 的 `document_chunks` 表，和 `documents` 表在同一个库，
  删除文档时"删记录"与"删向量"可以放进同一个事务，不再依赖应用层补偿。
· 使用**同步** SQLAlchemy 引擎：本类的所有方法都会被调用方通过
  `asyncio.to_thread(...)` 放进线程池，避免阻塞事件循环；因此这里不用异步驱动。

对外接口保持不变（add_chunks / search / delete_by_doc_id / count / embedding_source），
上层 agent、路由、重建索引逻辑无需改动。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import List, Optional

from langchain_core.documents import Document
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.rag.embeddings import embedding_service

logger = logging.getLogger(__name__)


def _sync_url(url: str) -> str:
    """把异步驱动的 URL 换成同步驱动（asyncpg → psycopg2）。"""
    return url.replace("+asyncpg", "+psycopg2")


def _vec_literal(vector: List[float]) -> str:
    """把 Python 列表转成 pgvector 认的字面量 `[0.1,0.2,...]`。"""
    return "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"


class RAGRetriever:
    """pgvector 向量访问层（进程内单例）。"""

    _instance: Optional["RAGRetriever"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "RAGRetriever":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False  # type: ignore[attr-defined]
                    cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self.engine: Optional[Engine] = None
        self.available: bool = False
        self.embedding_source: str = embedding_service.dimension_hint()
        self._init_engine()
        self._initialized = True

    # ------------------------------------------------------------------ #
    def _init_engine(self) -> None:
        try:
            self.engine = create_engine(
                _sync_url(settings.DATABASE_URL),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
            )
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            self.available = True
            logger.info("pgvector 初始化成功: embedding=%s", self.embedding_source)
        except Exception as exc:  # noqa: BLE001
            logger.error("pgvector 初始化失败，RAG 检索将不可用: %s", exc)
            self.engine = None
            self.available = False

    # ------------------------------------------------------------------ #
    # 写入 / 删除
    # ------------------------------------------------------------------ #
    def add_chunks(self, chunks: List[Document], doc_id: str, user_id: str) -> int:
        """写入一个文档的所有分块，返回写入数量。"""
        if not self.available or self.engine is None or not chunks:
            return 0

        texts = [chunk.page_content for chunk in chunks]
        vectors = embedding_service.embed_documents(texts)

        rows = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            meta = dict(chunk.metadata or {})
            meta.update({"doc_id": doc_id, "user_id": user_id, "chunk_index": index})
            rows.append(
                {
                    "id": f"{doc_id}_chunk_{index}",
                    "doc_id": doc_id,
                    "user_id": user_id,
                    "chunk_index": index,
                    "content": chunk.page_content,
                    "embedding": _vec_literal(vector),
                    "meta": json.dumps(meta, ensure_ascii=False),
                }
            )

        # 单次批量插入；id 冲突时覆盖，保证重复导入幂等
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO document_chunks
                        (id, doc_id, user_id, chunk_index, content, embedding, metadata)
                    VALUES
                        (:id, :doc_id, :user_id, :chunk_index, :content,
                         CAST(:embedding AS vector), CAST(:meta AS jsonb))
                    ON CONFLICT (id) DO UPDATE SET
                        content   = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        metadata  = EXCLUDED.metadata
                    """
                ),
                rows,
            )
        return len(rows)

    def delete_by_doc_id(self, doc_id: str, chunk_count: Optional[int] = None) -> int:
        """按 doc_id 删除该文档的所有分块。"""
        if not self.available or self.engine is None:
            return 0
        try:
            with self.engine.begin() as conn:
                result = conn.execute(
                    text("DELETE FROM document_chunks WHERE doc_id = :doc_id"),
                    {"doc_id": doc_id},
                )
                return int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除分块失败: %s", exc)
            return 0

    def count(self, user_id: Optional[str] = None) -> int:
        """统计分块数量，可按用户过滤。"""
        if not self.available or self.engine is None:
            return 0
        try:
            sql = "SELECT count(*) FROM document_chunks"
            params = {}
            if user_id:
                sql += " WHERE user_id = :user_id"
                params["user_id"] = user_id
            with self.engine.connect() as conn:
                return int(conn.execute(text(sql), params).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("统计分块数量失败: %s", exc)
            return 0

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def search(self, query: str, top_k: int = 10, user_id: Optional[str] = None) -> List[Document]:
        """
        向量召回，返回带相似度分数的 Document 列表。

        相似度 = 1 - 余弦距离，与旧实现（Chroma `1 - distance`）保持一致，
        便于上层阈值过滤沿用同一套数值。
        """
        if not self.available or self.engine is None:
            return []

        try:
            vector = _vec_literal(embedding_service.embed_query(query))
        except Exception as exc:  # noqa: BLE001
            logger.error("查询向量化失败: %s", exc)
            return []

        sql = (
            "SELECT content, metadata, "
            "       1 - (embedding <=> CAST(:vector AS vector)) AS score "
            "FROM document_chunks "
        )
        params = {"vector": vector, "top_k": top_k}
        if user_id:
            sql += "WHERE user_id = :user_id "
            params["user_id"] = user_id
        sql += "ORDER BY embedding <=> CAST(:vector AS vector) LIMIT :top_k"

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(text(sql), params).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.error("向量检索失败: %s", exc)
            return []

        documents: List[Document] = []
        for row in rows:
            metadata = dict(row["metadata"] or {})
            metadata["vector_score"] = round(float(row["score"]), 4)
            documents.append(Document(page_content=row["content"], metadata=metadata))
        return documents
