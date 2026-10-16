# CodePilot 研发知识助手

基于 **LangGraph 多 Agent 编排 + RAG 检索增强生成** 的研发知识助手系统。

面向团队内部技术文档问答场景：上传文档 → 自动分块向量化 → 意图路由 → 检索重排 → 生成草稿 → **独立审查** → 交付 → Agent 轨迹可视化。

后端 FastAPI 异步架构，前端 React + TypeScript，支持 SSE 流式下发、真实 Function Calling，
以及 **Supervisor + Worker + Verifier 多 Agent 协作**（审查不通过会带反馈回传重新生成）。

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
│ Cross-Encoder │   │ 执行工具        │   │                │
│ 重排 Top-5    │   │ 回填 ToolMsg    │   │                │
│ 组装上下文     │   │                │   │                │
└───────┬───────┘   └───────┬────────┘   └───────┬────────┘
        └───────────────────┼────────────────────┘
                            ▼   生成草稿 + 证据
                   ┌──────────────────┐
                   │     verifier     │  独立审查：核对依据、是否答到点
                   └────────┬─────────┘
                            │
              ┌─────────────┴─────────────┐
              │ 不通过（≤3轮）              │ 通过
              ▼                           ▼
        ┌──────────┐              ┌──────────────┐
        │  revise  │              │   finalize   │
        └────┬─────┘              └──────┬───────┘
             │ 带反馈回送对应 Worker       │
             └──► worker               SSE 下发（text/event-stream）
                                             │
                                           __end__
```

**节点间通过共享 `AgentState`（TypedDict）传递**：会话历史、草稿、证据、审查意见、执行轨迹。
`agent_trace` 使用 **reducer（`Annotated[List[str], operator.add]`）** 按执行顺序累加——
LangGraph 默认是"后写覆盖先写"，不加 reducer 会只剩最后一行的轨迹。

**协作体现在回路上**：`worker → verifier → revise → worker` 是一条真实的反馈回路，
verifier 的判定与修改意见会回传给 worker 触发重新生成；连续 3 轮不通过则**诚实拒答**。

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

### 1. 多 Agent 协作：生成 + 独立审查（反馈回路）

本项目把一次问答拆成 **分类 → 生成 → 审查 → 交付** 四个阶段。
让「多 Agent 协作」成立的不是"有多个节点"，而是 **verifier 的判定会回传给 worker 触发重新生成**：

```
worker → verifier ─通过→ finalize → END
              │
              └─不通过→ revise →（带 feedback 回到原 worker，≤3 轮）
                                    │
                                    └─超限→ 诚实拒答，绝不交付编造内容
```

**为什么需要独立审查**：生成 Agent 有两类无法自我发现的问题——
① **无依据断言**：检索为空或工具没返回数据时，模型仍给出具体数字与文件名，
**不抛异常、日志正常**；② **答非所问**。
同一个模型在同一次生成里已经走完那条推理路径，无法可靠地审查自己。

**审查判定原则**：
- 草稿中每个具体事实（数字、文件名、配置项、API 名）都必须能在【参考资料】里找到出处
- 必须正面回答用户问题，多部分问题不能只答一半
- **资料确实不足时，"明确说明没有相关信息"判定为通过**——诚实拒答是合格答案

**最终保证**：连续 3 轮未通过时，`finalize` 输出**诚实拒答**，
而不是那份可能编造的草稿。验证脚本对此有专门断言（原草稿内容绝不能出现在输出里）。

> 实测：同一个问题「知识库里有哪些文档？」
> 改造前回答 **「知识库中包含 3 份文档」——编造的（实际 0 份）**；
> 改造后回答 **「知识库当前文档数量为 0 份」**，依据是 `get_knowledge_base_stats()` 的真实返回值。

**⚠️ 代价（主动说明）**：为保证审查能拦住**整段**内容，必须「完整生成 → 审查 → 再下发」，
因此**真正的逐 token 流式没有了**：首字延迟从 ~1.46s 增至 **~2.42s（+66%）**。
**全文审查与真流式在单次请求内不可兼得**，本项目的选择是**可靠性优先**。
若要两者兼得，可改为分段审查（生成一段→审查一段→通过即下发），代价是无法发现跨段落的不一致。

### 2. SSE 事件协议

| type | 载荷 | 说明 |
|---|---|---|
| `trace` | `content: []`、`tool_calls: []` | 执行轨迹、工具调用记录与**审查结论** |
| `token` | `content: "增量文本"` | 增量输出（审查通过后的定稿） |
| `conv_id` | `content: "<会话ID>"` | 会话标识 |
| `error` | `content: "错误信息"` | 异常 |
| `done` | — | 结束标记 |

前端用 `fetch` + `response.body.getReader()` 手动解析流——不用 `EventSource`，
因为它只支持 GET 且无法自定义请求头，而这里需要 POST 并携带 `Authorization`。

> 典型轨迹（真实运行输出）：
> ```
> Supervisor 意图分类 → tool
> Tool Worker 调用工具 get_knowledge_base_stats()
> Verifier 审查（第 1 轮）→ 通过：草稿给出的文档数量与参考资料中工具返回的统计数字完全一致
> Finalize 已通过审查，输出定稿
> ```

### 3. 真实 Function Calling

`tool_worker` 用 `llm.bind_tools(ALL_TOOLS)` 让模型返回结构化 `tool_calls`，
执行后把结果作为 `ToolMessage` 回填，再生成草稿（工具调用最多 3 轮）。

已实现的 4 个工具（全部操作真实数据，非占位）：

| 工具 | 作用 |
|---|---|
| `list_user_documents` | 列出知识库全部文档及分块数 |
| `get_knowledge_base_stats` | 文档数 / 分块数 / 会话数 / 问答次数统计 |
| `search_document_by_filename` | 按文件名关键词模糊查找 |
| `search_document_content` | 全文检索关键词并返回命中片段 |

运行期依赖（`AsyncSession`、`user_id`）通过 **`contextvars`** 传递而非模块级全局变量——
这一点很关键：并发的 SSE 请求各自持有独立 context，不会串用户数据。

### 4. 两阶段检索：向量召回 + Cross-Encoder 重排

| 阶段 | 原理 | 特点 |
|---|---|---|
| 向量召回 | 双塔（bi-encoder），query 与 doc 独立编码后算余弦相似度 | 可离线预建索引，快；但无法建模词级交互，精度有上限 |
| Cross-Encoder 重排 | `(query, document)` 拼接后过同一模型 | 精度显著更高；但必须实时前向推理，无法预建索引 |

因此先召回 Top-10 候选，再重排保留 Top-5 作为生成上下文。
重排分数写入 `metadata["rerank_score"]`，便于在轨迹与日志中观测。

**降级**：Cross-Encoder 模型不可用时按向量检索原顺序截断返回，链路不中断。

### 5. 用户级知识库隔离

向量库 `metadata` 写入 `user_id` / `doc_id` / `chunk_index`，
查询时用 `where={"user_id": ...}` 过滤；数据库层同样带 `user_id` 条件。
删除文档时**按 `doc_id` 元数据过滤批量删除**（不依赖 `chunk_count` 是否准确）。

> 说明：这是**应用层软隔离**。若需强隔离，应使用独立 collection 或数据库行级权限。

### 6. 缓存与故障降级

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
│   │   ├── agents/
│   │   │   ├── supervisor.py        # LangGraph 编排（Supervisor+Worker+Verifier 回路）
│   │   │   └── verifier.py          # 审查 Agent：核对依据、检测无依据断言
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
├── verify_project.py                # 结构与逻辑自检（45 项，不依赖 LLM）
├── verify_llm_flow.py               # 真实 LLM 路径验证（11 项，需 API Key）
├── measure_verifier_cost.py         # 审查环节延迟开销实测
├── docs/adr/                        # 架构决策记录（含取舍与备选方案）
├── Dockerfile                       # 多阶段构建
└── docker-compose.yml               # backend + postgres + redis (+ frontend)
```

---

## 工程实践与踩坑记录

本项目在完善过程中解决了一批真实缺陷，其中几个很有代表性：

| 问题 | 现象 | 修复 |
|---|---|---|
| **LangGraph 状态字段被覆盖** | `agent_trace` 只剩最后一行，前面所有节点的轨迹被冲掉，"可观测性"形同虚设 | 用 reducer（`Annotated[List[str], operator.add]`）让轨迹按执行顺序累加 |
| **生成 Agent 会编造** | 检索为空/工具无数据时仍给出具体数字与文件名，且不抛异常、日志正常 | 增加独立审查 Agent；连续 3 轮不通过则诚实拒答，绝不交付编造内容 |
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

# 结构与逻辑自检（38 项，不依赖 LLM / 外部密钥）
python verify_project.py

# 真实 LLM 路径验证（意图分类 / 审查判定 / 协作回路）
python verify_llm_flow.py
```

测试覆盖：鉴权保护、会话 CRUD、文档上传与向量写入、知识库统计、
路径穿越防护、分块边界、缓存降级、工具 schema 与错误处理、JWT 篡改拒绝，
以及**审查结果解析容错、协作回路图结构、轮次上限、审查不通过时不得交付原草稿**等关键断言。

### 协作回路的确定性验证

`verify_project.py` 第 9 节用**注入式装置**（`backend/tests/loop_harness.py`）
把 LLM 调用替换为可控桩函数，让「第几轮通过 / 一直不通过」成为可复现的输入，
而真实的图结构、路由函数与 finalize 逻辑全部保留。因此以下行为**每次都能被验证**：

| 场景 | 断言 |
|---|---|
| 首轮通过 | 生成 1 次、审查 1 次，不触发多余重做 |
| 第 2 轮通过 | Worker **确实被再次调用**（回路的边走通了） |
| 每轮重做 | 携带上一轮的审查意见（反馈真的回传） |
| 永不通过 | **恰好 3 轮后停止**，不会无限循环 |
| 达上限 | 输出诚实拒答，且**原草稿内容绝不出现在输出里** |
| 全流程 | `agent_trace` 按执行顺序累加（reducer 生效） |

> ⚠️ 实现提示：替换图节点行为必须**先替换类方法、再创建 agent**，
> 并在**整次图执行期间**保持替换。因为图在编译期持有节点方法的引用，
> 而 `_generate_draft` 又是 Worker 运行期通过 `self.` 查找的——
> 提前还原会让 Worker 调回真实 LLM。详见 `docs/adr/001-verifier-agent-tradeoff.md`。

---

## 已知限制与后续方向

- **首字延迟**：为保证全文审查，采用「完整生成 → 审查 → 交付」，首字延迟增加约 66%；
  后续可改为**分段审查**（生成一段 → 审查一段 → 通过即下发），代价是无法发现跨段落的不一致
- **混合意图**：当前为单跳路由，Supervisor 一次只能选一条分支，
  「列出文档并总结第一条」这类混合需求只能答一半；
  可升级为 **worker 工具化**（把 worker 注册成 Supervisor 可调用的工具，在循环里多轮调用）
  或**并行 Worker + 合并**
- **审查非绝对可靠**：Verifier 也是一次 LLM 判定，同样可能误判。
  它能拦住"无依据断言"这类高发错误并让判定可观测，但不是 100% 保证
- **文档格式**：目前仅支持 UTF-8 文本类文件，PDF / Word 需接入解析库
- **语义缓存**：当前为精确匹配缓存，可演进为基于 embedding 相似度的语义缓存
- **检索评测**：缺少 Hit@K / MRR 等量化指标，可自建问答测试集补齐
- **意图分类成本**：当前每次请求都调用 LLM 分类，高 QPS 下可换小模型或加分类缓存
- **Reranker 并发**：Cross-Encoder 推理已放入线程池，进一步可做批处理或独立推理服务

---

## License

仅供学习与技术交流使用。
