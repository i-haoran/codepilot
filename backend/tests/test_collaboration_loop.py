"""
协作回路的端到端测试（不依赖真实 LLM）。

`test_verifier.py` 覆盖路由函数与审查结果解析；
这里补上**整条回路的确定性验证**——通过注入可控的审查 Agent，
让「第几轮通过 / 一直不通过」成为可复现的输入。

装置定义在 `loop_harness.py`（与 verify_project.py 共用，避免重复维护）。

覆盖：
1. 首轮通过时不触发多余重做
2. 审查不通过确实回到 Worker 重新生成（回路的边真的被走过）
3. 每轮重做都带上上一轮的审查意见（反馈真的回传）
4. 轮次上限生效，不会无限循环
5. 达上限仍不通过 → 诚实拒答，且**原草稿绝不泄漏**
6. agent_trace 按执行顺序累加（reducer 生效）
7. 审查历史记录每轮结论
8. 修订回到原分支，不重新分类
"""

import pytest

from app.agents.supervisor import MAX_REVISIONS
from tests.loop_harness import run_scenario


async def test_pass_on_first_attempt_does_not_retry():
    harness, state = await run_scenario(pass_on_attempt=1)

    assert state["verdict_passed"] is True
    assert state["response_text"] == "草稿A"
    assert len(harness.review_calls) == 1, "首轮通过却审查了多次"
    assert len(harness.worker_calls) == 1, "首轮通过却重新生成了"
    assert state.get("revision_count", 0) == 0


async def test_loop_revises_until_pass():
    harness, state = await run_scenario(pass_on_attempt=2)

    assert state["verdict_passed"] is True
    assert state["response_text"] == "草稿B", "交付的应是第 2 轮修订后的草稿"
    assert len(harness.review_calls) == 2
    assert len(harness.worker_calls) == 2, "回路未把控制流送回 Worker"
    assert state["revision_count"] == 1


async def test_feedback_is_passed_back_to_worker():
    harness, state = await run_scenario(pass_on_attempt=3)

    assert len(harness.worker_calls) == 3
    assert harness.worker_calls[0] == "", "首轮不应携带审查意见"
    assert "第 1 次要求" in harness.worker_calls[1], "第 2 次生成未收到第 1 轮反馈"
    assert "第 2 次要求" in harness.worker_calls[2], "第 3 次生成未收到第 2 轮反馈"
    assert state["verdict_passed"] is True
    assert state["response_text"] == "草稿C"


async def test_stops_at_max_revisions():
    harness, state = await run_scenario(
        pass_on_attempt=99,
        drafts=["草稿1", "草稿2", "草稿3", "草稿4", "草稿5"],
    )

    assert len(harness.review_calls) == MAX_REVISIONS, \
        f"审查次数应为上限 {MAX_REVISIONS}，实际 {len(harness.review_calls)}"
    assert len(harness.worker_calls) == MAX_REVISIONS
    assert state["revision_count"] == MAX_REVISIONS - 1
    assert state["verdict_passed"] is False


async def test_exhausted_loop_refuses_and_never_leaks_draft():
    """★ 核心保证：不通过就不交付。"""
    fabricated = "知识库中有 3 份文档（这是编造的）"
    _, state = await run_scenario(pass_on_attempt=99, drafts=[fabricated] * 5)
    text = state["response_text"]

    assert "无法可靠地回答" in text, "未输出拒答话术"
    assert "3 份文档" not in text, "★ 编造内容被泄漏给用户"
    assert "编造的" not in text, "★ 原草稿内容出现在输出中"
    assert "无依据" in text or "依据" in text, "未说明拒答原因"


async def test_trace_accumulates_across_iterations():
    _, state = await run_scenario(pass_on_attempt=3)
    trace = state["agent_trace"]

    assert any("意图分类" in t for t in trace), "缺少分类节点轨迹"
    assert sum(1 for t in trace if "Verifier 审查" in t) == 3, "审查轨迹数不符"
    assert any("重新生成" in t for t in trace), "缺少重做节点轨迹"
    assert any("Finalize" in t for t in trace), "缺少定稿节点轨迹"
    assert len(trace) >= 7, f"轨迹可能被覆盖（只有 {len(trace)} 行）"


async def test_review_history_records_each_attempt():
    _, state = await run_scenario(pass_on_attempt=3)
    history = state["review_history"]

    assert len(history) == 3, f"应有 3 条审查记录，实际 {len(history)}"
    assert "attempt=1 pass=False" in history[0]
    assert "attempt=2 pass=False" in history[1]
    assert "attempt=3 pass=True" in history[2]


async def test_revision_returns_to_same_branch():
    """修订必须回到原分支，不重新分类。"""
    harness, state = await run_scenario(pass_on_attempt=2)

    assert harness.supervisor_calls == 1, \
        f"重做时重复分类了（分类调用 {harness.supervisor_calls} 次）"
    assert state["next_worker"] == "direct_llm", "分支被改变"
    assert state["verdict_passed"] is True
