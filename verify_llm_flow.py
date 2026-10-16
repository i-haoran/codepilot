# -*- coding: utf-8 -*-
"""
真实 LLM 路径的端到端验证（需要有效的 DEEPSEEK_API_KEY 与本地 PostgreSQL）。

verify_project.py 覆盖的是**不依赖 LLM** 的结构与逻辑（38 项）；
本脚本补上「真的调用模型」这一层：
意图分类、草稿生成、审查判定、协作回路与最终交付。

用法：python verify_llm_flow.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

from app.core.config import settings  # noqa: E402

PG_URL = os.environ.get(
    "VERIFY_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/codepilot_verify",
)

OK, BAD = [], []


def report(name, ok, detail=""):
    (OK if ok else BAD).append(name)
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  -> " + detail) if detail else ""))


async def main():
    if not settings.DEEPSEEK_API_KEY:
        print("未配置 DEEPSEEK_API_KEY，跳过真实 LLM 验证")
        return 0

    print(f"模型: deepseek-chat   base_url={settings.DEEPSEEK_BASE_URL}")
    print(f"Key: {settings.DEEPSEEK_API_KEY[:6]}...({len(settings.DEEPSEEK_API_KEY)} 位)\n")

    # ------------------------------------------------------------------ #
    print("=== 1. 意图分类（Supervisor）===")
    from app.agents.supervisor import SupervisorAgent

    agent = SupervisorAgent()
    for question, expected in [
        ("知识库里有哪些文档？", "tool_worker"),
        ("CodePilot 的检索流程是怎样的？", "rag_worker"),
        ("用 Python 写一个快速排序", "direct_llm"),
    ]:
        state = await agent._supervisor_node({"messages": question, "history": ""})
        got = state.get("next_worker")
        report(f"「{question}」", got == expected, f"{got}（期望 {expected}）")

    # ------------------------------------------------------------------ #
    print("\n=== 2. 审查 Agent 的判定能力 ===")
    from app.agents.verifier import VerifierAgent

    verifier = VerifierAgent()

    v1 = await verifier.review(
        question="知识库里有多少份文档？",
        draft="知识库中一共有 3 份文档，分别是《架构设计》《API 规范》《部署手册》。",
        evidence="",
        route="tool_worker",
    )
    report("无证据却给出具体数字 → 判不通过", not v1.passed, f"{v1.label}：{v1.reason[:70]}")

    v2 = await verifier.review(
        question="知识库里有多少份文档？",
        draft="知识库中一共有 2 份文档，分别是 design.md 与 ops.md。",
        evidence="【工具 list_user_documents 返回】\n共 2 份文档：\n1. design.md（3 个分块）\n2. ops.md（5 个分块）",
        route="tool_worker",
    )
    report("有证据且一致 → 判通过", v2.passed, f"{v2.label}：{v2.reason[:70]}")

    v3 = await verifier.review(
        question="我们内部的支付网关密钥是多少？",
        draft="抱歉，知识库中没有关于支付网关密钥的信息，我无法回答这个问题。",
        evidence="【design.md】\n本文档介绍 CodePilot 的检索流程与分块策略。",
        route="rag_worker",
    )
    report("资料不足但诚实说明 → 判通过", v3.passed, f"{v3.label}：{v3.reason[:70]}")

    # ------------------------------------------------------------------ #
    print("\n=== 3. 协作回路：完整图执行（带真实数据库会话）===")
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.database import Base

    engine = create_async_engine(PG_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    question = "知识库里一共有多少份文档？请给出准确数字。"
    async with Session() as db:
        # 走 _run_graph：它会正确设置/清理工具上下文，与线上路径一致
        state = await agent._run_graph(question, "verify_llm_user", None, db=db)
    await engine.dispose()

    trace_lines = state.get("agent_trace", [])
    trace = " | ".join(trace_lines)

    print("  执行轨迹：")
    for line in trace_lines:
        print("    · " + line)
    print(f"  修订轮次：{state.get('revision_count', 0)}")
    print(f"  审查历史：{state.get('review_history', [])}")

    report("轨迹累加了多个节点（reducer 生效）", len(trace_lines) >= 3, f"共 {len(trace_lines)} 行")
    report("轨迹含意图分类", "Supervisor 意图分类" in trace)
    report("轨迹含审查结论", "Verifier" in trace)

    text = state.get("response_text", "")
    refused = "无法可靠地回答" in text
    report("产出了定稿内容", bool(text))
    report(
        "交付内容已通过审查或为诚实拒答",
        bool(text) and (state.get("verdict_passed", False) or refused),
        "诚实拒答" if refused else "通过审查",
    )

    print("\n  ---- 最终输出 ----")
    print("  " + text.replace("\n", "\n  ")[:600])

    print("\n" + "=" * 70)
    print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
    for name in BAD:
        print("  FAILED: " + name)
    print("=" * 70)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
