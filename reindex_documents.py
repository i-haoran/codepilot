# -*- coding: utf-8 -*-
"""
重建知识库索引。

背景：数据库 documents 表保存了文档原文，而 Chroma 只存向量与元数据。
一旦向量库被清空（例如 embedding 模型变更触发的 collection 重建），
数据库仍有记录与全文，但检索会全部落空——表现为「知识库里有文档，
但问什么都答不出来」。

本脚本从 documents.content 重新分块并写入向量库，实现无损重建。
用法：python reindex_documents.py
"""
import asyncio
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.rag.processor import DocumentProcessor  # noqa: E402
from app.rag.retriever import RAGRetriever  # noqa: E402


async def main():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    retriever = RAGRetriever()
    processor = DocumentProcessor()

    print(f"向量库: {retriever.collection.name if retriever.collection else '不可用'}")
    print(f"embedding: {retriever.embedding_source}")
    print(f"重建前分块总数: {retriever.count()}\n")

    async with Session() as db:
        docs = (await db.execute(select(Document))).scalars().all()
        if not docs:
            print("documents 表为空，无需重建")
            await engine.dispose()
            return

        total_written = 0
        for doc in docs:
            # 先清掉该文档可能残留的旧分块，保证重建幂等
            removed = retriever.delete_by_doc_id(doc.id, chunk_count=doc.chunk_count)
            chunks = processor.process(
                doc.content or "", metadata={"filename": doc.filename, "user_id": doc.user_id}
            )
            if not chunks:
                print(f"  [skip] {doc.filename}：内容为空，无法分块")
                continue
            written = retriever.add_chunks(chunks, doc_id=doc.id, user_id=doc.user_id)
            total_written += written
            print(f"  [ok] {doc.filename}: 清理旧块 {removed} → 写入 {written} 块"
                  f"（数据库记录 {doc.chunk_count}）")
            if written != doc.chunk_count:
                doc.chunk_count = written
        await db.commit()

    print(f"\n重建后分块总数: {retriever.count()}（本次写入 {total_written}）")

    # 重建后立即验证：能否检索到内容
    probe = "git 提交"
    hits = retriever.search(probe, top_k=3)
    print(f'\n检索验证 "{probe}": 命中 {len(hits)} 条')
    for i, h in enumerate(hits, 1):
        print(f"  {i}. {h.page_content[:60].replace(chr(10), ' ')} "
              f"(score={h.metadata.get('vector_score')})")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
