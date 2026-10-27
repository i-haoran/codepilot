"""add document_chunks with pgvector

把向量存储从嵌入式 Chroma 迁到 PostgreSQL + pgvector。

建表与索引全部用原生 SQL 写：
  · `vector` 类型由扩展提供，不依赖 Python 侧的 pgvector 包，迁移在任何环境都能跑
  · HNSW 索引参数 m=16 / ef_construction=64，与 app/models/chunk.py 保持一致

Revision ID: 9f2c1a7b4d31
Revises: 4ef67b63e076
Create Date: 2026-10-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "9f2c1a7b4d31"
down_revision: Union[str, None] = "4ef67b63e076"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 512


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id           VARCHAR(128) PRIMARY KEY,
            doc_id       VARCHAR(36)  NOT NULL,
            user_id      VARCHAR(64)  NOT NULL,
            chunk_index  INTEGER      NOT NULL DEFAULT 0,
            content      TEXT         NOT NULL,
            embedding    vector({EMBEDDING_DIM}) NOT NULL,
            metadata     JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            created_at   TIMESTAMP    NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS ix_document_chunks_doc_id ON document_chunks (doc_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_chunks_user_id ON document_chunks (user_id)")

    # HNSW + cosine：与检索用的 `<=>` 运算符配套。数据量大时构建会慢，属一次性成本。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_chunks")
    # 扩展不主动删除：同一库里可能还有别的表在用
