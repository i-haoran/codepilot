"""
Supervisor 多 Agent 编排（LangGraph StateGraph）。

架构
----
                    ┌──────────────┐
    用户消息 ───────▶│  supervisor  │   LLM 意图分类（rag / tool / direct）
                    └──────┬───────┘
             add_conditional_edges 按 next_worker 路由
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌───────────┐   ┌────────────┐   ┌────────────┐
    │ rag_worker│   │ tool_worker│   │ direct_llm │
    └─────┬─────┘   └──────┬─────┘   └──────┬─────┘
          │                │                │
          └────────────────┴────────────────┘
                           ▼
                        __end__ → token 生成器 → SSE 逐 token 下发

流式改造要点（原实现的问题）
--------------------------
原 `stream_with_trace()` 先 `await graph.ainvoke(...)` 拿到**完整回答字符串**，
再 `for char in response: yield char` 逐字吐出。前端看着像流式，
但首字延迟（TTFT）等于整段生成时间，没有任何改善。

现在改为：每个 Worker 节点在内部调用 LLM 的 **`astream()`**，
把异步生成器作为节点出参返回；节点本身仍立即返回（图调度不做流式），
由路由层从最终 state 取出生成器并逐 token 透传给 SSE。
这样 TTFT 就只等于 LLM 产出首个 token 的时间。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph

from app.core.config import settings
from app.rag.retriever import RAGRetriever
from app.rag.reranker import Reranker
from app.rag.tools import ALL_TOOLS, clear_tool_context, execute_tool, set_tool_context

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是 CodePilot 研发知识助手，服务团队内部的技术文档问答。\n"
    "严格遵守以下规则：\n"
    "1. 绝对不要说“根据知识库”“参考文档”“文档中提到”“根据提供的资料”等类似表述\n"
    "2. 绝对不要在回答开头或结尾解释你是如何获取信息的\n"
    "3. 直接给出答案，就像你本来就知道一样\n"
    "4. 语言简洁专业，使用 Markdown 组织要点\n"
)

CLASSIFY_PROMPT = (
    "你是一个意图分类器。根据用户问题判断应该路由到哪个处理模块。\n"
    "可选模块：\n"
    "- rag: 需要查询知识库/文档/技术文档/代码规范/API文档\n"
    "- tool: 需要查询统计数据、列举我上传的文档、按文件名查找文档、统计会话数量\n"
    "- direct: 普通对话/通用问题/闲聊/写代码\n\n"
    "对话历史：{history}\n"
    "用户问题：{message}\n\n"
    "只回复一个词：rag 或 tool 或 direct"
)

# 单次回答的 token 上限保护，避免异常情况下的超长输出
MAX_TOOL_ROUNDS = 3


class RAGState(TypedDict, total=False):
    """图状态。字段全部可选，节点只需返回自己更新的键。"""

    messages: str
    history: str
    user_id: str
    next_worker: str
    token_stream: Any          # AsyncGenerator[str, None]
    agent_trace: List[str]
    tool_calls: List[Dict[str, str]]


class SupervisorAgent:
    """Supervisor-Worker 多 Agent 编排器。"""

    def __init__(self) -> None:
        # 参数名必须用 api_key：新版 langchain-openai 已移除 openai_api_key 别名，
        # 用旧名会导致 pydantic 校验阶段直接抛 OpenAIError（Missing credentials）。
        self.llm = ChatOpenAI(
            model="deepseek-chat",
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=2000,
            streaming=True,
        )
        # 意图分类是短输出任务：单独用 low max_tokens 的实例，成本和延迟都更低
        self.classifier_llm = ChatOpenAI(
            model="deepseek-chat",
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=16,
            streaming=False,
        )
        # 绑定工具：让 LLM 能返回结构化的 tool_calls
        self.tool_llm = self.llm.bind_tools(ALL_TOOLS)

        self.rag_retriever = RAGRetriever()
        self.reranker = Reranker()
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # 图构建
    # ------------------------------------------------------------------ #
    def _build_graph(self):
        workflow = StateGraph(RAGState)
        workflow.add_node("supervisor", self._supervisor_node)
        workflow.add_node("rag_worker", self._rag_worker)
        workflow.add_node("tool_worker", self._tool_worker)
        workflow.add_node("direct_llm", self._direct_llm)

        workflow.set_entry_point("supervisor")
        workflow.add_conditional_edges(
            "supervisor",
            self._route_after_supervisor,
            {
                "rag_worker": "rag_worker",
                "tool_worker": "tool_worker",
                "direct_llm": "direct_llm",
            },
        )
        workflow.add_edge("rag_worker", "__end__")
        workflow.add_edge("tool_worker", "__end__")
        workflow.add_edge("direct_llm", "__end__")
        return workflow.compile()

    # ------------------------------------------------------------------ #
    # 节点
    # ------------------------------------------------------------------ #
    async def _supervisor_node(self, state: RAGState) -> Dict[str, Any]:
        """只做意图分类，输出 rag / tool / direct。"""
        message = state.get("messages", "")
        history = state.get("history", "")
        prompt = CLASSIFY_PROMPT.format(history=history[:200], message=message)

        try:
            result = await self.classifier_llm.ainvoke(prompt)
            intent = (result.content or "").strip().lower()
        except Exception as exc:  # noqa: BLE001
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

        return {
            "next_worker": intent,
            "agent_trace": [f"Supervisor 意图分类 → {intent}"],
        }

    def _route_after_supervisor(self, state: RAGState) -> str:
        return state.get("next_worker", "direct_llm")

    async def _rag_worker(self, state: RAGState) -> Dict[str, Any]:
        """向量召回 → Cross-Encoder 重排 → 组装上下文 → 流式生成。"""
        message = state.get("messages", "")
        user_id = state.get("user_id") or None
        trace: List[str] = []

        # 向量检索与重排都是阻塞操作（Chroma 查询 / 模型推理），
        # 放进线程池避免阻塞事件循环，否则并发请求会被串行化。
        docs: List[Document] = await asyncio.to_thread(
            self.rag_retriever.search, message, 10, user_id
        )
        trace.append(f"RAG Worker 向量召回 {len(docs)} 个候选片段")

        if docs:
            reranked: List[Document] = await asyncio.to_thread(
                self.reranker.rerank, message, docs, 5
            )
            if reranked:
                top_score = reranked[0].metadata.get("rerank_score")
                score_text = f"，最高分 {top_score:.4f}" if isinstance(top_score, float) else ""
                trace.append(f"RAG Worker Cross-Encoder 重排后保留 {len(reranked)} 个片段{score_text}")
            context = self._build_context(reranked)
        else:
            reranked = []
            trace.append("RAG Worker 未召回相关文档，降级为通用回答")
            context = ""

        stream = self._stream_llm(message, state.get("history", ""), context)
        return {"token_stream": stream, "agent_trace": trace}

    async def _tool_worker(self, state: RAGState) -> Dict[str, Any]:
        """
        真实 Function Calling。

        LLM 先决定调用哪个工具（streaming=False 拿完整 tool_calls，避免流式分片
        拼接的复杂度），执行工具后把结果作为 ToolMessage 回填，再流式生成最终回答。
        """
        message = state.get("messages", "")
        user_id = state.get("user_id", "")
        trace: List[str] = []
        tool_calls_log: List[Dict[str, str]] = []

        if not settings.DEEPSEEK_API_KEY:
            trace.append("Tool Worker 未配置 API Key，降级为直答")
            return {
                "token_stream": self._stream_llm(message, state.get("history", ""), ""),
                "agent_trace": trace,
                "tool_calls": [],
            }

        messages: List[Any] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=message),
        ]

        try:
            for _round in range(MAX_TOOL_ROUNDS):
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
                    tool_calls_log.append({"name": name, "arguments": json.dumps(args, ensure_ascii=False)})
                    messages.append(ToolMessage(content=result, tool_call_id=call.get("id", "")))
        except Exception as exc:  # noqa: BLE001
            logger.error("工具调用链路失败，降级为直答: %s", exc, exc_info=True)
            trace.append(f"Tool Worker 工具调用失败（{type(exc).__name__}），降级为直答")

        if not tool_calls_log:
            trace.append("Tool Worker 未触发工具调用，走通用回答")

        stream = self._stream_messages(messages)
        return {"token_stream": stream, "agent_trace": trace, "tool_calls": tool_calls_log}

    async def _direct_llm(self, state: RAGState) -> Dict[str, Any]:
        stream = self._stream_llm(state.get("messages", ""), state.get("history", ""), "")
        return {"token_stream": stream, "agent_trace": ["Direct LLM 通用对话模式"]}

    # ------------------------------------------------------------------ #
    # 流式生成
    # ------------------------------------------------------------------ #
    def _build_context(self, docs: List[Document]) -> str:
        blocks = []
        for doc in docs:
            filename = (doc.metadata or {}).get("filename", "未知文档")
            blocks.append(f"【{filename}】\n{doc.page_content}")
        return "\n\n".join(blocks)

    @staticmethod
    def _fmt_args(args: Dict[str, Any]) -> str:
        return ", ".join(f"{k}={v}" for k, v in args.items()) if args else ""

    async def _stream_llm(self, message: str, history: str, context: str) -> AsyncGenerator[str, None]:
        """单轮流式生成（用于 rag / direct 分支）。"""
        system = SYSTEM_PROMPT
        if context:
            system += f"\n以下是内部参考资料，仅供你组织答案，不要提及来源：\n{context}\n"

        messages: List[Any] = [SystemMessage(content=system)]
        if history:
            messages.append(HumanMessage(content=f"以下是此前的对话记录，供你参考上下文：\n{history}"))
        messages.append(HumanMessage(content=message))

        async for chunk in self.llm.astream(messages):
            text = self._chunk_text(chunk)
            if text:
                yield text

    async def _stream_messages(self, messages: List[Any]) -> AsyncGenerator[str, None]:
        """基于已有消息序列流式生成最终回答（用于 tool 分支）。"""
        async for chunk in self.llm.astream(messages):
            text = self._chunk_text(chunk)
            if text:
                yield text

    @staticmethod
    def _chunk_text(chunk: Any) -> str:
        """从 LangChain 流式分片中稳健地取出文本（content 可能是字符串或分块列表）。"""
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

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def _build_history(self, history_records) -> str:
        lines = []
        for record in history_records or []:
            lines.append(f"用户：{record.message}\n助手：{record.response}")
        return "\n".join(lines)

    async def _run_graph(self, message: str, user_id: str, history_records) -> RAGState:
        history = self._build_history(history_records)
        return await self.graph.ainvoke({
            "messages": message,
            "history": history,
            "user_id": user_id,
            "next_worker": "",
            "agent_trace": [],
            "tool_calls": [],
        })

    async def run(self, message: str, user_id: str, history_records=None) -> str:
        """非流式：把生成器拼成完整字符串返回（供 /chat 同步接口使用）。"""
        state = await self._run_graph(message, user_id, history_records)
        stream = state.get("token_stream")
        if stream is None:
            return "抱歉，我无法回答这个问题。"
        parts = [chunk async for chunk in stream]
        return "".join(parts) or "抱歉，我无法回答这个问题。"

    async def stream_with_trace(
        self, message: str, user_id: str, history_records=None, db: Any = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式接口：依次下发 trace / token / done 事件。

        事件类型：
          {"type": "trace", "content": [...]}          执行轨迹与工具调用记录
          {"type": "token", "content": "增量文本"}      逐 token 输出
          {"type": "done"}                              结束标记
        """
        if db is not None:
            set_tool_context(db, user_id)
        try:
            state = await self._run_graph(message, user_id, history_records)

            trace = list(state.get("agent_trace") or [])
            tool_calls = state.get("tool_calls") or []
            yield {"type": "trace", "content": trace, "tool_calls": tool_calls}

            stream = state.get("token_stream")
            if stream is None:
                yield {"type": "token", "content": "抱歉，我无法回答这个问题。"}
            else:
                produced = False
                async for text in stream:
                    produced = True
                    yield {"type": "token", "content": text}
                if not produced:
                    yield {"type": "token", "content": "抱歉，我无法回答这个问题。"}

            yield {"type": "done"}
        except Exception as exc:  # noqa: BLE001
            logger.error("流式生成失败: %s", exc, exc_info=True)
            yield {"type": "error", "content": f"生成失败：{exc}"}
            yield {"type": "done"}
        finally:
            clear_tool_context()
