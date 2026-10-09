"""
应用配置。所有配置项均可通过环境变量或 .env 覆盖。
"""

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings

# .env 必须相对「本文件」定位，而不是相对当前工作目录。
# 原实现只写 env_file=".env"，从项目根目录启动时（uvicorn app.core.app:app）
# 工作目录不是 backend/，于是读不到 .env → API Key / SECRET_KEY 全部取默认值。
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    # ---------- 应用 ----------
    APP_NAME: str = "CodePilot"
    APP_VERSION: str = "1.1.0"
    DEBUG: bool = False

    # ---------- 数据库 ----------
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/codepilot"

    # ---------- Redis ----------
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL: int = 300  # 问答缓存有效期（秒）

    # ---------- LLM ----------
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    QWEN_API_KEY: Optional[str] = None
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # ---------- 向量库 ----------
    CHROMA_COLLECTION: str = "codepilot_knowledge"
    # Chroma 使用嵌入式 PersistentClient，数据落本地目录，无需 HOST/PORT

    # ---------- Embedding ----------
    # provider: local（sentence-transformers 本地模型）| dashscope（阿里云）
    # 必须显式指定中文模型：Chroma 默认的 all-MiniLM-L6-v2 是英文语料训练的，
    # 对中文文档检索效果很差。
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"

    # ---------- RAG 分块与检索 ----------
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    RETRIEVE_TOP_K: int = 10   # 向量召回候选数
    RERANK_TOP_K: int = 5      # Cross-Encoder 重排后保留数

    # ---------- JWT ----------
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # ---------- 限流 ----------
    RATE_LIMIT_PER_MINUTE: int = 60
    CHAT_RATE_LIMIT: str = "20/minute"

    # ---------- HuggingFace 镜像（国内下载模型用） ----------
    HF_ENDPOINT: str = "https://hf-mirror.com"

    class Config:
        env_file = str(ENV_FILE)
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


settings = Settings()

# sentence-transformers / huggingface_hub 在导入时即读取该变量，
# 必须在模型加载前设置好（原实现在 import 之后设置，实际不生效）。
os.environ.setdefault("HF_ENDPOINT", settings.HF_ENDPOINT)

# HuggingFace 缓存目录：默认 /root/.cache 或 C:\Users\<user>\.cache 在部分环境不可写，
# 会导致模型加载失败并静默降级为 Chroma 默认的英文 embedding。
# 这里在缓存目录不可写时自动切换到项目内的可写目录。
def _ensure_hf_cache_dir() -> Optional[str]:
    """
    确保 HuggingFace 缓存目录可写。

    默认缓存目录（如 C:\\Users\\<user>\\.cache\\huggingface\\hub 或 /root/.cache/...）
    在受限环境中可能不可写，会导致中文向量模型加载失败 → 静默降级为
    Chroma 默认的英文 embedding，检索质量骤降却几乎没有明显报错。

    策略：优先尊重用户显式设置的 HF_HOME；否则**直接使用项目内的可写目录**。
    刻意不去探测系统默认目录是否可写——"父目录可写"不代表"要写入的子目录可写"，
    探测容易误判（这个坑本项目已经踩过一次）。
    """
    if os.environ.get("HF_HOME"):
        return os.environ["HF_HOME"]

    fallback = BACKEND_DIR / ".hf_cache"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(fallback)
        # Windows 无开发者模式时无法用符号链接缓存，huggingface_hub 会刷警告
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        return str(fallback)
    except Exception:  # noqa: BLE001 - 创建失败则退回系统默认行为
        return None


try:
    _hf_fallback = _ensure_hf_cache_dir()
except Exception:  # noqa: BLE001 - 缓存探测失败不应阻断启动
    _hf_fallback = None
