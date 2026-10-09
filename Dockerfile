# ---------- 阶段 1：构建前端静态资源 ----------
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

# 先只拷贝依赖清单，利用 Docker 层缓存：源码变动时不必重装依赖
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# tsc + vite build → 产出 /frontend/dist
RUN npm run build


# ---------- 阶段 2：运行时镜像 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Chroma(HNSW) 与 sentence-transformers 需要编译工具链；
# libgomp1 是 torch / onnxruntime 的运行时依赖，缺少会导致导入即报错
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 预下载中文向量模型与 Cross-Encoder，避免容器首次请求时才下载
# （首次下载既慢又可能因网络失败导致 RAG 静默降级）
ARG PRELOAD_MODELS=true
RUN if [ "$PRELOAD_MODELS" = "true" ]; then \
        python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('BAAI/bge-small-zh-v1.5'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2'); \
print('models preloaded')"; \
    fi

COPY backend/ ./

# 前端构建产物放到 /frontend/dist，与 app/core/app.py 的候选路径一致
COPY --from=frontend-builder /frontend/dist /frontend/dist

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.core.app:app", "--host", "0.0.0.0", "--port", "8080"]
