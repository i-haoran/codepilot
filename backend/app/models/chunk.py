"""
向量分块表（pgvector）。

为什么放在 PostgreSQL 里
------------------------
原实现用嵌入式 Chroma，向量与业务数据分属两个存储，删文档时"删库记录"和
"删向量"无法放进同一个事务，写入失败会留下「库里有记录、向量没写全」的不一致。
改为 pgvector 后，向量就是 PG 里的一张普通表，可以和 documents 表同事务提交。

一行 = 一个分块：
  · id          由 `{doc_id}_chunk_{i}` 推导，便于按文档批量删除
  · embedding   512 维（bge-small-zh-v1.5），HNSW + cosine 索引
  · metadata    原始分块元数据（section_path / chunk_index / doc_title 等）
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.database import Base


class DocumentChunk(Base):
    """文档分块的向量表。"""

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(settings.EMBEDDING_DIM))
    # 属性名不能叫 metadata（SQLAlchemy 保留），列名仍叫 metadata
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # HNSW + cosine：与检索时使用的 `<=>`（余弦距离）运算符配套
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    @staticmethod
    def make_id(doc_id: str, index: int) -> str:
        return f"{doc_id}_chunk_{index}"
