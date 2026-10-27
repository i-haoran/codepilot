"""
把已有向量从 Chroma 搬到 PostgreSQL（pgvector）。

为什么单独写脚本
----------------
迁移只做一次，不值得塞进业务代码。向量**原样复制**，不重新计算——因为
embedding 模型没变，同一份文本的向量在两边应当完全一致；重算既慢又可能因
模型版本漂移引入差异。

用法（在 backend 容器内执行）：

    python /scripts/migrate_chroma_to_pgvector.py --chroma /app/chroma_data

可选参数：
    --batch 500        每批写入条数
    --dry-run          只统计，不写库
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, "/app")

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402


def sync_url(url: str) -> str:
    return url.replace("+asyncpg", "+psycopg2")


def vec_literal(values) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in values) + "]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chroma", default="/app/chroma_data")
    ap.add_argument("--collection", default=settings.CHROMA_COLLECTION)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import chromadb

    if not os.path.isdir(args.chroma):
        print(f"Chroma 目录不存在: {args.chroma}", file=sys.stderr)
        return 2

    client = chromadb.PersistentClient(path=args.chroma)
    try:
        collection = client.get_collection(args.collection)
    except Exception as exc:  # noqa: BLE001
        print(f"读取 collection 失败: {exc}", file=sys.stderr)
        return 2

    data = collection.get(include=["documents", "metadatas", "embeddings"])
    ids = data.get("ids") or []
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    embeddings = data.get("embeddings")

    total = len(ids)
    dim = len(embeddings[0]) if embeddings is not None and len(embeddings) else 0
    print(f"Chroma 分块数: {total}  维度: {dim}")

    if args.dry_run:
        return 0

    if total == 0:
        print("无数据可迁移")
        return 0

    if dim != settings.EMBEDDING_DIM:
        print(
            f"维度不匹配：Chroma 是 {dim}，当前配置 EMBEDDING_DIM={settings.EMBEDDING_DIM}。"
            "换模型的场景必须重新向量化，不能直接搬运。",
            file=sys.stderr,
        )
        return 3

    engine = create_engine(sync_url(settings.DATABASE_URL), pool_pre_ping=True)

    rows = []
    for i, chunk_id in enumerate(ids):
        meta = dict(metas[i] or {}) if i < len(metas) else {}
        doc_id = str(meta.get("doc_id") or "")
        user_id = str(meta.get("user_id") or "")
        if not doc_id or not user_id:
            print(f"跳过缺少 doc_id/user_id 的分块: {chunk_id}", file=sys.stderr)
            continue
        rows.append(
            {
                "id": chunk_id,
                "doc_id": doc_id,
                "user_id": user_id,
                "chunk_index": int(meta.get("chunk_index") or 0),
                "content": docs[i] if i < len(docs) else "",
                "embedding": vec_literal(embeddings[i]),
                "meta": json.dumps(meta, ensure_ascii=False),
            }
        )

    written = 0
    with engine.begin() as conn:
        for start in range(0, len(rows), args.batch):
            batch = rows[start:start + args.batch]
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
                batch,
            )
            written += len(batch)
            print(f"  已写入 {written}/{len(rows)}")

    with engine.connect() as conn:
        db_count = conn.execute(text("SELECT count(*) FROM document_chunks")).scalar()

    print(f"完成：写入 {written} 条，库中现有 {db_count} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
