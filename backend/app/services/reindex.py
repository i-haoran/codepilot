"""
知识库索引自愈。

问题背景
--------
数据库 `documents` 表保存了文档**原文**（`content` 字段），而 Chroma 只保存
向量与元数据（`filename` / `user_id` / `doc_id` 等），**不保存原文**。

因此一旦向量库被清空，就会进入一种很隐蔽的不一致状态：

    数据库：documents 有记录，chunk_count = 11   ← 看起来一切正常
    向量库：0 个分块                              ← 检索永远落空

用户看到的症状是「知识库里有文档，但问什么都答不出来」。
在引入审查 Agent 之后，这类问题的表现形式会变成**诚实拒答**
（「资料中没有相关信息」），看起来不像故障，更难被发现。

向量库会在什么情况下被清空
--------------------------
1. embedding 模型变更（例如从 Chroma 默认英文模型切到中文模型）时，
   `RAGRetriever` 会重建 collection；
2. 手动删除 `chroma_data` 目录；
3. 索引写入过程中进程中断。

只要数据库里还有原文，**这些情况都属于可无损重建**——本模块就是做这件事。

设计取舍
--------
`RAGRetriever` 不做重建，因为它是一个纯基础设施层，不便反向依赖数据库模型。
把「读原文 → 分块 → 写向量」编排放在 services 层，职责更清晰，也便于单独测试。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.rag.splitter_factory import get_processor
from app.rag.retriever import RAGRetriever

logger = logging.getLogger(__name__)


async def reconcile_knowledge_base(
    db: AsyncSession, *, force: bool = False
) -> Optional[Dict[str, int]]:
    """
    校验并修复「数据库分块数」与「向量库实际分块数」的一致性。

    :param force: 为 True 时无条件重建所有文档的索引；否则只在检出不一致时重建
    :return: 统计信息；无需修复时返回 None

    ⚠️ 该协程必须在**创建 engine 的同一个事件循环**中运行。
    asyncpg 的连接绑定在创建它的事件循环上，若放到其它线程用 `asyncio.run()`
    执行，会抛 `got Future attached to a different loop`。
    因此这里所有**阻塞操作**（模型加载、向量写入）都用 `asyncio.to_thread` 下放，
    数据库会话仍留在原循环里使用。

    一致性判定不做逐文档比对（那需要为每份文档各查一次向量库），
    而是先比较总量：`documents.chunk_count` 之和 vs 向量库实际总数。
    总量一致就认为无需修复，避免每次启动都做昂贵的全量检查。
    """
    # 构造 RAGRetriever 会同步加载 embedding 模型（约数十秒），必须下放到线程，
    # 否则会阻塞事件循环，表现为服务"启动了但不响应"。
    retriever = await asyncio.to_thread(RAGRetriever)
    if retriever.collection is None:
        logger.warning("向量库不可用，跳过知识库一致性校验")
        return None

    docs = (await db.execute(select(Document))).scalars().all()
    if not docs:
        return None

    expected = sum(int(d.chunk_count or 0) for d in docs)
    actual = await asyncio.to_thread(retriever.count)

    if not force and expected == actual:
        return None

    logger.warning(
        "检出知识库索引不一致（数据库记录 %s 块，向量库实际 %s 块），开始从原文重建……",
        expected, actual,
    )
    processor = get_processor()
    repaired_docs = 0
    repaired_chunks = 0

    for doc in docs:
        content = doc.content or ""
        if not content.strip():
            continue

        chunks = processor.process(
            content, metadata={"filename": doc.filename, "user_id": doc.user_id}
        )
        if not chunks:
            continue

        # 幂等：先清掉该文档可能残留的分块，再重新写入。
        # 向量化（最耗时的一步）在线程池中完成，不阻塞事件循环。
        await asyncio.to_thread(retriever.delete_by_doc_id, doc.id, doc.chunk_count)
        written = await asyncio.to_thread(
            retriever.add_chunks, chunks, doc.id, doc.user_id
        )

        if written != (doc.chunk_count or 0):
            doc.chunk_count = written

        repaired_docs += 1
        repaired_chunks += written
        logger.info("已重建索引：%s → %s 块", doc.filename, written)

    await db.commit()
    total_after = await asyncio.to_thread(retriever.count)
    logger.info(
        "知识库索引重建完成：修复 %s 份文档，共 %s 块（重建后总量 %s）",
        repaired_docs, repaired_chunks, total_after,
    )
    return {
        "documents": repaired_docs,
        "chunks": repaired_chunks,
        "expected_before": expected,
        "actual_before": actual,
        "total_after": total_after,
    }
