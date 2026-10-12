# -*- coding: utf-8 -*-
"""
《CodePilot 项目详解》内容定义 —— 第一部分。

块格式约定：
  ("h1", 标题, 序号或None)   ("h2", 标题)   ("h3", 标题)
  ("p", 正文)                ("ul", [...])  ("ol", [...])
  ("code", 代码, 说明)        ("table", 表头, 行, 列宽)
  ("note", 文本, tip|warn|danger|info)
  ("qa", 问题, [要点...], [追问...])
  ("pagebreak",)
正文支持 **加粗** 与 `行内代码`。
"""

DOC_TITLE = "CodePilot 研发知识助手"
DOC_SUBTITLE = "项目技术详解 · 架构设计 · 核心代码 · 面试准备"
DOC_VERSION = "版本 1.1.0　·　2026 年 10 月"

BLOCKS_PART1 = [

# ==================================================================== #
("h1", "项目概览", None),
# ==================================================================== #

("h2", "1.1 一句话介绍"),
("p", "CodePilot 是一个面向团队内部技术文档问答场景的**研发知识助手**。"
      "它把「文档上传 → 自动分块向量化 → 意图路由 → 检索重排 → 流式生成 → 轨迹可视化」"
      "整条链路做成了完整可用的系统：后端 FastAPI 异步架构，"
      "用 **LangGraph 的 StateGraph** 实现 Supervisor-Worker 多 Agent 编排，"
      "前面挂一条 **RAG 检索增强**管线（向量召回 + Cross-Encoder 重排），"
      "数据层是 PostgreSQL + Redis（含故障降级），前端 React + TypeScript 通过 SSE 逐 token 接收回答。"),

("h2", "1.2 为什么要做这个项目"),
("p", "可以直接回答面试官的问题，核心是「解决什么真实问题」："),
("ul", [
    "**团队知识散落**：技术文档、API 规范、代码规范分散在 Confluence、语雀、Git 仓库里，找一份资料要翻好几个地方",
    "**通用大模型不懂内部上下文**：直接问 DeepSeek/ChatGPT 得不到内部系统的准确答案，还会一本正经地编",
    "**纯 RAG 不够用**：只有检索能力时，用户问「知识库里有几份文档」这类问题无法回答，需要工具调用",
    "**等待体验差**：大模型生成整段回答要几秒，用户盯着空白页面没有反馈，需要流式输出",
]),
("note", "面试时**不要**说「为了学 LangGraph 所以做了这个项目」。"
         "要把它讲成一个有真实痛点、有明确用户、有技术取舍的系统。", "warn"),

("h2", "1.3 技术栈总览"),
("table",
 ["层次", "技术选型", "选它的原因"],
 [
  ["Web 框架", "FastAPI + Pydantic v2", "原生异步、自动生成 OpenAPI 文档、依赖注入做鉴权很干净"],
  ["Agent 编排", "LangGraph StateGraph", "以状态图方式管理节点流转，条件路由比手写 if/else 清晰且可扩展"],
  ["大模型", "DeepSeek API（OpenAI 兼容）", "兼容 OpenAI SDK，成本低，中文能力强"],
  ["向量库", "Chroma（嵌入式持久化）", "无需独立部署、本地持久化，适合中小规模知识库"],
  ["Embedding", "BAAI/bge-small-zh-v1.5", "中文语料训练，512 维，体积小（约 95MB），中文检索效果明显优于英文模型"],
  ["重排模型", "cross-encoder/ms-marco-MiniLM-L-6-v2", "Cross-Encoder 架构，对 (query, passage) 成对打分，精度高于向量召回"],
  ["关系库", "PostgreSQL + SQLAlchemy 2.0 异步", "asyncpg 驱动真正异步，IO 密集场景下吞吐更好"],
  ["缓存", "Redis（可降级内存）", "缓存问答结果降低 LLM 调用成本，Redis 故障时不阻断主链路"],
  ["鉴权", "JWT（HS256）+ bcrypt", "无状态鉴权便于水平扩展，bcrypt 加盐哈希不存明文密码"],
  ["限流", "slowapi", "按 IP 限制聊天接口频率，防止 API Key 被刷"],
  ["前端", "React 18 + TypeScript + Vite", "类型安全、构建快，fetch + ReadableStream 手动解析 SSE"],
 ],
 [3.0, 5.2, 8.4]),

("h2", "1.4 功能清单"),
("table",
 ["功能模块", "已实现的能力"],
 [
  ["多 Agent 编排", "Supervisor 用 LLM 做意图分类（rag / tool / direct），条件路由到三个 Worker，共享状态传递会话历史与执行轨迹"],
  ["RAG 检索", "递归分块（512/50）→ Chroma 向量召回 Top-10 → Cross-Encoder 重排 Top-5 → 组装上下文，向量库按 user_id 隔离"],
  ["Function Calling", "4 个真实工具：文档列表、知识库统计、按文件名查找、全文关键词检索；结果以 ToolMessage 回填后继续生成"],
  ["流式输出", "SSE 逐 token 透传，自定义 5 类事件帧（token / trace / conv_id / error / done），首字延迟与整段生成时间解耦"],
  ["知识库管理", "文档上传 → 解码 → 分块 → 向量化 → 落库；删除时同步按 doc_id 批量清理向量"],
  ["会话管理", "多会话创建 / 列表 / 历史回放 / 删除，会话标题自动取首条消息前 50 字"],
  ["用户体系", "注册、登录、JWT 鉴权、bcrypt 密码哈希、头像上传、个人信息维护"],
  ["缓存降级", "SHA-256 摘要做键 + TTL 缓存；Redis 异常自动降级进程内内存缓存"],
  ["限流保护", "slowapi 按 IP 限制聊天接口 20 次/分钟（可选依赖，缺失自动禁用）"],
  ["可观测性", "统一请求日志中间件（方法 / 路径 / 状态码 / 耗时）、全局异常处理、健康检查暴露依赖状态"],
  ["容器化部署", "多阶段 Dockerfile + Docker Compose 编排 backend / PostgreSQL / Redis，含健康检查与数据卷持久化"],
 ],
 [3.4, 13.2]),

("pagebreak",),

# ==================================================================== #
("h1", "系统架构设计", None),
# ==================================================================== #

("h2", "2.1 整体架构图"),
("code", """
                             用户浏览器 (React + TypeScript)
                                        │
                    ┌───────────────────┴───────────────────┐
                    │   axios（REST）      fetch + ReadableStream（SSE）
                    ▼                                       ▼
        ┌───────────────────────────────────────────────────────────┐
        │                      FastAPI 应用层                        │
        │  请求日志中间件 → CORS → slowapi 限流 → JWT 鉴权依赖        │
        │  路由：/auth  /chat  /chat/stream  /conversations         │
        │        /documents  /profile  /health                      │
        └───────────────────────────┬───────────────────────────────┘
                                    ▼
        ┌───────────────────────────────────────────────────────────┐
        │           LangGraph StateGraph（Supervisor-Worker）        │
        │                                                           │
        │   ┌──────────────┐   LLM 意图分类（max_tokens=16）          │
        │   │  supervisor  │   → rag / tool / direct                 │
        │   └──────┬───────┘                                        │
        │          │ add_conditional_edges                          │
        │   ┌──────┴───────┬────────────────┐                        │
        │   ▼              ▼                ▼                        │
        │ ┌──────────┐ ┌───────────┐ ┌────────────┐                 │
        │ │rag_worker│ │tool_worker│ │ direct_llm │                 │
        │ └────┬─────┘ └─────┬─────┘ └─────┬──────┘                 │
        │      │             │             │                        │
        │      │  共享 AgentState（TypedDict）传递：                 │
        │      │  messages / history / user_id /                    │
        │      │  next_worker / token_stream / agent_trace          │
        │      └─────────────┴─────────────┘                        │
        │                    ▼                                      │
        │              __end__ → token 异步生成器                    │
        └────────────────────┬──────────────────────────────────────┘
                             ▼
        ┌───────────────────────────────────────────────────────────┐
        │            数据与模型层                                    │
        │  Chroma 向量库   │  PostgreSQL   │  Redis 缓存             │
        │  bge-small-zh    │  SQLAlchemy   │  降级到内存缓存          │
        │  Cross-Encoder   │  4 张表       │                        │
        │  DeepSeek API（LLM）                                      │
        └───────────────────────────────────────────────────────────┘
""", "图 2-1　CodePilot 整体架构"),

("h2", "2.2 为什么用 Supervisor-Worker 而不是单 Agent"),
("p", "这是面试必问的问题，回答要落到**具体的技术代价**上，而不是「架构更清晰」这种空话。"
      "下面按「现象 → 机制 → 修法」来讲，每一层都要能说清楚。"),
("h3", "单 Agent 的做法"),
("p", "如果只有一个 Agent，它需要同时具备三种能力：查知识库、调工具、日常对话。"
      "做法是把**所有工具描述 + 所有行为指令**塞进同一个 system prompt。"
      "本项目里正好存在两套会互相冲突的指令："),
("table",
 ["指令来源", "内容", "内在倾向"],
 [
  ["系统提示词（生成规则）", "1. 绝对不要说「根据知识库」「参考文档」；2. 绝对不要解释你是如何获取信息的；3. 直接给出答案",
   "**隐藏**信息来源，不要暴露检索/查询过程"],
  ["工具定义（docstring）", "`list_user_documents`：当用户询问「我上传了哪些文档」时使用……",
   "**暴露**查询动作，明确说明数据来自哪里"],
 ],
 [4.0, 8.2, 5.0]),
("p", "这两套指令在单 Agent 里**同时生效**。模型面对「知识库里有几份文档」这个问题时，"
      "必须在同一轮生成里同时完成两件事："),
("ol", [
    "判断「这是检索问题还是工具问题」——**决策**",
    "遵守「不要提来源」——**输出格式约束**",
]),
("note", "**「意图混淆」的正确定义**：决策被输出格式约束污染。"
         "规则 1 在暗示「别暴露你是怎么拿到的」，而工具调用天然要暴露「我查了数据库」，"
         "模型在这两股力量之间摇摆，行为变得不可预测。", "info"),

("h3", "失败场景 A：该调工具，却硬去检索并编造结果"),
("p", "**用户问**：「知识库里有几份文档？」"),
("p", "**单 Agent 的典型坏输出**：「知识库中包含 **3** 份文档，分别是……」"),
("p", "看起来是对的，但**它是编的**——模型没有调用 `list_user_documents`，"
      "而是把附近文档的片段拼凑出一个数字。因为 prompt 里既有工具描述、又有「别解释来源」，"
      "工具描述被淹没，模型倾向走它更熟悉的「检索 + 归纳」路径。"),
("note", "这是意图混淆**最危险的形态：不是报错，而是自信地胡说**。"
         "系统日志里看不到任何异常，用户也不会察觉，只有比对数据库才能发现数字是错的。", "danger"),

("h3", "失败场景 B：根本不该检索，却强行检索"),
("p", "**用户问**：「用 Python 写一个快速排序」"),
("p", "prompt 里有「优先基于参考资料回答」这条规则。于是模型去检索知识库，"
      "召回几个凑巧相关的片段，然后把它们硬塞进答案——输出里混进了与问题无关的"
      "「内部文档风格」内容，**反而比不检索更差**。"),
("p", "在 Supervisor 架构下这个问题不存在，因为 `direct` 分支**根本不加载检索上下文**："),
("code", """
async def _direct_llm(self, state: RAGState) -> Dict[str, Any]:
    # context 传空字符串 —— 压根没有检索这一步
    stream = self._stream_llm(state.get("messages", ""), state.get("history", ""), "")
    return {"token_stream": stream, "agent_trace": ["Direct LLM 通用对话模式"]}
""", "核心代码 2-1　direct 分支不注入任何上下文"),

("h3", "为什么会这样：三个叠加的机制"),
("table",
 ["机制", "说明"],
 [
  ["**没有优先级，全是并列规则**", "「必须基于文档回答」和「不要提来源」是两句平行指令，冲突时模型只能自己猜谁优先"],
  ["**注意力被稀释**", "prompt 越长，每条指令分到的注意力权重越低，靠后的规则经常被忽略"
                     "（这是 LLM 的「Lost in the Middle」现象）"],
  ["**决策与执行耦合在同一轮生成**", "模型在同一轮里既要选动作、又要遵守格式、又要生成内容，三个目标互相拉扯"],
 ],
 [4.6, 12.0]),

("h3", "Supervisor 的修法：把决策和执行拆到两次独立的生成里"),
("p", "核心思路是让每个 prompt **只装一套指令**："),
("table",
 ["节点", "prompt 里装了什么", "为什么不会混淆"],
 [
  ["supervisor", "对话历史（截断 200 字符）+ 用户问题 + 「只回复一个词：rag 或 tool 或 direct」",
   "**没有任何输出格式约束**，模型只需做判断，不必同时想着「别提来源」"],
  ["rag_worker", "系统规则（别提来源）+ 检索到的上下文",
   "已经确定要检索了，**不需要再判断「该不该检索」**"],
  ["tool_worker", "系统规则 + 工具描述 + 消息序列",
   "已经确定要调工具了，**不会被「别提来源」干扰决策**"],
  ["direct_llm", "仅系统规则",
   "干净，**没有被检索内容污染的机会**"],
 ],
 [2.6, 7.4, 6.6]),
("p", "**一句话总结**：意图混淆的根源不是模型能力不够，"
      "而是**你在一个 prompt 里让它同时做两个决定**。"
      "Supervisor 的价值就是把这两个决定分到两次独立的生成里。"),

("h3", "Supervisor-Worker 的收益汇总"),
("ul", [
    "Supervisor 只做一件事——意图分类，prompt 很短，`max_tokens=16`，成本与延迟都很低",
    "每个 Worker 的 prompt 只需关注自己那一类任务，不存在指令干扰（见上表）",
    "分类器与生成器是不同参数的独立实例，各自独立调优",
    "新增能力只需加一个 Worker 节点 + 一条条件边，不改动既有分支",
]),

("h3", "公平地说：什么时候单 Agent 反而更合适"),
("p", "这一段建议在面试中**主动讲出来**。它体现的是「你在做权衡」，"
      "而不是「照抄多 Agent 模板」——后者恰恰是面试官最警惕的。"),
("ul", [
    "**只有单一意图**：如果系统只做文档问答、不需要工具调用，"
    "单 Agent 更简单、**少一次 LLM 调用、延迟更低**",
    "**延迟敏感场景**：Supervisor 架构每次请求至少两次 LLM 往返（分类 + 生成），"
    "对首字延迟要求极高的产品，这笔开销需要权衡",
    "**意图空间很小**：只有两三种意图且边界清晰时，用关键词规则也比多一次 LLM 调用划算",
]),
("note", "可以这样回答「你这套架构是不是过度设计」："
         "「单 Agent 的问题在多意图场景下才明显。我拆开是因为有 rag / tool / direct "
         "三种差异很大的任务，塞进一个 prompt 会互相干扰——**但这也是我加 `direct` 分支的原因："
         "简单问题不该付两倍的 LLM 成本**。如果我的系统只做文档问答，我会用单 Agent。」", "tip"),
("note", "如果面试官追问「意图分类为什么用 LLM 而不是规则」，"
         "标准回答是：规则难以覆盖自然语言的表达多样性（「这份文档里怎么配 Redis」和「Redis 配置」是同一意图）。"
         "但**必须补一句**：如果 QPS 高，我会换成小模型分类或给分类结果加缓存——"
         "这能体现你考虑了成本，而不是无脑上 LLM。", "tip"),

("h2", "2.3 LangGraph 图构建代码"),
("p", "图结构定义在 `backend/app/agents/supervisor.py` 的 `_build_graph()` 中。"
      "注意 `add_conditional_edges` 的路由映射表：键是路由函数的返回值，值是目标节点名。"),
("code", """
def _build_graph(self):
    workflow = StateGraph(RAGState)
    workflow.add_node("supervisor", self._supervisor_node)
    workflow.add_node("rag_worker", self._rag_worker)
    workflow.add_node("tool_worker", self._tool_worker)
    workflow.add_node("direct_llm", self._direct_llm)

    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        self._route_after_supervisor,          # 路由函数：读 state["next_worker"]
        {
            "rag_worker": "rag_worker",         # 返回值 → 目标节点
            "tool_worker": "tool_worker",
            "direct_llm": "direct_llm",
        },
    )
    # 每个 Worker 执行完即结束，不做多跳串联
    workflow.add_edge("rag_worker", "__end__")
    workflow.add_edge("tool_worker", "__end__")
    workflow.add_edge("direct_llm", "__end__")
    return workflow.compile()
""", "核心代码 2-1　StateGraph 构建与条件路由"),

("h3", "状态定义：全部字段可选"),
("p", "用 `TypedDict` 的 `total=False` 让每个节点只需返回自己更新的键，"
      "避免节点之间因为字段缺失而互相牵连。"),
("code", """
class RAGState(TypedDict, total=False):
    messages: str          # 当前用户消息
    history: str           # 历史对话（已拼成文本）
    user_id: str           # 用于向量库用户级隔离
    next_worker: str       # supervisor 的路由决策
    token_stream: Any      # AsyncGenerator[str, None]：流式 token 生成器
    agent_trace: List[str] # 执行轨迹，前端可视化用
    tool_calls: List[Dict[str, str]]  # 工具调用记录
""", "核心代码 2-2　图状态定义"),

("h3", "路由函数与兜底"),
("p", "路由函数必须**永不抛异常**：状态里没有 `next_worker` 时兜底到 `direct_llm`，"
      "否则一次分类异常会让整个请求返回 500。"),
("code", """
async def _supervisor_node(self, state: RAGState) -> Dict[str, Any]:
    message = state.get("messages", "")
    history = state.get("history", "")
    prompt = CLASSIFY_PROMPT.format(history=history[:200], message=message)

    try:
        result = await self.classifier_llm.ainvoke(prompt)
        intent = (result.content or "").strip().lower()
    except Exception as exc:
        logger.error("意图分类失败，降级为 direct: %s", exc)
        intent = "direct"

    # 分类器可能输出 "rag." 或 "rag 因为..."，做一次健壮性截取
    if intent not in ("rag", "tool", "direct"):
        for candidate in ("rag", "tool", "direct"):
            if intent.startswith(candidate):
                intent = candidate
                break
        else:
            intent = "direct"

    return {"next_worker": intent,
            "agent_trace": [f"Supervisor 意图分类 → {intent}"]}


def _route_after_supervisor(self, state: RAGState) -> str:
    return state.get("next_worker", "direct_llm")   # 缺字段兜底
""", "核心代码 2-3　意图分类节点与路由函数"),

("pagebreak",),

# ==================================================================== #
("h1", "核心代码精讲", None),
# ==================================================================== #

("h2", "3.1 真实流式输出是怎么实现的"),
("p", "这是本项目**最能体现工程理解**的一个点，因为它和常见的「伪流式」有本质区别。"),

("h3", "先看错误做法"),
("p", "很多 RAG 项目这样写「流式」："),
("code", """
# ✗ 伪流式：先等完整回答，再逐字吐出
async def stream_with_trace(self, message, user_id, history):
    result = await self.graph.ainvoke({...})      # ← 这里阻塞等整段生成完
    response = result.get("response", "")
    for char in response:                          # ← 然后一个字一个字吐
        yield char
""", "反例：整段生成后再逐字吐"),
("p", "前端看到的效果**和真流式一模一样**——文字一个个冒出来。但关键指标没有改善："),
("table",
 ["指标", "伪流式", "真流式"],
 [
  ["首字延迟（TTFT）", "等于整段生成时间（可能 3-5 秒）", "等于 LLM 产出首个 token 的时间（通常 <1 秒）"],
  ["用户感知", "点发送后长时间空白，然后突然开始打字", "立刻开始输出，边想边说"],
  ["超时风险", "长回答容易触发网关超时", "连接持续有数据流动，不易超时"],
 ],
 [4.0, 6.3, 6.3]),

("h3", "本项目的做法"),
("p", "在每个 Worker 节点**内部**调用 LLM 的 `astream()`，把返回的**异步生成器**作为节点出参放进 state。"
      "节点本身立即返回（LangGraph 的图调度不参与流式），"
      "然后由路由层从最终 state 里取出这个生成器，逐 token 透传给 SSE。"),
("code", """
async def _rag_worker(self, state: RAGState) -> Dict[str, Any]:
    message = state.get("messages", "")
    user_id = state.get("user_id") or None
    trace: List[str] = []

    # 向量检索与重排都是阻塞操作（Chroma 查询 / 模型推理），
    # 放进线程池避免阻塞事件循环，否则并发请求会被串行化。
    docs = await asyncio.to_thread(self.rag_retriever.search, message, 10, user_id)
    trace.append(f"RAG Worker 向量召回 {len(docs)} 个候选片段")

    if docs:
        reranked = await asyncio.to_thread(self.reranker.rerank, message, docs, 5)
        context = self._build_context(reranked)
    else:
        trace.append("RAG Worker 未召回相关文档，降级为通用回答")
        context = ""

    # ★ 关键：这里不 await，只构造生成器，节点立即返回
    stream = self._stream_llm(message, state.get("history", ""), context)
    return {"token_stream": stream, "agent_trace": trace}


async def _stream_llm(self, message: str, history: str, context: str):
    system = SYSTEM_PROMPT
    if context:
        system += f"\\n以下是内部参考资料，仅供你组织答案，不要提及来源：\\n{context}\\n"

    messages: List[Any] = [SystemMessage(content=system)]
    if history:
        messages.append(HumanMessage(content=f"以下是此前的对话记录：\\n{history}"))
    messages.append(HumanMessage(content=message))

    async for chunk in self.llm.astream(messages):     # ← 逐 token
        text = self._chunk_text(chunk)
        if text:
            yield text
""", "核心代码 3-1　RAG Worker 与真正的逐 token 流式生成"),

("h3", "一个容易被忽略的健壮性问题"),
("p", "LangChain 流式分片的 `content` 字段**不总是字符串**——"
      "在涉及工具调用或多模态时可能是分块列表。直接拼接会抛类型错误，"
      "所以需要一个归一化函数："),
("code", """
@staticmethod
def _chunk_text(chunk: Any) -> str:
    \"\"\"从 LangChain 流式分片中稳健地取出文本（content 可能是 str 或分块列表）。\"\"\"
    content = getattr(chunk, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)
""", "核心代码 3-2　流式分片文本归一化"),

("h3", "SSE 事件协议设计"),
("p", "没有用默认的纯文本流，而是自定义了 5 类 JSON 事件帧。"
      "原因是：前端需要同时拿到**增量文本**、**Agent 执行轨迹**、**会话 ID** 和**错误信息**，"
      "纯文本流无法区分这些语义。"),
("table",
 ["事件类型", "载荷", "用途"],
 [
  ["`trace`", "`content: string[]`、`tool_calls: {name, arguments}[]`", "渲染 Agent 执行轨迹与工具调用，让「黑盒」变透明"],
  ["`token`", "`content: \"增量文本\"`", "逐 token 追加到消息气泡"],
  ["`conv_id`", "`content: \"<会话ID>\"`", "新会话首次请求后前端需要知道会话 ID"],
  ["`error`", "`content: \"错误信息\"`", "流中途出错时前端能显示原因而不是卡住"],
  ["`done`", "—", "结束标记，前端据此停止读取"],
 ],
 [2.6, 8.2, 5.8]),
("p", "发送侧实现（路由层）："),
("code", """
async for event in agent.stream_with_trace(message, user.id, history_records, db=db):
    event_type = event.get("type")
    if event_type == "token":
        full_response.append(event.get("content", ""))
    elif event_type == "done":
        continue

    frame = {"type": event_type, "content": event.get("content")}
    if event.get("tool_calls"):
        frame["tool_calls"] = event["tool_calls"]
    yield f"data: {json.dumps(frame, ensure_ascii=False)}\\n\\n"

return StreamingResponse(
    event_generator(),
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",      # ★ 关闭 Nginx 缓冲，否则流式会被攒批
    },
)
""", "核心代码 3-3　SSE 帧构造与 StreamingResponse"),
("note", "`X-Accel-Buffering: no` 这个响应头很容易漏。"
         "如果前面挂了 Nginx 而没关缓冲，Nginx 会把多个 SSE 帧攒起来一起发，"
         "流式效果直接消失——这是一个典型的「本地正常、上线就变卡」问题。", "warn"),

("h3", "前端为什么要用 fetch 而不是 EventSource"),
("code", """
// EventSource 只支持 GET，且无法自定义请求头 —— 而我们需要 POST 提交消息
// 并携带 Authorization: Bearer <JWT>，因此必须用 fetch + ReadableStream 手动解析。
const response = await fetch('/api/v1/chat/stream', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  },
  body: JSON.stringify(req),
})

const reader = response.body.getReader()
const decoder = new TextDecoder()
let buffer = ''

while (true) {
  const { done, value } = await reader.read()
  if (done) break

  buffer += decoder.decode(value, { stream: true })
  const lines = buffer.split('\\n')
  buffer = lines.pop() || ''          // 保留最后一段不完整数据，跨 chunk 拼接

  for (const line of lines) {
    if (!line.startsWith('data: ')) continue
    const parsed = JSON.parse(line.slice(6).trim())
    // ...按 parsed.type 分发
  }
}
""", "核心代码 3-4　前端 SSE 流解析（注意 buffer 的跨块拼接）"),
("note", "`buffer = lines.pop()` 这行是流式解析的关键："
         "TCP 分片不保证一个 `data:` 帧完整到达，必须把最后一段不完整的行留在缓冲区等下一块。"
         "漏掉这行会出现「偶尔 JSON 解析失败」的诡异 bug。", "tip"),

("pagebreak",),

# ==================================================================== #
("h1", "RAG 检索链路", None),
# ==================================================================== #

("h2", "4.1 四段式管线"),
("code", """
文档上传
   │
   ▼
① 清洗与分块          RecursiveCharacterTextSplitter 逻辑
   512 字符 / 50 字符重叠，分隔符优先级：\\n\\n → \\n → 。！？ → .!? → 空格
   │
   ▼
② 向量化与存储        bge-small-zh-v1.5（512 维） → Chroma
   metadata: { filename, user_id, doc_id, chunk_index, total_chunks }
   │
   ▼
③ 召回                Chroma 向量检索 Top-10（带 user_id 过滤）
   cosine 距离 → 相似度 = 1 - distance
   │
   ▼
④ 重排与组装          Cross-Encoder 对 (query, passage) 打分 → 保留 Top-5
   拼接为上下文注入 system prompt
""", "图 4-1　RAG 四段式管线"),

("h2", "4.2 为什么需要 Cross-Encoder 重排（面试重点）"),
("p", "这是 RAG 相关问题里**出现频率最高**的追问，必须能讲清楚两阶段检索的原理差异。"),
("table",
 ["对比项", "向量检索（双塔 / bi-encoder）", "Cross-Encoder 重排"],
 [
  ["结构", "query 和 document **分别**独立编码成向量，再算余弦相似度", "把 (query, document) **拼接**后送进同一个模型"],
  ["能否建模交互", "不能。两侧编码时互相看不到对方", "能。注意力机制可以捕捉 query 与 document 的词级关联"],
  ["精度", "受限于「独立编码」，语义匹配有上限", "显著更高"],
  ["速度", "快。文档向量可**离线预计算**，线上只算 query 向量 + 向量检索", "慢。每个候选都要**实时前向推理**，无法预建索引"],
  ["能否预建索引", "可以（HNSW 等近似最近邻索引）", "不可以"],
  ["在本项目中的角色", "第一阶段：从全库快速召回 Top-10 候选", "第二阶段：对 10 个候选精排，保留 Top-5"],
 ],
 [2.8, 6.9, 6.9]),
("note", "一句话总结可以背下来：**「双塔负责快，交叉编码器负责准；用双塔把候选从几万缩到十个，"
         "再用 Cross-Encoder 在这十个里挑出最相关的五个」**。"
         "如果只做向量检索不做重排，等于放弃了精度；如果只做 Cross-Encoder，全库打分的时间无法接受。", "tip"),

("h2", "4.3 重排器实现与降级"),
("code", """
class Reranker:
    \"\"\"Cross-Encoder 重排序（进程内单例，模型延迟加载）。\"\"\"

    _instance: Optional["Reranker"] = None

    def __new__(cls, model_name: str = DEFAULT_MODEL) -> "Reranker":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._model = None
            cls._instance._load_failed = False
            cls._instance.model_name = model_name
        return cls._instance

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if self._load_failed:          # 失败过就不再重试，避免每次请求都卡在加载
            return False
        try:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name)
            return True
        except Exception as exc:
            self._load_failed = True
            logger.warning("Cross-Encoder 加载失败，退化为向量检索顺序: %s", exc)
            return False

    def rerank(self, query, documents, top_k=5) -> List[Document]:
        if not documents:
            return []
        if not self._ensure_model():
            return documents[:top_k]            # ★ 降级：按向量检索原顺序截断

        pairs = [(query, doc.page_content) for doc in documents]
        scores = self._model.predict(pairs)
        scored = sorted(zip(documents, scores), key=lambda item: float(item[1]), reverse=True)

        result = []
        for doc, score in scored[:top_k]:
            metadata = dict(doc.metadata or {})
            metadata["rerank_score"] = float(score)   # 写入分数便于排查
            result.append(Document(page_content=doc.page_content, metadata=metadata))
        return result
""", "核心代码 4-1　Cross-Encoder 重排与降级"),
("p", "设计要点："),
("ul", [
    "**单例 + 延迟加载**：模型加载要几秒、占内存，绝不能每个请求都新建",
    "**失败标记**：`_load_failed` 保证模型不可用时不再反复重试拖慢请求",
    "**降级而非报错**：重排失败时返回向量检索的原始顺序，检索链路整体不中断",
    "**分数落盘**：把 `rerank_score` 写进 metadata，方便排查「为什么召回了却答偏」",
]),

("h2", "4.4 分块策略：为什么不用现成的 splitter"),
("p", "原实现依赖 `langchain_text_splitters`，但该包在 langchain 1.x 生态中已不是默认安装项，"
      "缺失会导致 `app.rag.processor` 导入失败 → 整个服务起不来。"
      "因此改为**零外部依赖的递归分块实现**。"),
("code", """
DEFAULT_SEPARATORS = ["\\n\\n", "\\n", "。", "！", "？", ".", "!", "?",
                     "；", ";", "，", ",", " ", ""]

def _split(self, text: str, separators: List[str]) -> List[str]:
    if len(text) <= self.chunk_size:
        return [text] if text.strip() else []

    separator = separators[0] if separators else ""
    remaining = separators[1:] if len(separators) > 1 else [""]

    if separator == "":                       # 无分隔符可用，硬切
        return self._hard_split(text)

    parts = text.split(separator)
    if len(parts) == 1:                       # 当前分隔符无效，降级到下一优先级
        return self._split(text, remaining)

    chunks, buffer = [], ""
    for part in parts:
        piece = part + separator if part is not parts[-1] else part
        if len(piece) > self.chunk_size:      # 单段超长：冲刷缓冲后继续降级切分
            if buffer:
                chunks.append(buffer); buffer = ""
            chunks.extend(self._split(piece, remaining))
            continue
        if len(buffer) + len(piece) <= self.chunk_size:
            buffer += piece
        else:
            if buffer:
                chunks.append(buffer)
            buffer = piece
    if buffer:
        chunks.append(buffer)

    return self._apply_overlap([c for c in chunks if c.strip()])
""", "核心代码 4-2　递归分块核心逻辑"),
("h3", "修复的隐藏 Bug：清洗把分隔符吃掉了"),
("p", "原实现用 `\" \".join(text.split())` 做清洗——"
      "`str.split()` 不带参数时**会按任意空白切分，包括换行**，"
      "于是 `\\n\\n` 和 `\\n` 这两个最重要的分块分隔符被压成了空格，"
      "语义分块彻底失效。修复方式是只压缩空白、保留换行结构："),
("code", """
@staticmethod
def _clean_text(text: str) -> str:
    text = text.replace("\\xa0", " ").replace("\\r\\n", "\\n").replace("\\r", "\\n")
    lines = [line.rstrip() for line in text.split("\\n")]
    text = "\\n".join(lines)
    while "\\n\\n\\n" in text:          # 3 个以上连续换行压成 2 个，保留段落边界
        text = text.replace("\\n\\n\\n", "\\n\\n")
    normalized = []
    for line in text.split("\\n"):
        normalized.append(" ".join(line.split(" ")) if "  " in line else line)
    return "\\n".join(normalized).strip()
""", "核心代码 4-3　保留换行结构的文本清洗"),
("note", "分块重叠（overlap）的作用：防止答案正好落在两个分块的交界处而检索不到。"
         "本项目设为 50 字符，且 `chunk_overlap` 必须小于 `chunk_size`（构造函数里有校验，"
         "否则切分会无法终止）。", "info"),

("h2", "4.5 中文 Embedding 的选择与踩坑"),
("p", "Chroma 在**不指定 embedding function** 时会使用内置的 `all-MiniLM-L6-v2`——"
      "这是**英文语料**训练的 384 维模型。用它检索中文文档，效果会明显失真，"
      "但因为不会报错，很容易被忽略。本项目改为显式指定 **`BAAI/bge-small-zh-v1.5`**（512 维中文模型）。"),
("h3", "踩坑一：Chroma 重新打开集合时不恢复 embedding function"),
("p", "这是本项目遇到的最隐蔽的一个问题，值得单独讲："),
("code", """
# 现象：
#   第一次运行一切正常（写入 512 维向量成功）
#   进程重启后再运行 → InvalidArgumentError:
#       Collection expecting embedding with dimension of 512, got 384

# 原因：
#   Chroma 在「创建」collection 时会绑定 embedding_function，
#   但在「打开已存在的」collection 时不会恢复它，
#   而是回落到内置的 DefaultEmbeddingFunction（all-MiniLM-L6-v2，384 维）。

# 修复：打开后显式把 embedding function 重新挂回去
existing = self.client.get_collection(name)
existing._embedding_function = ef      # ★ 关键的一行
return existing
""", "核心代码 4-4　Chroma embedding function 丢失的修复"),
("h3", "踩坑二：写入用 __call__，查询用 embed_query"),
("p", "自定义 embedding function 只实现 `__call__` 时，**写入正常但查询恒定失败**。"
      "因为 Chroma 在 `add()` 时走 `__call__`，在 `query()` 时会查找 `embed_query`，"
      "且以**关键字参数 `input=`** 调用，入参可能是字符串也可能是列表。"),
("code", """
class _ChromaEmbeddingFunction:
    def __call__(self, input):                       # 写入路径
        return service.embed_documents(list(input))

    def embed_documents(self, input=None, **kwargs): # 兼容显式调用
        texts = input if input is not None else kwargs.get("texts", [])
        return service.embed_documents(list(texts))

    def embed_query(self, input=None, **kwargs):     # 查询路径（注意关键字名是 input）
        text = input if input is not None else kwargs.get("text", "")
        if isinstance(text, (list, tuple)):
            return service.embed_documents(list(text))
        return service.embed_query(text)
""", "核心代码 4-5　同时满足写入与查询两套调用约定"),
("h3", "踩坑三：HF 缓存目录不可写导致静默降级"),
("p", "默认缓存目录（如 `C:\\Users\\<user>\\.cache\\huggingface`）在受限环境中可能不可写，"
      "模型加载失败后系统会**静默降级为英文 embedding**——检索质量骤降却几乎没有明显报错。"
      "修复方式是在配置阶段就把缓存目录切到项目内可写路径："),
("code", """
def _ensure_hf_cache_dir() -> Optional[str]:
    \"\"\"
    优先尊重用户显式设置的 HF_HOME；否则直接使用项目内的可写目录。
    刻意不去探测系统默认目录是否可写——「父目录可写」不代表
    「要写入的子目录可写」，探测容易误判。
    \"\"\"
    if os.environ.get("HF_HOME"):
        return os.environ["HF_HOME"]

    fallback = BACKEND_DIR / ".hf_cache"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(fallback)
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        return str(fallback)
    except Exception:
        return None
""", "核心代码 4-6　HuggingFace 缓存目录兜底"),

("h2", "4.6 向量库维度自愈"),
("p", "当已存在的索引维度与当前配置不一致（例如从 384 维英文模型切到 512 维中文模型），"
      "继续使用只会让每次读写都抛异常。因此访问层做了自愈：检出不一致就重建 collection，"
      "并把旧数据量打印出来，明确告知需要重新上传文档。"),
("code", """
recorded = (existing.metadata or {}).get("embedding_model")
if recorded == self.embedding_source:
    if ef is not None:
        existing._embedding_function = ef
    return existing

old_count = existing.count()
logger.warning(
    "collection '%s' 的 embedding 与当前配置不一致（索引=%s，当前=%s，已有 %s 个分块）。"
    "正在重建 collection 以保证检索可用；原有向量需要重新上传文档才能恢复。",
    name, recorded, self.embedding_source, old_count,
)
self.client.delete_collection(name)
""", "核心代码 4-7　维度不一致时自愈重建"),
("note", "说明：仅 `delete_collection` 在某些 Chroma 版本下不会清理磁盘上的 HNSW 索引文件，"
         "仍可能出现维度报错。最彻底的做法是删除 `chroma_data` 目录后重新入库。"
         "这点在面试里主动说出来，比被追问出来更好。", "warn"),

("pagebreak",),

# ==================================================================== #
("h1", "Function Calling 实现", None),
# ==================================================================== #

("h2", "5.1 从占位到真实调用"),
("p", "原实现的 `tool_worker` 是假的——把用户问题原样拼成字符串返回："),
("code", """
# ✗ 原实现：根本没有工具调用
async def _tool_worker(self, state):
    tool_result = f"[模拟工具调用] 查询结果: {state['messages']}"
    context = f"工具查询结果：{tool_result}"
    response = await self._generate_response(...)
""", "反例：假的工具调用"),
("p", "改造后的真实调用链路：用 `bind_tools` 让模型返回结构化 `tool_calls`，"
      "执行工具后把结果作为 `ToolMessage` 回填，再让模型基于工具结果生成最终回答。"),

("h2", "5.2 工具定义（LangChain @tool 装饰器）"),
("p", "工具的 **docstring 就是给 LLM 看的工具描述**，写得越清楚，模型选错工具的几率越低。"
      "参数类型注解会被转成 JSON Schema。"),
("code", """
@tool
async def get_knowledge_base_stats() -> str:
    \"\"\"获取当前用户知识库的统计数据，包含文档总数、向量分块总数、会话总数与问答总次数。
    当用户询问"知识库有多少内容""我总共问了多少问题"等统计类问题时使用。\"\"\"
    ctx = _ctx()

    doc_count = await ctx.db.scalar(
        select(func.count()).select_from(Document).where(Document.user_id == ctx.user_id)
    )
    chunk_sum = await ctx.db.scalar(
        select(func.coalesce(func.sum(Document.chunk_count), 0))
        .where(Document.user_id == ctx.user_id)
    )
    conv_count = await ctx.db.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == ctx.user_id)
    )
    chat_count = await ctx.db.scalar(
        select(func.count()).select_from(ChatHistory).where(ChatHistory.user_id == ctx.user_id)
    )
    vector_count = RAGRetriever().count(user_id=ctx.user_id)

    return (f"知识库统计：\\n· 文档数量：{doc_count or 0} 份\\n"
            f"· 数据库记录分块数：{chunk_sum or 0}\\n"
            f"· 向量库实际分块数：{vector_count}\\n"
            f"· 会话数量：{conv_count or 0} 个\\n"
            f"· 历史问答次数：{chat_count or 0} 次")
""", "核心代码 5-1　真实工具实现（查询真实数据）"),

("h2", "5.3 四个工具一览"),
("table",
 ["工具名", "参数", "作用", "触发场景"],
 [
  ["`list_user_documents`", "无", "列出知识库全部文档及分块数", "「我上传了哪些文档」"],
  ["`get_knowledge_base_stats`", "无", "文档数 / 分块数 / 会话数 / 问答次数统计", "「知识库有多少内容」"],
  ["`search_document_by_filename`", "`keyword`", "按文件名关键词模糊查找", "「有没有关于 XX 的文档」"],
  ["`search_document_content`", "`keyword`", "全文检索关键词并返回命中片段", "「哪份文档提到了 XX」"],
 ],
 [5.0, 2.2, 5.4, 4.0]),

("h2", "5.4 工具上下文的并发隔离（重要）"),
("p", "工具需要访问数据库会话与当前用户 ID，但这两个东西**不能放进 JSON Schema**。"
      "最直觉的做法是用模块级全局变量传递——**这在并发下会串用户数据**。"),
("code", """
# ✗ 危险做法：模块级全局变量
_context: Optional[ToolContext] = None

def set_tool_context(db, user_id):
    global _context
    _context = ToolContext(db, user_id)      # 两个用户并发请求会互相覆盖！
""", "反例：全局变量传上下文"),
("p", "正确做法是用 `contextvars`：它对每个 asyncio Task 独立，"
      "并发的 SSE 请求各自持有自己的上下文，不会串数据。"),
("code", """
import contextvars

_tool_context: contextvars.ContextVar[Optional[ToolContext]] = contextvars.ContextVar(
    "codepilot_tool_context", default=None
)

def set_tool_context(db: Any, user_id: str) -> None:
    _tool_context.set(ToolContext(db, user_id))

def _ctx() -> ToolContext:
    ctx = _tool_context.get()
    if ctx is None:
        raise RuntimeError("工具上下文未初始化：请先调用 set_tool_context(db, user_id)")
    return ctx
""", "核心代码 5-2　用 contextvars 做请求级上下文隔离"),
("note", "这个点非常适合在面试里主动讲，因为它同时体现了**异步编程理解**和**安全意识**。"
         "可以说：「工具需要拿到数据库会话，但如果用全局变量传递，"
         "两个用户同时提问就会读到对方的 user_id——所以我用了 contextvars。」", "tip"),

("h2", "5.5 工具调用主循环"),
("code", """
async def _tool_worker(self, state: RAGState) -> Dict[str, Any]:
    message = state.get("messages", "")
    trace: List[str] = []
    tool_calls_log: List[Dict[str, str]] = []

    messages: List[Any] = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=message),
    ]

    try:
        for _round in range(MAX_TOOL_ROUNDS):          # 最多 3 轮，防止无限循环
            # 这里用非流式拿完整 tool_calls，避免流式分片拼接的复杂度
            decision: AIMessage = await self.tool_llm.ainvoke(messages)
            calls = getattr(decision, "tool_calls", None) or []
            if not calls:
                break

            messages.append(decision)
            for call in calls:
                name = call.get("name", "")
                args = call.get("args", {}) or {}
                trace.append(f"Tool Worker 调用工具 {name}({self._fmt_args(args)})")
                result = await execute_tool(name, args)
                tool_calls_log.append({"name": name,
                                       "arguments": json.dumps(args, ensure_ascii=False)})
                # ★ 结果必须以 ToolMessage 形式回填，且带上 tool_call_id
                messages.append(ToolMessage(content=result, tool_call_id=call.get("id", "")))
    except Exception as exc:
        logger.error("工具调用链路失败，降级为直答: %s", exc, exc_info=True)
        trace.append(f"Tool Worker 工具调用失败（{type(exc).__name__}），降级为直答")

    stream = self._stream_messages(messages)    # 最终回答仍然流式
    return {"token_stream": stream, "agent_trace": trace, "tool_calls": tool_calls_log}
""", "核心代码 5-3　Function Calling 主循环"),
("p", "几个关键设计决策："),
("ul", [
    "**决策阶段用非流式**：流式返回的 `tool_calls` 是分片到达的，需要手动拼接，"
    "复杂度高且容易出错；工具决策本身输出很短，非流式的延迟可以接受，"
    "而**最终回答仍然流式**，用户体验不受影响",
    "**轮次上限 3**：防止模型陷入「调用工具 → 再调用工具」的无限循环",
    "**异常降级为直答**：工具链路出错时不返回 500，而是退化为普通对话回答",
    "**执行错误转文本**：`execute_tool` 内部捕获异常并返回可读文本，"
    "让 LLM 能基于错误信息组织回答，而不是让整个请求失败",
]),

("pagebreak",),
]

# ------------------------------------------------------------------ #
# 与第二部分合并（数据层 / 缓存降级 / 安全 / 工程实践 / 面试题 / 附录）
# ------------------------------------------------------------------ #
from doc_content_part2 import BLOCKS_PART2  # noqa: E402

BLOCKS = BLOCKS_PART1 + BLOCKS_PART2
