"""
文本向量化服务（Embedding）。

设计目标
--------
1. **中文优先**：Chroma 在未指定 embedding function 时会退化到内置的
   `all-MiniLM-L6-v2`（英文语料训练），对中文文档检索效果很差。
   这里显式指定中文向量模型，并保留降级路径。
2. **延迟加载 + 单例**：模型体积较大，首次使用时才加载，且全局只加载一次，
   避免每个请求都重新构造（原实现在每个 API 请求里 new 一个 RAGRetriever）。
3. **可降级**：模型下载失败（离线/网络受限）时返回 None，
   由调用方决定回退到 Chroma 默认 embedding，保证 RAG 链路不整体崩溃。

环境变量
--------
EMBEDDING_PROVIDER : local | dashscope   （默认 local）
EMBEDDING_MODEL    : 本地模型名 或 DashScope 模型名
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# 中文语义检索上表现稳定、体积适中（约 95MB）的模型
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"


class _SentenceTransformerWrapper:
    """
    对 sentence_transformers.SentenceTransformer 的最小封装。

    只需提供 embed_documents / embed_query 两个方法，
    与 LangChain Embeddings 接口保持一致。
    """

    def __init__(self, model_name: str, device: str = "cpu") -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device=device)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # normalize_embeddings=True：向量单位化，配合 cosine 距离更稳定
        vectors = self.model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class EmbeddingService:
    """封装文本向量化模型，进程内单例 + 双重检查锁。"""

    _instance: Optional["EmbeddingService"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "EmbeddingService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._model = None          # type: ignore[attr-defined]
                    obj._load_failed = False   # type: ignore[attr-defined]
                    cls._instance = obj
        return cls._instance

    # ------------------------------------------------------------------ #
    # 模型加载
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> str:
        return (getattr(settings, "EMBEDDING_PROVIDER", "local") or "local").lower()

    @property
    def model_name(self) -> str:
        configured = getattr(settings, "EMBEDDING_MODEL", None)
        if configured and not configured.startswith("text-embedding-ada-002"):
            return configured
        return DEFAULT_LOCAL_MODEL

    def get_model(self) -> Optional[Any]:
        """返回已加载的模型对象；加载失败返回 None（调用方需自行降级）。"""
        if self._model is not None or self._load_failed:
            return self._model
        with self._lock:
            if self._model is not None or self._load_failed:
                return self._model
            try:
                self._model = self._build_model()
                logger.info("Embedding 模型加载成功: provider=%s model=%s",
                            self.provider, self.model_name)
            except Exception as exc:  # noqa: BLE001 - 任意加载异常都要降级
                self._load_failed = True
                logger.warning(
                    "Embedding 模型加载失败，将降级为 Chroma 默认 embedding: %s", exc
                )
        return self._model

    def _build_model(self) -> Any:
        if self.provider == "dashscope":
            # 延迟导入：只有真的选了 dashscope 才需要该依赖
            from langchain_community.embeddings import DashScopeEmbeddings

            return DashScopeEmbeddings(
                model=self.model_name,
                dashscope_api_key=settings.QWEN_API_KEY,
            )

        # 本地模型：直接封装 sentence_transformers。
        # 不依赖 langchain_huggingface / langchain_community —— 这两个包
        # 在 langchain 1.x 中已不是默认安装项，缺失会导致导入即失败。
        return _SentenceTransformerWrapper(self.model_name)

    # ------------------------------------------------------------------ #
    # 对外的两个接口（Chroma 需要）
    # ------------------------------------------------------------------ #
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self.get_model()
        if model is None:
            raise RuntimeError("embedding 模型不可用")
        return model.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        model = self.get_model()
        if model is None:
            raise RuntimeError("embedding 模型不可用")
        return model.embed_query(text)

    # ------------------------------------------------------------------ #
    # 供 Chroma 使用的 embedding_function：签名需符合 EmbeddingFunction 协议
    # ------------------------------------------------------------------ #
    def as_chroma_function(self) -> Any:
        """
        构造传给 Chroma 的 embedding_function。

        必须同时实现 `__call__`（写入时使用）与 `embed_query`（查询时使用）：
        Chroma 在 collection.add() 走 __call__，而在 collection.query() 时会查找
        embed_query；只实现 __call__ 会导致「写入正常但检索永远失败」——
        这是一个很隐蔽的坑（本项目实测踩到过）。
        """
        if self.get_model() is None:
            return None

        service = self

        class _ChromaEmbeddingFunction:
            """适配 Chroma 的 embedding_function 协议。"""

            def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
                return service.embed_documents(list(input))

            def embed_documents(self, input: Any = None, **kwargs) -> list[list[float]]:  # noqa: A002
                texts = input if input is not None else kwargs.get("texts", [])
                return service.embed_documents(list(texts))

            def embed_query(self, input: Any = None, **kwargs) -> Any:  # noqa: A002
                # Chroma 内部以关键字 `input=` 调用本方法，且入参可能是
                # 单个字符串或字符串列表，两种形态都要兼容。
                text = input if input is not None else kwargs.get("text", "")
                if isinstance(text, (list, tuple)):
                    return service.embed_documents(list(text))
                return service.embed_query(text)

            @staticmethod
            def name() -> str:
                # Chroma 持久化时会记录该名字用于校验，保持稳定
                return "codepilot-embedding"

        return _ChromaEmbeddingFunction()

    def dimension_hint(self) -> str:
        """用于日志/文档说明当前模型，不触发模型加载之外的副作用。"""
        return f"{self.provider}:{self.model_name}"


embedding_service = EmbeddingService()
