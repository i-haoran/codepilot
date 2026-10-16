# -*- coding: utf-8 -*-
"""
协作回路测试装置（不依赖 pytest，供 pytest 与独立验证脚本共用）。

设计目的：把 LLM 调用（意图分类 / 草稿生成 / 审查）替换为**可控桩函数**，
从而让「第几轮通过 / 一直不通过」成为可复现的输入，
而**真实的图结构、路由函数、判据与 finalize 逻辑全部保留**。

⚠️ 关键坑（本项目实测踩到）
--------------------------
替换必须发生在 **`SupervisorAgent()` 实例化之前**，且必须是**类级替换**：

1. `_build_graph()` 在 `__init__` 中编译图时，节点位置就**持有了方法对象的引用**。
   之后再改实例属性不会被图看到（实例属性遮蔽对已持有的引用无效）；
2. 即使改成类级替换，**已编译的图**仍然指向旧的函数对象。
   因此唯一的可靠做法是：**先替换类方法 → 再创建 agent（图用桩函数编译）→ 用完还原**。

这也是"运行期替换图节点行为"的真实约束：要么走工厂，要么改图本身。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agents.supervisor import MAX_REVISIONS, SupervisorAgent
from app.agents.verifier import Verdict


class LoopHarness:
    """可控的协作回路测试装置。"""

    def __init__(
        self,
        pass_on_attempt: int,
        drafts: Optional[List[str]] = None,
        route: str = "direct_llm",
    ) -> None:
        """
        :param pass_on_attempt: 第几轮审查返回「通过」；大于 MAX_REVISIONS 表示永不通过
        :param drafts: 每轮产出的草稿，按序取用（不足则复用最后一个）
        :param route: 固定路由到的分支名
        """
        self.pass_on_attempt = pass_on_attempt
        self.drafts = drafts or ["草稿A", "草稿B", "草稿C", "草稿D", "草稿E"]
        self.route = route

        self.review_calls: List[Dict[str, Any]] = []
        self.worker_calls: List[str] = []
        self.supervisor_calls = 0
        self._draft_index = 0
        self._saved: Dict[str, Any] = {}
        self.agent: Optional[SupervisorAgent] = None

    # ---------------------------------------------------------------- #
    # 桩函数
    # ---------------------------------------------------------------- #
    async def fake_supervisor_node(self, state) -> Dict[str, Any]:
        self.supervisor_calls += 1
        intent = {"rag_worker": "rag", "tool_worker": "tool", "direct_llm": "direct"}.get(
            self.route, "direct"
        )
        return {
            "next_worker": self.route,
            "agent_trace": [f"Supervisor 意图分类 → {intent}"],
            "revision_count": 0,
            "review_history": [],
        }

    async def fake_generate_draft(self, message, history, context, feedback="") -> str:
        self.worker_calls.append(feedback)
        draft = self.drafts[min(self._draft_index, len(self.drafts) - 1)]
        self._draft_index += 1
        return draft

    async def fake_review(self, question, draft, evidence, route="", attempt=1) -> Verdict:
        self.review_calls.append({"draft": draft, "attempt": attempt, "route": route})
        passed = attempt >= self.pass_on_attempt
        return Verdict(
            passed=passed,
            reason="证据充分，可以交付" if passed else f"第 {attempt} 轮：草稿含无依据的具体数字",
            unsupported_claims=[] if passed else ["知识库中有 3 份文档"],
            feedback="" if passed else f"请删除编造的数字（第 {attempt} 次要求）",
        )

    # ---------------------------------------------------------------- #
    # 装配
    # ---------------------------------------------------------------- #
    def build_agent(self) -> SupervisorAgent:
        """
        先替换类方法，再创建 agent —— 这样图会用桩函数编译。

        ⚠️ 还原时机很关键：必须在**整次图执行结束后**才还原，不能在这里就还原。
        因为：
        · `_supervisor_node` 等节点方法在编译期被图**持有引用** → 还原不影响图；
        · 但 `_generate_draft` 是 Worker 在运行期通过 `self.` 查找的 →
          一旦提前还原类属性，Worker 就会调回真实 LLM（本项目实测踩到：
          表现为「Worker 生成 0 次」且输出变成真实模型的回答）。
        """
        cls = SupervisorAgent
        self._saved = {
            "_supervisor_node": cls._supervisor_node,
            "_generate_draft": cls._generate_draft,
        }
        cls._supervisor_node = self.fake_supervisor_node
        cls._generate_draft = self.fake_generate_draft

        self.agent = SupervisorAgent()

        # verifier 是普通实例属性，运行期替换即可被 _verifier_node 读到
        self._saved_review = self.agent.verifier.review
        self.agent.verifier.review = self.fake_review
        return self.agent

    def restore(self) -> None:
        """还原被替换的类方法与实例方法（应在图执行完毕后再调用）。"""
        cls = SupervisorAgent
        for name, fn in (getattr(self, "_saved", None) or {}).items():
            setattr(cls, name, fn)
        self._saved = {}
        if self.agent is not None and getattr(self, "_saved_review", None):
            self.agent.verifier.review = self._saved_review
            self._saved_review = None

    def __enter__(self) -> "LoopHarness":
        return self

    def __exit__(self, *exc) -> None:
        self.restore()


async def run_loop(agent: SupervisorAgent, question: str = "知识库里有多少份文档？") -> Dict[str, Any]:
    """以标准初始状态执行一次完整图，返回最终 state。"""
    return await agent.graph.ainvoke({
        "messages": question,
        "history": "",
        "user_id": "loop_test",
        "next_worker": "",
        "draft": "",
        "evidence": "",
        "revision_feedback": "",
        "revision_count": 0,
        "agent_trace": [],
        "review_history": [],
    })


async def run_scenario(pass_on_attempt: int, drafts: Optional[List[str]] = None):
    """一步到位：装配 → 执行 → 还原。返回 (harness, state)。"""
    harness = LoopHarness(pass_on_attempt=pass_on_attempt, drafts=drafts)
    agent = harness.build_agent()
    try:
        state = await run_loop(agent)
    finally:
        harness.restore()
    return harness, state


__all__ = ["LoopHarness", "run_loop", "run_scenario", "MAX_REVISIONS"]
