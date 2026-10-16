"""
审查 Agent（Verifier）。

为什么需要它
-----------
生成 Agent 存在两类无法自我发现的问题：

1. **无依据断言**：知识库检索为空、或工具没返回数据时，模型仍会编造具体数字与结论
   （例如明明没查数据库，却回答「知识库中有 3 份文档」）。
   这类错误**不会报异常**，日志里一切正常，只有比对真实数据才能发现。

2. **答非所问**：回答通顺但与用户问题无关，或只答了问题的一部分。

同一个模型在**同一次生成**里无法可靠地审查自己——它已经沿着那条推理路径走完了。
因此这里把「生成」和「审查」拆成两次独立的 LLM 调用，由审查 Agent 只看
**问题 + 证据 + 草稿**做判定，不参与生成过程。

协作闭环
-------
    worker → verifier ─通过→ finalize → END
                 │
                 └─不通过→（带反馈回到 worker 重新生成，最多 N 轮）
                              │
                              └─超过上限→ 诚实拒答，而不是继续编

这就是本项目里"真正的多 Agent 协作"：Agent 之间有**反馈回路**，
而不是各自跑完就结束的单跳路由。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

VERIFIER_SYSTEM_PROMPT = """你是 CodePilot 的答案审查员。你的唯一职责是审查另一个 AI 生成的草稿答案，\
判断它是否可以交付给用户。你不负责重写答案，只负责判定与给出可执行的修改意见。

判定原则（按重要性排序）：

1. **有依据性（最重要）**
   - 草稿中的每一个**具体事实**（数字、文件名、配置项、API 名、结论）都必须能在【参考资料】中找到出处。
   - 如果【参考资料】为空或不含所需信息，而草稿仍然给出了具体答案 → 判定为 fail。
   - 允许合理的语言组织与概括，但不允许**新增**参考资料中没有的事实。
   - 特别警惕这类编造：文档数量、统计数字、不存在的文件名、未在资料中出现的接口或参数。

2. **答到点上**
   - 草稿必须正面回答【用户问题】。
   - 如果问题包含多个部分而草稿只回答了一部分 → 判定为 fail。

3. **不越权**
   - 如果参考资料不足以回答，正确做法是明确说明"资料中没有相关信息"，
     这种情况下判定为 pass（诚实拒答是合格答案）。
   - 但如果草稿用编造内容掩盖了资料不足 → 判定为 fail。

只输出一个 JSON 对象，不要输出任何其他文字、不要用 Markdown 代码块包裹：

{
  "pass": true 或 false,
  "reason": "一句话说明判定理由",
  "unsupported_claims": ["草稿中找不到依据的具体断言", "..."],
  "feedback": "如果 pass 为 false，给出具体、可执行的修改要求；pass 为 true 时留空字符串"
}"""

VERIFIER_USER_TEMPLATE = """【用户问题】
{question}

【参考资料】
{evidence}

【待审查草稿】
{draft}

请审查并输出 JSON 判定结果。"""

# 无法通过审查时的兜底话术：诚实说明，而不是继续编
REFUSAL_TEMPLATE = (
    "抱歉，我无法可靠地回答这个问题。\n\n"
    "原因：{reason}\n\n"
    "建议您在知识库中补充相关文档后重试，或换一种更具体的问法。"
)


@dataclass
class Verdict:
    """审查结论。"""

    passed: bool
    reason: str = ""
    unsupported_claims: List[str] = field(default_factory=list)
    feedback: str = ""
    raw: str = ""
    parse_failed: bool = False

    @property
    def label(self) -> str:
        if self.parse_failed:
            return "解析失败（放行）"
        return "通过" if self.passed else "不通过"


class VerifierAgent:
    """
    审查 Agent：独立于生成 Agent 的第二次 LLM 调用，只做判定。

    使用 temperature=0 保证同一草稿的判定稳定；max_tokens 给足以便输出结构化 JSON。
    """

    def __init__(self, llm: Optional[ChatOpenAI] = None) -> None:
        self.llm = llm or ChatOpenAI(
            model="deepseek-chat",
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0,
            max_tokens=500,
            streaming=False,
        )

    # ------------------------------------------------------------------ #
    async def review(
        self,
        question: str,
        draft: str,
        evidence: str,
        route: str = "",
        attempt: int = 1,
    ) -> Verdict:
        """
        审查草稿。

        :param question: 用户原始问题
        :param draft:    待审查的草稿答案
        :param evidence: 该分支掌握的参考资料（检索片段 / 工具结果 / 空）
        :param route:    分支名，仅用于日志与轨迹
        :param attempt:  第几次尝试，仅用于日志与轨迹
        """
        if not settings.DEEPSEEK_API_KEY:
            # 没有 Key 就无法审查；放行但不假装审查过
            return Verdict(passed=True, reason="未配置 API Key，跳过审查", parse_failed=False)

        if not draft.strip():
            return Verdict(passed=False, reason="草稿为空", feedback="请重新生成一个完整回答。")

        prompt = VERIFIER_USER_TEMPLATE.format(
            question=question,
            evidence=evidence.strip() or "（无参考资料）",
            draft=draft,
        )

        try:
            result = await self.llm.ainvoke([
                SystemMessage(content=VERIFIER_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            raw = (result.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("审查调用失败，放行草稿: %s", exc, exc_info=True)
            return Verdict(passed=True, reason=f"审查调用异常（{type(exc).__name__}），放行", raw="")

        return self._parse(raw)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(raw: str) -> Verdict:
        """
        解析审查结果。

        设计取舍：**解析失败时放行（fail-open）**。
        理由是审查是增强环节，不应因为它自身出错就让用户拿不到任何回答；
        但会在 trace 与日志里明确标注「解析失败（放行）」，避免问题被隐藏。
        """
        text = raw.strip()
        # 容错：模型可能仍用 ```json 包裹
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("审查结果无法解析为 JSON，放行草稿: %s", raw[:200])
            return Verdict(passed=True, reason="审查结果解析失败，放行", raw=raw, parse_failed=True)

        try:
            data: Dict[str, Any] = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            logger.warning("审查结果 JSON 解析失败，放行草稿: %s", exc)
            return Verdict(passed=True, reason="审查结果解析失败，放行", raw=raw, parse_failed=True)

        passed = bool(data.get("pass", True))
        claims = data.get("unsupported_claims") or []
        if not isinstance(claims, list):
            claims = [str(claims)]

        return Verdict(
            passed=passed,
            reason=str(data.get("reason", "")).strip(),
            unsupported_claims=[str(c) for c in claims if str(c).strip()],
            feedback=str(data.get("feedback", "")).strip(),
            raw=raw,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def build_refusal(verdict: Verdict) -> str:
        """审查不通过且达到上限时，返回诚实拒答而不是继续编造。"""
        reason = verdict.reason or "现有资料不足以支撑一个可靠的回答。"
        return REFUSAL_TEMPLATE.format(reason=reason)
