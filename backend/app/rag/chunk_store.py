"""
分块向量的异步读写（与业务数据同事务）。

为什么单独一个模块
------------------
`RAGRetriever` 为了兼容"在线程池里跑"的调用方式，用的是**同步** SQLAlchemy 引擎；
而事务要求它和 `documents` 表的增删处在**同一个数据库事务**里。两者驱动不同，
无法共享连接，所以这里提供一组以 `AsyncSession` 为入参的协程，
由路由层在同一事务内调用：

    db.add(doc)                     # 业务记录
    await insert_chunks(db, rows)   # 向量
    await db.commit()               # 一次提交 —— 要么都成，要么都不成

`RAGRetriever` 仍保留同步实现，供重建索引、统计等非事务场景复用。
"""

from __future__ import annotations

import json
from typing import List, Sequence

from langchain_core.documents import Document
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_SQL = text(
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
)


def vec_literal(vector: Sequence[float]) -> str:
    """把向量转成 pgvector 字面量 `[0.1,0.2,...]`。"""
    return "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"


def build_rows(
    chunks: List[Document],
    vectors: List[Sequence[float]],
    doc_id: str,
    user_id: str,
) -> List[dict]:
    """把分块 + 向量组装成可直接批量写入的行。"""
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
                "embedding": vec_literal(vector),
                "meta": json.dumps(meta, ensure_ascii=False),
            }
        )
    return rows


async def insert_chunks(db: AsyncSession, rows: List[dict]) -> int:
    """在当前事务内写入分块向量，不提交。"""
    if not rows:
        return 0
    await db.execute(_INSERT_SQL, rows)
    return len(rows)


async def delete_chunks(db: AsyncSession, doc_id: str) -> int:
    """在当前事务内删除某文档的全部分块，不提交。"""
    result = await db.execute(
        text("DELETE FROM document_chunks WHERE doc_id = :doc_id"),
        {"doc_id": doc_id},
    )
    return int(result.rowcount or 0)
