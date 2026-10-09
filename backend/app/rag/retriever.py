"""
RAG 向量检索器。

职责
----
· 管理 Chroma 持久化客户端与 collection（进程内单例，避免每请求重建）
· 写入文档分块（带 user_id / doc_id 元数据，用于用户级隔离与按文档删除）
· 向量召回 Top-K 候选，供上层 Cross-Encoder 重排

设计取舍
--------
原实现每个 API 请求都会 `RAGRetriever()`，而构造函数里会创建
`chromadb.PersistentClient`（持有文件句柄与索引缓存）。高频请求下这是明显浪费，
且多实例可能拿到不一致的索引状态。这里改为进程内单例。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, List, Optional

import chromadb
from langchain_core.documents import Document

from app.core.config import settings
from app.rag.embeddings import embedding_service

logger = logging.getLogger(__name__)


class RAGRetriever:
    """Chroma 向量库访问层（单例）。"""

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
        # 单例只初始化一次
        if getattr(self, "_initialized", False):
            return
        self.client: Optional[Any] = None
        self.collection: Optional[Any] = None
        self.embedding_source: str = "none"
        self._init_client()
        self._initialized = True

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _init_client(self) -> None:
        try:
            chroma_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "chroma_data",
            )
            os.makedirs(chroma_dir, exist_ok=True)

            self.client = chromadb.PersistentClient(path=chroma_dir)
            self.collection = self._get_or_create_collection()
            logger.info("Chroma 初始化成功: path=%s embedding=%s",
                        chroma_dir, self.embedding_source)
        except Exception as exc:  # noqa: BLE001
            logger.error("Chroma 初始化失败，RAG 检索将不可用: %s", exc)
            self.client = None
            self.collection = None

    def _get_or_create_collection(self) -> Any:
        """
        创建 collection，显式绑定中文 embedding 模型。

        Chroma 不允许对已存在的 collection 更改 embedding function / 维度。
        当检测到已存索引与当前配置**维度不一致**（例如从默认英文模型 384 维
        切到中文模型 512 维）时，继续使用只会让每次读写都抛
        `Collection expecting embedding with dimension of X, got Y`。

        因此这里做自愈：备份并重建 collection，同时把旧数据量打印出来，
        让使用者明确知道需要重新上传文档重建索引。
        """
        assert self.client is not None
        name = settings.CHROMA_COLLECTION
        ef = embedding_service.as_chroma_function()
        self.embedding_source = embedding_service.dimension_hint() if ef else "chroma-default"
        metadata = {"hnsw:space": "cosine", "embedding_model": self.embedding_source}

        try:
            existing = self.client.get_collection(name)
        except Exception:
            existing = None  # collection 不存在，正常创建

        if existing is not None:
            recorded = (existing.metadata or {}).get("embedding_model")
            if recorded == self.embedding_source:
                # 关键修复：Chroma 重新打开「已存在」的 collection 时，
                # 不会恢复构造时传入的 embedding_function，而是回落到内置的
                # DefaultEmbeddingFunction（all-MiniLM-L6-v2，384 维英文模型）。
                # 结果就是：第一次写入正常，进程重启后读写全部报
                # "expecting embedding with dimension of 512, got 384"。
                # 因此这里必须显式把 embedding function 重新挂回去。
                if ef is not None:
                    existing._embedding_function = ef
                return existing

            # 模型不一致：确认维度是否真的冲突，再决定是否重建
            old_count = 0
            try:
                old_count = existing.count()
            except Exception:  # noqa: BLE001
                pass

            logger.warning(
                "collection '%s' 的 embedding 与当前配置不一致（索引=%s，当前=%s，已有 %s 个分块）。"
                "正在重建 collection 以保证检索可用；原有向量需要重新上传文档才能恢复。",
                name, recorded, self.embedding_source, old_count,
            )
            try:
                self.client.delete_collection(name)
            except Exception as exc:  # noqa: BLE001
                logger.error("删除旧 collection 失败，将沿用旧集合（检索可能报维度错误）: %s", exc)
                if ef is not None:
                    existing._embedding_function = ef
                return existing

        kwargs: dict[str, Any] = {"name": name, "metadata": metadata}
        if ef is not None:
            kwargs["embedding_function"] = ef
        collection = self.client.get_or_create_collection(**kwargs)
        if ef is not None:
            collection._embedding_function = ef
        return collection

    # ------------------------------------------------------------------ #
    # 写入 / 删除
    # ------------------------------------------------------------------ #
    def add_chunks(self, chunks: List[Document], doc_id: str, user_id: str) -> int:
        """写入一个文档的所有分块，返回写入数量。"""
        if not self.collection or not chunks:
            return 0

        ids, docs, metas = [], [], []
        for index, chunk in enumerate(chunks):
            metadata = dict(chunk.metadata or {})
            metadata.update({"doc_id": doc_id, "user_id": user_id, "chunk_index": index})
            ids.append(f"{doc_id}_chunk_{index}")
            docs.append(chunk.page_content)
            metas.append(metadata)

        # 单次批量写入，避免逐条 add 带来的 N 次索引提交
        self.collection.add(ids=ids, documents=docs, metadatas=metas)
        return len(ids)

    def delete_by_doc_id(self, doc_id: str, chunk_count: Optional[int] = None) -> int:
        """
        按 doc_id 删除该文档的所有分块。

        优先用元数据过滤删除（不依赖 chunk_count 是否准确）；
        失败时回退到按 id 前缀构造的方式。
        """
        if not self.collection:
            return 0
        try:
            existing = self.collection.get(where={"doc_id": doc_id}, include=[])
            ids = existing.get("ids") or []
            if ids:
                self.collection.delete(ids=ids)
                return len(ids)
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("按 doc_id 删除向量失败，回退到 id 前缀方式: %s", exc)

        if not chunk_count:
            return 0
        chunk_ids = [f"{doc_id}_chunk_{i}" for i in range(chunk_count)]
        try:
            self.collection.delete(ids=chunk_ids)
            return len(chunk_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回退删除向量同样失败: %s", exc)
            return 0

    def count(self, user_id: Optional[str] = None) -> int:
        """统计分块数量，可按用户过滤。供 Tool Calling 使用。"""
        if not self.collection:
            return 0
        try:
            if user_id:
                return len(self.collection.get(where={"user_id": user_id}, include=[]).get("ids") or [])
            return self.collection.count()
        except Exception as exc:  # noqa: BLE001
            logger.warning("统计向量数量失败: %s", exc)
            return 0

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def search(self, query: str, top_k: int = 10, user_id: Optional[str] = None) -> List[Document]:
        """
        向量召回，返回带相似度分数的 Document 列表。

        注意：这里返回的是**召回候选**，尚未重排；调用方应交给 Reranker 精排。
        """
        if not self.collection:
            return []

        params: dict[str, Any] = {"query_texts": [query], "n_results": top_k}
        if user_id:
            params["where"] = {"user_id": user_id}

        try:
            results = self.collection.query(**params)
        except Exception as exc:  # noqa: BLE001
            logger.error("向量检索失败: %s", exc)
            return []

        documents: List[Document] = []
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]
        for i, text in enumerate(docs):
            metadata = dict(metas[i]) if i < len(metas) and metas[i] else {}
            if i < len(dists) and dists[i] is not None:
                # cosine 距离 → 相似度，便于在 trace 里展示与阈值过滤
                metadata["vector_score"] = round(1.0 - float(dists[i]), 4)
            documents.append(Document(page_content=text, metadata=metadata))
        return documents
