"""
Supervisor 多 Agent 协作编排（LangGraph StateGraph）。

这不是"每个分支跑完就结束"的单跳路由，而是带**反馈回路**的多 Agent 协作：

                    用户消息
                       │
                       ▼
                 ┌───────────┐
                 │ supervisor│  LLM 意图分类 → rag / tool / direct
                 └─────┬─────┘
                       │ add_conditional_edges
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
  ┌───────────┐  ┌────────────┐  ┌────────────┐
  │ rag_worker│  │ tool_worker│  │ direct_llm │   生成草稿 + 证据
  └─────┬─────┘  └─────┬──────┘  └─────┬──────┘
        └──────────────┼──────────────┘
                       ▼
                 ┌───────────┐
                 │ verifier  │  独立审查 Agent：核对依据、是否答到点
                 └─────┬─────┘
                       │
          ┌────────────┴────────────┐
          │ 不通过                   │ 通过
          ▼（带反馈重新生成，≤3轮）    ▼
   ←──── 回到对应 worker        ┌──────────┐
          │                     │ finalize │ 下发定稿
          │ 超过轮次上限          └────┬─────┘
          └──→ 诚实拒答 ─────────────► │
                                      ▼
                                    __end__

设计要点
--------
1. **决策与执行分离**：supervisor 只分类，worker 只生成，verifier 只判定。
2. **Agent 之间有真实协作**：verifier 的判定与修改意见会作为 `revision_feedback`
   回传给 worker，触发带反馈的重新生成——这是图级循环，不是单跳路由。
3. **生成与下发解耦**：worker 产出完整草稿（非流式）供审查，只有通过审查的内容
   才由 finalize 下发。代价是首字延迟增加一次审查时间；换来的是
   **不会把未经核对的答案推给用户**。
4. **诚实拒答**：连续 N 轮未通过审查时，明确告知无法可靠回答，而不是编造。
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
from typing import Annotated, Any, AsyncGenerator, Dict, List, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph

from app.agents.verifier import Verdict, VerifierAgent
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
    "3. 参考资料存在时，只使用其中确实存在的信息，不要补充资料中没有的事实、数字或结论\n"
    "4. 参考资料不足时分两种情况处理：\n"
    "   a) 问题是**通用技术知识**（某个工具/语言的通用用法、概念解释、算法等）：\n"
    "      正常作答，给出通用知识即可，**不要因为参考资料缺失就拒绝回答**；\n"
    "   b) 问题是**本项目专属信息**（内部文件名、统计数据、内部接口与配置、团队规范等）：\n"
    "      明确说明资料中没有该信息，不要猜测。\n"
    "5. 语言简洁专业，使用 Markdown 组织要点\n"
)

CLASSIFY_PROMPT = (
    "你是一个意图分类器，负责判断用户问题应该由哪个模块处理。\n"
    "\n"
    "【rag】用户询问某个具体技术主题、工具用法、命令、规范或配置——"
    "这类问题**可能写在内部文档里**，需要先查知识库。\n"
    "  例：git 怎么合并分支 / redis 怎么配置 / 这个项目的接口规范 / nginx 超时怎么调\n"
    "  注意：像 git、redis、docker、nginx 这类**通用技术工具**的用法问题也属于 rag——\n"
    "  内部文档往往就是这类速查手册，必须先检索确认文档里有没有。\n"
    "\n"
    "【tool】需要查询统计数据或文档清单，答案来自数据库而非文档内容。\n"
    "  例：知识库里有哪些文档 / 一共多少份文档 / 有没有关于 xx 的文件 / 我问过多少问题\n"
    "\n"
    "【direct】不依赖任何内部资料的请求。\n"
    "  例：你好 / 谢谢 / 你是谁 / 用 Python 写个快排 / 解释一下什么是递归 / 推荐几本书\n"
    "\n"
    "判断原则：**只要问题涉及具体技术主题或工具用法，优先选 rag**；\n"
    "只有明确是闲聊、通用编程知识、或与内部资料无关的请求时才选 direct。\n"
    "\n"
    "对话历史：{history}\n"
    "用户问题：{message}\n"
    "\n"
    "只回复一个词：rag 或 tool 或 direct"
)

MAX_TOOL_ROUNDS = 3     # tool_worker 内部工具调用轮次上限
MAX_REVISIONS = 3       # 「生成 → 审查 → 重新生成」轮次上限

# ---------------------------------------------------------------------- #
# 答案来源标记
# ---------------------------------------------------------------------- #
# 用户必须能区分「答案来自我上传的文档」还是「模型的通用知识」，
# 否则会误把模型的自由发挥当成内部资料，也会误以为"文档里没有"。
# 来源被显式建模，并同时用于两处：
#   · 后端：在答案正文前加免责说明（即使前端不渲染标记，也不会误导用户）
#   · SSE：随 trace 事件下发，供前端渲染来源徽标
ANSWER_FROM_KB = "knowledge_base"        # 检索命中，答案基于知识库文档
ANSWER_FROM_TOOLS = "tools"              # 基于工具查询到的实时数据
ANSWER_NOT_RETRIEVED = "not_retrieved"   # 检索了但未命中，退化为通用知识
ANSWER_NO_RETRIEVAL = "no_retrieval"     # 该问题本就不需要检索（闲聊/写代码等）
ANSWER_REFUSED = "refused"               # 审查未通过，诚实拒答

# 需要明确提示「未基于知识库」的来源
UNGROUNDED_SOURCES = {ANSWER_NOT_RETRIEVED, ANSWER_REFUSED}

SOURCE_LABELS = {
    ANSWER_FROM_KB: "基于知识库",
    ANSWER_FROM_TOOLS: "基于实时查询",
    ANSWER_NOT_RETRIEVED: "未检索到 · 通用知识",
    ANSWER_NO_RETRIEVAL: "通用对话",
    ANSWER_REFUSED: "无法回答",
}

# 未命中知识库时的正文提示：必须让用户一眼看出这不是文档内容
NOT_RETRIEVED_NOTICE = (
    "> ⚠️ **本次回答未基于你的知识库**：检索未命中相关片段。\n"
    "> 以下内容来自模型的通用知识，可能不适用于你的项目，请自行核实。\n\n"
)

INTENT_TO_WORKER = {"rag": "rag_worker", "tool": "tool_worker", "direct": "direct_llm"}
WORKER_LABELS = {
    "rag_worker": "RAG Worker",
    "tool_worker": "Tool Worker",
    "direct_llm": "Direct LLM",
}


class RAGState(TypedDict, total=False):
    """
    图状态。字段全部可选，节点只需返回自己更新的键。

    注意 `agent_trace` 与 `review_history` 使用了 **reducer**（Annotated + operator.add）：
    LangGraph 对同名字段的默认行为是「后写入的覆盖先写入的」，
    若不加 reducer，每个节点返回的 trace 会把前面所有节点的 trace 冲掉，
    最终只剩最后一行的轨迹——那样「可观测性」就形同虚设。
    加上 reducer 后，各节点的 trace 会**按执行顺序累加**。
    """

    messages: str
    history: str
    user_id: str
    next_worker: str
    draft: str                 # 当前草稿答案
    evidence: str              # 该分支掌握的参考资料（供审查使用）
    revision_feedback: str     # 审查不通过时的修改意见，回传给 worker
    revision_count: int        # 已重新生成次数
    agent_trace: Annotated[List[str], operator.add]
    review_history: Annotated[List[str], operator.add]
    tool_calls: List[Dict[str, str]]
    response_text: str         # finalize 定稿的最终答案
    verdict_passed: bool       # 审查结论：是否通过
    verdict_reason: str        # 审查结论：原因
    answer_source: str         # 答案来源（见上方 ANSWER_* 常量），用于提示用户


class SupervisorAgent:
    """Supervisor-Worker + Verifier 的多 Agent 协作编排器。"""

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
        self.verifier = VerifierAgent()
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
        workflow.add_node("verifier", self._verifier_node)
        workflow.add_node("revise", self._revise_node)
        workflow.add_node("finalize", self._finalize_node)

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

        # 三个 Worker 产出草稿后，统一交给审查 Agent
        for worker in ("rag_worker", "tool_worker", "direct_llm"):
            workflow.add_edge(worker, "verifier")

        # 审查结果决定：交付 / 带反馈重新生成
        workflow.add_conditional_edges(
            "verifier",
            self._route_after_verifier,
            {"finalize": "finalize", "revise": "revise"},
        )

        # revise 是瞬时路由节点：不做计算，只把控制流送回对应 Worker，
        # 从而在图上形成「worker → verifier → worker」的真实回路。
        workflow.add_conditional_edges(
            "revise",
            self._route_to_worker,
            {
                "rag_worker": "rag_worker",
                "tool_worker": "tool_worker",
                "direct_llm": "direct_llm",
            },
        )

        workflow.add_edge("finalize", "__end__")
        return workflow.compile()

    # ------------------------------------------------------------------ #
    # 节点：supervisor
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
        if intent not in INTENT_TO_WORKER:
            for candidate in INTENT_TO_WORKER:
                if intent.startswith(candidate):
                    intent = candidate
                    break
            else:
                intent = "direct"

        return {
            "next_worker": INTENT_TO_WORKER[intent],
            "agent_trace": [f"Supervisor 意图分类 → {intent}"],
            "revision_count": 0,
            "review_history": [],
        }

    def _route_after_supervisor(self, state: RAGState) -> str:
        return state.get("next_worker", "direct_llm")

    # ------------------------------------------------------------------ #
    # 节点：三个 Worker（产出草稿 + 证据）
    # ------------------------------------------------------------------ #
    async def _rag_worker(self, state: RAGState) -> Dict[str, Any]:
        """向量召回 → Cross-Encoder 重排 → 组装上下文 → 生成草稿。"""
        message = state.get("messages", "")
        user_id = state.get("user_id") or None
        trace: List[str] = []

        # 向量检索与重排都是阻塞操作（Chroma 查询 / 模型推理），
        # 放进线程池避免阻塞事件循环，否则并发请求会被串行化。
        docs: List[Document] = await asyncio.to_thread(
            self.rag_retriever.search, message, 10, user_id
        )
        trace.append(f"RAG Worker 向量召回 {len(docs)} 个候选片段")

        # 相关性过滤：向量检索**一定会返回 top-k**，即使全部毫不相关。
        # 不过滤就会出现「知识库里没有这个内容，却声称基于知识库回答」，
        # 把无关片段当证据注入 prompt。低于阈值的候选直接丢弃。
        #
        # 阈值取自 RAM 检索的向量相似度（而非重排分数）——原因见 config.py 中
        # RETRIEVE_MIN_SCORE 的说明：英文 Cross-Encoder 对中文无区分度。
        min_score = getattr(settings, "RETRIEVE_MIN_SCORE", 0.0) or 0.0
        if docs and min_score > 0:
            kept = [
                d for d in docs
                if isinstance(d.metadata.get("vector_score"), float)
                and d.metadata["vector_score"] >= min_score
            ]
            if len(kept) != len(docs):
                trace.append(
                    f"RAG Worker 相关性过滤：保留 {len(kept)}/{len(docs)} 个片段"
                    f"（相似度阈值 {min_score}）"
                )
            docs = kept

        if docs:
            reranked: List[Document] = await asyncio.to_thread(
                self.reranker.rerank, message, docs, 5
            )
            if reranked:
                top_score = reranked[0].metadata.get("rerank_score")
                score_text = f"，最高分 {top_score:.4f}" if isinstance(top_score, float) else ""
                trace.append(f"RAG Worker Cross-Encoder 重排后保留 {len(reranked)} 个片段{score_text}")
            evidence = self._build_context(reranked)
            # 只有真正拿到证据才算「基于知识库」
            source = ANSWER_FROM_KB if evidence else ANSWER_NOT_RETRIEVED
        else:
            trace.append("RAG Worker 未召回相关文档，将退化为通用知识作答")
            evidence = ""
            source = ANSWER_NOT_RETRIEVED

        draft = await self._generate_draft(
            message, state.get("history", ""), evidence, state.get("revision_feedback", "")
        )
        return {
            "draft": draft,
            "evidence": evidence,
            "agent_trace": trace,
            "answer_source": source,
        }

    async def _tool_worker(self, state: RAGState) -> Dict[str, Any]:
        """
        真实 Function Calling，产出草稿与工具结果作为证据。

        LLM 先决定调用哪个工具（streaming=False 拿完整 tool_calls，避免流式分片
        拼接的复杂度），执行工具后把结果作为 ToolMessage 回填，再生成草稿。
        """
        message = state.get("messages", "")
        trace: List[str] = []
        tool_calls_log: List[Dict[str, str]] = []
        evidence_parts: List[str] = []

        if not settings.DEEPSEEK_API_KEY:
            trace.append("Tool Worker 未配置 API Key，降级为直答")
            draft = await self._generate_draft(message, state.get("history", ""), "", "")
            return {
                "draft": draft, "evidence": "", "agent_trace": trace,
                "tool_calls": [], "answer_source": ANSWER_NO_RETRIEVAL,
            }

        system = SYSTEM_PROMPT
        feedback = state.get("revision_feedback", "")
        if feedback:
            system += (
                "\n\n【上一轮审查未通过，请严格按以下意见修正】\n"
                f"{feedback}\n"
                "如果工具结果确实无法支撑回答，请如实说明，不要编造。\n"
            )

        messages: List[Any] = [
            SystemMessage(content=system),
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
                    evidence_parts.append(f"【工具 {name} 返回】\n{result}")
                    messages.append(ToolMessage(content=result, tool_call_id=call.get("id", "")))
        except Exception as exc:  # noqa: BLE001
            logger.error("工具调用链路失败，降级为直答: %s", exc, exc_info=True)
            trace.append(f"Tool Worker 工具调用失败（{type(exc).__name__}），降级为直答")

        if not tool_calls_log:
            trace.append("Tool Worker 未触发工具调用，走通用回答")

        messages.append(HumanMessage(
            content="请基于以上工具返回的结果，直接回答用户的问题。只使用结果中确实存在的信息。"
        ))
        draft = await self._generate_draft_from_messages(messages)

        # 有工具结果才算「基于实时查询」，否则只是通用回答
        source = ANSWER_FROM_TOOLS if evidence_parts else ANSWER_NO_RETRIEVAL
        return {
            "draft": draft,
            "evidence": "\n\n".join(evidence_parts),
            "agent_trace": trace,
            "tool_calls": tool_calls_log,
            "answer_source": source,
        }

    async def _direct_llm(self, state: RAGState) -> Dict[str, Any]:
        draft = await self._generate_draft(
            state.get("messages", ""), state.get("history", ""), "",
            state.get("revision_feedback", ""),
        )
        # 该分支本就不检索知识库，如实标记为通用对话
        return {
            "draft": draft,
            "evidence": "",
            "agent_trace": ["Direct LLM 通用对话模式"],
            "answer_source": ANSWER_NO_RETRIEVAL,
        }

    # ------------------------------------------------------------------ #
    # 节点：verifier —— 独立审查 Agent
    # ------------------------------------------------------------------ #
    async def _verifier_node(self, state: RAGState) -> Dict[str, Any]:
        question = state.get("messages", "")
        draft = state.get("draft", "")
        evidence = state.get("evidence", "")
        worker = state.get("next_worker", "")
        attempt = int(state.get("revision_count", 0)) + 1

        verdict = await self.verifier.review(
            question=question, draft=draft, evidence=evidence, route=worker, attempt=attempt
        )

        detail = f"Verifier 审查（第 {attempt} 轮）→ {verdict.label}"
        if verdict.reason:
            detail += f"：{verdict.reason}"
        trace = [detail]
        if verdict.unsupported_claims:
            trace.append("Verifier 发现无依据断言：" + "；".join(verdict.unsupported_claims[:3]))

        # 判定结果写入 state：_route_after_verifier / _finalize_node 需要读它
        return {
            "review_history": [
                f"attempt={attempt} pass={verdict.passed} reason={verdict.reason}"
            ],
            "revision_feedback": verdict.feedback,
            "agent_trace": trace,
            "verdict_passed": verdict.passed,
            "verdict_reason": verdict.reason,
        }

    def _route_after_verifier(self, state: RAGState) -> str:
        """通过 → 交付；不通过且有重试额度 → 带反馈重做；否则 → 诚实拒答。"""
        if state.get("verdict_passed", True):
            return "finalize"
        if int(state.get("revision_count", 0)) + 1 >= MAX_REVISIONS:
            logger.warning("审查连续 %s 轮未通过，转为诚实拒答", MAX_REVISIONS)
            return "finalize"
        return "revise"

    async def _revise_node(self, state: RAGState) -> Dict[str, Any]:
        """瞬时节点：不做计算，只递增轮次并把控制流送回对应 Worker。"""
        count = int(state.get("revision_count", 0)) + 1
        return {
            "revision_count": count,
            "agent_trace": [f"Supervisor 要求第 {count + 1} 次重新生成（携带审查意见）"],
        }

    def _route_to_worker(self, state: RAGState) -> str:
        return state.get("next_worker", "direct_llm")

    # ------------------------------------------------------------------ #
    # 节点：finalize —— 只有通过审查的内容才会下发
    # ------------------------------------------------------------------ #
    async def _finalize_node(self, state: RAGState) -> Dict[str, Any]:
        source = state.get("answer_source", ANSWER_NO_RETRIEVAL)

        if state.get("verdict_passed", True):
            text = state.get("draft", "")
            trace = ["Finalize 已通过审查，输出定稿"]

            # 检索未命中时，必须在正文里明确告知用户「这不是知识库内容」。
            # 仅靠前端徽标不够——复制粘贴或截屏分享时徽标会丢失，
            # 而用户可能正是拿这段文字去做判断。
            if source in UNGROUNDED_SOURCES and source == ANSWER_NOT_RETRIEVED:
                text = NOT_RETRIEVED_NOTICE + text
                trace.append("Finalize 标注：未基于知识库（检索未命中）")
        else:
            # 连续多轮无法给出有依据的答案：诚实拒答，而不是把编造内容推给用户
            verdict = Verdict(
                passed=False,
                reason=state.get("verdict_reason", "") or "现有资料不足以支撑一个可靠的回答。",
            )
            text = VerifierAgent.build_refusal(verdict)
            source = ANSWER_REFUSED
            trace = [f"Finalize 经 {MAX_REVISIONS} 轮审查仍未通过，转为诚实拒答"]
            trace.append("Finalize 标注：无法回答（审查未通过）")

        return {"agent_trace": trace, "response_text": text, "answer_source": source}

    # ------------------------------------------------------------------ #
    # 草稿生成
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

    async def _generate_draft(
        self, message: str, history: str, context: str, feedback: str = ""
    ) -> str:
        """基于上下文生成草稿（非流式，供审查）。feedback 非空时表示本轮是修订。"""
        system = SYSTEM_PROMPT
        if feedback:
            system += (
                "\n\n【上一轮审查未通过，请严格按以下意见修正】\n"
                f"{feedback}\n"
                "只使用参考资料中确实存在的信息；资料不足时如实说明，不要编造。\n"
            )
        if context:
            system += f"\n以下是内部参考资料，仅供你组织答案，不要提及来源：\n{context}\n"

        messages: List[Any] = [SystemMessage(content=system)]
        if history:
            messages.append(HumanMessage(content=f"以下是此前的对话记录，供你参考上下文：\n{history}"))
        messages.append(HumanMessage(content=message))
        return await self._generate_draft_from_messages(messages)

    async def _generate_draft_from_messages(self, messages: List[Any]) -> str:
        """非流式生成完整草稿。异常时返回空串，由审查 Agent 判定为不通过。"""
        try:
            result = await self.llm.ainvoke(messages)
            return self._chunk_text(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("草稿生成失败: %s", exc, exc_info=True)
            return ""

    @staticmethod
    def _chunk_text(chunk: Any) -> str:
        """从 LangChain 消息中稳健地取出文本（content 可能是字符串或分块列表）。"""
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

    async def _run_graph(self, message: str, user_id: str, history_records, db: Any = None) -> RAGState:
        if db is not None:
            set_tool_context(db, user_id)
        try:
            return await self.graph.ainvoke({
                "messages": message,
                "history": self._build_history(history_records),
                "user_id": user_id,
                "next_worker": "",
                "draft": "",
                "evidence": "",
                "revision_feedback": "",
                "revision_count": 0,
                "review_history": [],
                "agent_trace": [],
                "tool_calls": [],
                "answer_source": ANSWER_NO_RETRIEVAL,
            })
        finally:
            clear_tool_context()

    async def run(self, message: str, user_id: str, history_records=None, db: Any = None) -> str:
        """非流式：返回通过审查的最终答案（供 /chat 同步接口使用）。"""
        state = await self._run_graph(message, user_id, history_records, db=db)
        return state.get("response_text") or "抱歉，我无法回答这个问题。"

    async def stream_with_trace(
        self, message: str, user_id: str, history_records=None, db: Any = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式接口：依次下发 trace / token / done 事件。

        事件类型：
          {"type": "trace", "content": [...], "tool_calls": [...],
           "answer_source": "...", "source_label": "..."}   执行轨迹、审查结论与答案来源
          {"type": "token", "content": "增量文本"}            增量输出
          {"type": "done"}                                    结束标记

        `answer_source` 用于让前端明确展示「本回答是否基于知识库」，
        取值见 ANSWER_* 常量；`source_label` 是可直接显示的中文标签。
        """
        try:
            state = await self._run_graph(message, user_id, history_records, db=db)
            source = state.get("answer_source", ANSWER_NO_RETRIEVAL)
            yield {
                "type": "trace",
                "content": list(state.get("agent_trace") or []),
                "tool_calls": state.get("tool_calls") or [],
                "answer_source": source,
                "source_label": SOURCE_LABELS.get(source, ""),
            }

            text = state.get("response_text") or ""
            if not text:
                yield {"type": "token", "content": "抱歉，我无法回答这个问题。"}
            else:
                for chunk in self._iter_chunks(text):
                    yield {"type": "token", "content": chunk}

            yield {"type": "done"}
        except Exception as exc:  # noqa: BLE001
            logger.error("流式生成失败: %s", exc, exc_info=True)
            yield {"type": "error", "content": f"生成失败：{exc}"}
            yield {"type": "done"}

    @staticmethod
    def _iter_chunks(text: str, size: int = 2) -> List[str]:
        """
        把定稿文本切成小片下发，形成打字机节奏。

        关于取舍（面试可主动说明）：本项目为了保证审查能拦住**整段**内容，
        采用「先完整生成 → 审查 → 再下发」的顺序，因此这里的分片只影响前端节奏，
        并不降低首字延迟。**全文审查与真流式在单次请求内不可兼得**——
        本项目的选择是**可靠性优先**。若两者都要，可改为分段审查
        （生成一段→审查一段→通过即下发），代价是无法拦截跨段落的不一致。
        """
        return [text[i:i + size] for i in range(0, len(text), size)]
