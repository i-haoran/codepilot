"""
Cross-Encoder 重排序器。

为什么要重排
-----------
向量检索（Chroma）本质是**双塔（bi-encoder）**结构：query 和 document 分别独立编码，
再做余弦相似度。优点是文档向量可以离线预计算、检索极快；缺点是无法建模
query 与 document 之间的词级交互，精度有上限。

Cross-Encoder 把 (query, document) 拼接后送进同一个模型，能充分建模交互，
精度显著更高，但**必须对每个候选实时前向推理**，无法预建索引。

因此采用两阶段：**双塔快速召回 Top-N → Cross-Encoder 精排 Top-K**，
在延迟与精度之间取得平衡。这是业界 RAG 的标准做法。

降级策略
-------
模型未安装 / 下载失败时 `_ensure_model()` 返回 False，
直接按原有顺序截断返回（退化为纯向量检索结果），保证链路不中断。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class Reranker:
    """Cross-Encoder 重排序（进程内单例，模型延迟加载）。"""

    _instance: Optional["Reranker"] = None

    def __new__(cls, model_name: str = DEFAULT_MODEL) -> "Reranker":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._model = None          # type: ignore[attr-defined]
            cls._instance._load_failed = False   # type: ignore[attr-defined]
            cls._instance.model_name = model_name  # type: ignore[attr-defined]
        return cls._instance

    # ------------------------------------------------------------------ #
    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
            logger.info("Cross-Encoder 加载成功: %s", self.model_name)
            return True
        except Exception as exc:  # noqa: BLE001
            self._load_failed = True
            logger.warning("Cross-Encoder 加载失败，退化为向量检索顺序: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    def rerank(self, query: str, documents: List[Document], top_k: int = 5) -> List[Document]:
        """
        对候选文档重排并返回 Top-K。

        重排分数写入 `metadata["rerank_score"]`，便于在 Agent 轨迹与日志中观测，
        也是排查“为什么召回了却答偏”的关键依据。
        """
        if not documents:
            return []
        if not self._ensure_model():
            return documents[:top_k]

        try:
            pairs = [(query, doc.page_content) for doc in documents]
            scores = self._model.predict(pairs)
        except Exception as exc:  # noqa: BLE001
            logger.error("重排推理失败，退化为向量检索顺序: %s", exc)
            return documents[:top_k]

        scored = sorted(zip(documents, scores), key=lambda item: float(item[1]), reverse=True)

        result: List[Document] = []
        for doc, score in scored[:top_k]:
            metadata = dict(doc.metadata or {})
            metadata["rerank_score"] = float(score)
            result.append(Document(page_content=doc.page_content, metadata=metadata))
        return result
