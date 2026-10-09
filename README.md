# CodePilot 研发知识助手

基于 **LangGraph 多 Agent 编排 + RAG 检索增强生成** 的研发知识助手系统。

面向团队内部技术文档问答场景：上传文档 → 自动分块向量化 → 意图路由 → 检索重排 → 流式生成 → Agent 轨迹可视化。

后端 FastAPI 异步架构，前端 React + TypeScript，支持 SSE 逐 token 流式输出与真实 Function Calling。

---

## 架构设计

```
                          用户消息
                             │
                             ▼
                  ┌──────────────────────┐
                  │      supervisor      │  LLM 意图分类（max_tokens=16）
                  │   rag / tool / direct│
                  └──────────┬───────────┘
                             │  add_conditional_edges
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
┌───────────────┐   ┌────────────────┐   ┌────────────────┐
│  rag_worker   │   │  tool_worker   │   │   direct_llm   │
├───────────────┤   ├────────────────┤   ├────────────────┤
│ 向量召回 Top-10│   │ bind_tools     │   │ 通用对话        │
│ Cross-Encoder │   │  执行工具       │   │                │
│ 重排 Top-5    │   │  回填 ToolMsg   │   │                │
│ 组装上下文     │   │  再流式生成      │   │                │
└───────┬───────┘   └───────┬────────┘   └───────┬────────┘
        └───────────────────┴────────────────────┘
                            ▼
                     __end__ → token 生成器
                            ▼
                 SSE 逐 token 下发（text/event-stream）
```

**节点间通过共享 `AgentState`（TypedDict）传递**会话历史、执行轨迹与 token 生成器；
每个 Worker 独立收敛后用 `add_edge(..., "__end__")` 结束，不做多跳串联。

---

## 技术栈

### 后端
| 类别 | 选型 |
|---|---|
| API 框架 | FastAPI + Pydantic v2 |
| Agent 编排 | LangGraph `StateGraph`（Supervisor-Worker） |
| LLM | DeepSeek API（OpenAI 兼容接口，`langchain-openai`） |
| RAG | Chroma（嵌入式持久化）+ Cross-Encoder 重排 |
| Embedding | `BAAI/bge-small-zh-v1.5`（中文优化，512 维） |
| 数据库 | PostgreSQL + SQLAlchemy 2.0 异步（asyncpg） |
| 缓存 | Redis（不可用时自动降级为进程内内存缓存） |
| 鉴权 | JWT（python-jose HS256）+ bcrypt 密码哈希 |
| 限流 | slowapi（可选依赖，缺失时自动禁用） |
| 测试 | pytest + pytest-asyncio |

### 前端
React 18 + TypeScript · Vite 5 · React Router v6 · axios · react-markdown

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env，至少填入 DEEPSEEK_API_KEY 和 SECRET_KEY
docker compose up -d --build
```

访问 http://localhost:8080 。镜像为多阶段构建，已内置前端产物与预下载的向量模型。

启动前端开发服务器（热更新，可选）：

```bash
docker compose --profile dev up -d
# 访问 http://localhost:5173
```

### 方式二：本地开发

```bash
# 后端
cd backend
pip install -r requirements.txt
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
uvicorn app.core.app:app --reload --port 8080

# 前端（另开终端）
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

> 没有 PostgreSQL 时可用 SQLite 快速跑通：把 `.env` 里的 `DATABASE_URL` 改成
> `sqlite+aiosqlite:///./codepilot.db`。

### 端到端自检

```bash
python verify_project.py
```

不依赖 pytest，直接校验模块导入、分块、缓存降级、工具 schema、
向量写入/检索/删除闭环，以及真实 PostgreSQL 下的完整 API 链路。

---

## 核心实现要点

### 1. 真实流式输出（SSE，逐 token 透传）

RAG 分支的节点内部调用 LLM 的 `astream()`，把**异步生成器作为节点出参**返回；
节点本身仍立即返回，由路由层从最终 state 取出生成器并逐 token 透传给 SSE。

这样首字延迟（TTFT）只等于 LLM 产出首个 token 的时间，而不是整段生成时间。

> 反面做法：先 `await llm.ainvoke()` 拿到完整字符串，再逐字符 yield。
> 前端看起来也是"打字机效果"，但 TTFT 没有任何改善——这是常见的伪流式实现。

事件协议（每帧 `data: {json}\n\n`）：

| type | 载荷 | 说明 |
|---|---|---|
| `trace` | `content: []`、`tool_calls: []` | 执行轨迹与工具调用记录 |
| `token` | `content: "增量文本"` | 逐 token 输出 |
| `conv_id` | `content: "<会话ID>"` | 会话标识 |
| `error` | `content: "错误信息"` | 异常 |
| `done` | — | 结束标记 |

前端用 `fetch` + `response.body.getReader()` 手动解析流——不用 `EventSource`，
因为它只支持 GET 且无法自定义请求头，而这里需要 POST 并携带 `Authorization`。

### 2. 真实 Function Calling

`tool_worker` 用 `llm.bind_tools(ALL_TOOLS)` 让模型返回结构化 `tool_calls`，
执行后把结果作为 `ToolMessage` 回填，再流式生成最终回答（最多 3 轮）。

已实现的 4 个工具（全部操作真实数据，非占位）：

| 工具 | 作用 |
|---|---|
| `list_user_documents` | 列出知识库全部文档及分块数 |
| `get_knowledge_base_stats` | 文档数 / 分块数 / 会话数 / 问答次数统计 |
| `search_document_by_filename` | 按文件名关键词模糊查找 |
| `search_document_content` | 全文检索关键词并返回命中片段 |

运行期依赖（`AsyncSession`、`user_id`）通过 **`contextvars`** 传递而非模块级全局变量——
这一点很关键：并发的 SSE 请求各自持有独立 context，不会串用户数据。

### 3. 两阶段检索：向量召回 + Cross-Encoder 重排

| 阶段 | 原理 | 特点 |
|---|---|---|
| 向量召回 | 双塔（bi-encoder），query 与 doc 独立编码后算余弦相似度 | 可离线预建索引，快；但无法建模词级交互，精度有上限 |
| Cross-Encoder 重排 | `(query, document)` 拼接后过同一模型 | 精度显著更高；但必须实时前向推理，无法预建索引 |

因此先召回 Top-10 候选，再重排保留 Top-5 作为生成上下文。
重排分数写入 `metadata["rerank_score"]`，便于在轨迹与日志中观测。

**降级**：Cross-Encoder 模型不可用时按向量检索原顺序截断返回，链路不中断。

### 4. 用户级知识库隔离

向量库 `metadata` 写入 `user_id` / `doc_id` / `chunk_index`，
查询时用 `where={"user_id": ...}` 过滤；数据库层同样带 `user_id` 条件。
删除文档时**按 `doc_id` 元数据过滤批量删除**（不依赖 `chunk_count` 是否准确）。

> 说明：这是**应用层软隔离**。若需强隔离，应使用独立 collection 或数据库行级权限。

### 5. 缓存与故障降级

- 键为 `{user_id}:{message}` 的 SHA-256 摘要——**包含 user_id，避免不同用户命中彼此答案**
- Redis `setex` 带 TTL（默认 300s）
- Redis 连接异常时自动降级到进程内 `_memory_cache`，**缓存组件故障不阻断主链路**

> 注意语义边界：这是**精确匹配缓存**，不是语义缓存。
> 措辞不同（"如何配置 Redis" vs "怎么配置 Redis"）不会命中。
> 真正的语义缓存需要把 query 向量化后做相似度检索并设阈值——属后续演进方向。

---

## 数据库设计

```
users                          conversations
├── id            PK (uuid)    ├── id          PK (uuid)
├── username      UNIQUE, IDX  ├── user_id     IDX  ──┐
├── password_hash (bcrypt)     ├── title            │
├── avatar                     ├── created_at       │
└── created_at                 └── updated_at       │
                                                    │
documents                        chat_history       │
├── id            PK (uuid)      ├── id            PK (uuid)
├── user_id       IDX  ──────────├── user_id       IDX
├── filename                     ├── conversation_id IDX ──┘
├── content       TEXT           ├── message       TEXT
├── chunk_count                  ├── response      TEXT
└── created_at                   └── created_at
```

向量数据存于 Chroma（与关系库分离），通过 `metadata.doc_id` / `metadata.user_id` 关联。

---

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/register` | 注册 |
| POST | `/api/v1/auth/login` | 登录 |
| GET | `/api/v1/auth/me` | 当前用户 |
| POST | `/api/v1/chat` | 同步聊天（带缓存） |
| POST | `/api/v1/chat/stream` | **SSE 流式聊天** |
| GET | `/api/v1/conversations` | 会话列表 |
| POST | `/api/v1/conversations` | 新建会话 |
| DELETE | `/api/v1/conversations/{id}` | 删除会话 |
| GET | `/api/v1/conversations/{id}/history` | 会话历史 |
| POST | `/api/v1/documents` | 上传文档（自动分块向量化） |
| GET | `/api/v1/documents` | 文档列表 |
| DELETE | `/api/v1/documents/{id}` | 删除文档（同步清理向量） |
| GET | `/api/v1/documents/stats` | 知识库统计（含向量库一致性检查） |
| GET/PUT | `/api/v1/profile` | 个人信息 |
| POST | `/api/v1/upload-avatar` | 上传头像 |
| GET | `/health` | 健康检查（含向量库 / embedding / LLM 状态） |

接口文档：http://localhost:8080/docs

---

## 项目结构

```
CodePilot/
├── backend/
│   ├── app/
│   │   ├── agents/supervisor.py     # LangGraph 编排 + 流式生成 + Function Calling
│   │   ├── api/{auth,routes}.py     # 路由层
│   │   ├── core/{app,config,auth,database,logging}.py
│   │   ├── models/                  # SQLAlchemy 模型（4 张表）
│   │   ├── rag/
│   │   │   ├── embeddings.py        # 中文 embedding 服务（单例 + 降级）
│   │   │   ├── processor.py         # 零依赖递归分块
│   │   │   ├── retriever.py         # Chroma 访问层（单例）
│   │   │   ├── reranker.py          # Cross-Encoder 重排
│   │   │   └── tools.py             # Function Calling 工具集
│   │   └── services/cache.py        # Redis 缓存 + 内存降级
│   ├── tests/                       # pytest 用例
│   └── requirements.txt
├── frontend/src/
│   ├── components/                  # ChatMessage / ChatInput / AgentTrace
│   ├── pages/                       # Chat / KnowledgeBase / History / PersonalCenter / Login
│   └── services/api.ts              # axios + SSE 流解析
├── verify_project.py                # 端到端自检脚本
├── Dockerfile                       # 多阶段构建
└── docker-compose.yml               # backend + postgres + redis (+ frontend)
```

---

## 工程实践与踩坑记录

本项目在完善过程中解决了一批真实缺陷，其中几个很有代表性：

| 问题 | 现象 | 修复 |
|---|---|---|
| `ChatOpenAI` 参数名 | 新版 langchain-openai 移除了 `openai_api_key` 别名，构造时直接抛 `OpenAIError` | 改用 `api_key` / `base_url` |
| `.env` 读取路径 | `env_file=".env"` 相对**工作目录**解析，从项目根启动读不到配置，API Key 全部取默认值 | 改为相对 config.py 所在目录定位 |
| 分块依赖缺失 | `langchain_text_splitters` 在 langchain 1.x 已非默认安装，缺失导致整个 RAG 层 import 失败 | 自实现零依赖递归分块 |
| 清洗破坏分隔符 | `" ".join(text.split())` 把换行也压成空格，`\n\n` / `\n` 分隔符彻底失效 | 只压缩空白，保留换行结构 |
| Chroma 维度不匹配 | 索引 384 维（默认英文模型）、当前配置 512 维（中文模型），读写全部报错 | 检测不一致则重建 collection，并显式重挂 embedding function |
| **Chroma 不恢复 embedding function** | 重新打开已存在的 collection 时回落到 `DefaultEmbeddingFunction`（384 维）→ 首次运行正常、重启后必失败 | 打开后显式 `collection._embedding_function = ef` |
| Chroma 查询调用约定 | 自定义 embedding function 只实现 `__call__` 时，写入正常但 `query` 恒定失败 | 同时实现 `embed_query(input=...)` |
| slowapi 硬依赖 | 缺包时 `app.api.routes` 导入失败 → 服务起不来 | 改为可选依赖 + 空装饰器降级 |
| HF 缓存目录不可写 | 中文模型加载失败并**静默降级**为英文模型，检索质量骤降却无明显报错 | 探测失败即切换到项目内可写目录 |
| 路径穿越 | 头像接口未校验路径，可读取目录外文件 | 落地 `resolve()` 前缀校验 |
| 缓存跨用户串号 | 缓存键仅含 message，不同用户会命中彼此答案 | 键中加入 `user_id` |
| Redis/Celery 悬空配置 | requirements 装了 celery 但代码从未使用 | 移除依赖与配置 |

---

## 测试

```bash
cd backend
python -m pytest tests/ -v

# 或使用不依赖 pytest 的端到端自检
python verify_project.py
```

测试覆盖：鉴权保护、会话 CRUD、文档上传与向量写入、知识库统计、
路径穿越防护、分块边界、缓存降级、工具 schema 与错误处理、JWT 篡改拒绝。

---

## 已知限制与后续方向

- **文档格式**：目前仅支持 UTF-8 文本类文件，PDF / Word 需接入解析库
- **语义缓存**：当前为精确匹配缓存，可演进为基于 embedding 相似度的语义缓存
- **评测体系**：缺少检索质量量化指标，可自建问答测试集统计 Hit@K / MRR
- **意图分类**：当前每次请求都调用 LLM 分类，高 QPS 下可换小模型或加分类缓存
- **多跳编排**：目前为单跳路由，复杂任务可扩展为多 Worker 串行/并行
- **Reranker 并发**：Cross-Encoder 推理已放入线程池，进一步可做批处理或独立推理服务

---

## License

仅供学习与技术交流使用。
