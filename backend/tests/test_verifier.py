"""
审查 Agent（Verifier）与多 Agent 协作环路的单元测试。

这些用例均不依赖真实 LLM：
· 审查结果的 JSON 解析、容错与放行策略，用构造的字符串直接验证
· 图结构与路由决策，用构造的 state 直接验证
这样在 CI 里无需任何外部密钥即可运行。
"""

import pytest

from app.agents.supervisor import MAX_REVISIONS, SupervisorAgent
from app.agents.verifier import Verdict, VerifierAgent


# ---------------------------------------------------------------------- #
# 审查结果解析
# ---------------------------------------------------------------------- #
def test_parse_pass():
    raw = '{"pass": true, "reason": "答案有依据", "unsupported_claims": [], "feedback": ""}'
    verdict = VerifierAgent._parse(raw)
    assert verdict.passed is True
    assert verdict.label == "通过"
    assert verdict.unsupported_claims == []
    assert verdict.parse_failed is False


def test_parse_fail_with_claims_and_feedback():
    raw = (
        '{"pass": false, "reason": "存在无依据数字", '
        '"unsupported_claims": ["知识库中有 3 份文档"], '
        '"feedback": "请删除文档数量，或先调用统计工具确认"}'
    )
    verdict = VerifierAgent._parse(raw)
    assert verdict.passed is False
    assert verdict.label == "不通过"
    assert verdict.unsupported_claims == ["知识库中有 3 份文档"]
    assert "统计工具" in verdict.feedback


def test_parse_tolerates_markdown_fence():
    """模型可能仍用 ```json 包裹，解析必须容错。"""
    raw = '```json\n{"pass": false, "reason": "答非所问", "feedback": "请正面回答问题"}\n```'
    verdict = VerifierAgent._parse(raw)
    assert verdict.passed is False
    assert verdict.reason == "答非所问"


def test_parse_tolerates_surrounding_text():
    """模型有时会在 JSON 前后加解释文字。"""
    raw = '好的，我的判定如下：\n{"pass": true, "reason": "OK"}\n以上。'
    verdict = VerifierAgent._parse(raw)
    assert verdict.passed is True
    assert verdict.reason == "OK"


def test_parse_failure_fails_open_but_marks():
    """
    解析失败时放行（fail-open），避免审查环节自身出错导致用户拿不到回答；
    但必须显式标记 parse_failed，避免问题被隐藏。
    """
    for raw in ["完全不是 JSON", "", "   ", "{不是合法JSON}"]:
        verdict = VerifierAgent._parse(raw)
        assert verdict.passed is True, raw
        assert verdict.parse_failed is True, raw
        assert verdict.label == "解析失败（放行）"


def test_parse_handles_unsupported_claims_not_list():
    """unsupported_claims 可能是字符串而非列表，需要归一化。"""
    verdict = VerifierAgent._parse('{"pass": false, "unsupported_claims": "有编造内容"}')
    assert verdict.passed is False
    assert verdict.unsupported_claims == ["有编造内容"]


def test_parse_defaults_pass_true_when_field_missing():
    """字段缺失时默认放行，避免因格式不标准而误拦正确答案。"""
    verdict = VerifierAgent._parse('{"reason": "看起来没问题"}')
    assert verdict.passed is True


# ---------------------------------------------------------------------- #
# 拒答话术
# ---------------------------------------------------------------------- #
def test_build_refusal_contains_reason_and_is_not_an_answer():
    verdict = Verdict(passed=False, reason="参考资料中没有相关信息")
    text = VerifierAgent.build_refusal(verdict)
    assert "无法可靠地回答" in text
    assert "参考资料中没有相关信息" in text
    assert "补充" in text


def test_build_refusal_has_fallback_when_reason_empty():
    text = VerifierAgent.build_refusal(Verdict(passed=False))
    assert "无法可靠地回答" in text
    assert len(text) > 20


# ---------------------------------------------------------------------- #
# 审查 Agent 的放行分支（无 API Key 时不假装审查过）
# ---------------------------------------------------------------------- #
async def test_verifier_skips_without_api_key(monkeypatch):
    from app.agents import verifier as verifier_module

    monkeypatch.setattr(verifier_module.settings, "DEEPSEEK_API_KEY", None)
    agent = VerifierAgent()
    verdict = await agent.review("问题", "草稿", "证据")
    assert verdict.passed is True
    assert "未配置" in verdict.reason


async def test_verifier_rejects_empty_draft(monkeypatch):
    from app.agents import verifier as verifier_module

    monkeypatch.setattr(verifier_module.settings, "DEEPSEEK_API_KEY", "test-key")
    agent = VerifierAgent()
    verdict = await agent.review("问题", "   ", "证据")
    assert verdict.passed is False
    assert "空" in verdict.reason


# ---------------------------------------------------------------------- #
# 图结构：必须存在协作回路（这是"真多 Agent"的结构证据）
# ---------------------------------------------------------------------- #
def test_graph_contains_verifier_and_revise():
    agent = SupervisorAgent()
    nodes = set(agent.graph.get_graph().nodes)
    assert {"supervisor", "rag_worker", "tool_worker", "direct_llm",
            "verifier", "revise", "finalize"} <= nodes


def test_graph_has_cycle_back_to_workers():
    """
    关键断言：worker → verifier → revise → worker 必须构成回路。
    如果没有回边，就退化成了「单跳路由」，而不是多 Agent 协作。
    """
    agent = SupervisorAgent()
    edges = {(e.source, e.target) for e in agent.graph.get_graph().edges}

    # 三个 Worker 都汇入 verifier
    for worker in ("rag_worker", "tool_worker", "direct_llm"):
        assert (worker, "verifier") in edges, f"{worker} 未接入 verifier"

    # verifier 能走到 revise，revise 能回到 Worker —— 构成反馈回路
    assert ("verifier", "revise") in edges
    for worker in ("rag_worker", "tool_worker", "direct_llm"):
        assert ("revise", worker) in edges, f"revise 无法回到 {worker}"

    # 通过审查后必须能到 finalize 并结束
    assert ("verifier", "finalize") in edges
    assert ("finalize", "__end__") in edges


# ---------------------------------------------------------------------- #
# 路由决策
# ---------------------------------------------------------------------- #
def test_route_after_supervisor():
    agent = SupervisorAgent()
    for worker in ("rag_worker", "tool_worker", "direct_llm"):
        assert agent._route_after_supervisor({"next_worker": worker}) == worker
    assert agent._route_after_supervisor({}) == "direct_llm"


def test_route_to_worker_sends_back_to_same_branch():
    """修订必须回到原来那条分支，而不是重新分类。"""
    agent = SupervisorAgent()
    for worker in ("rag_worker", "tool_worker", "direct_llm"):
        assert agent._route_to_worker({"next_worker": worker}) == worker
    assert agent._route_to_worker({}) == "direct_llm"


def test_route_after_verifier_passed_goes_to_finalize():
    agent = SupervisorAgent()
    assert agent._route_after_verifier({"verdict_passed": True}) == "finalize"


def test_route_after_verifier_failed_requests_revision():
    agent = SupervisorAgent()
    state = {"verdict_passed": False, "revision_count": 0}
    assert agent._route_after_verifier(state) == "revise"


def test_route_after_verifier_gives_up_at_limit():
    """达到轮次上限后不再重试，转为诚实拒答（走 finalize 输出拒答话术）。"""
    agent = SupervisorAgent()
    state = {"verdict_passed": False, "revision_count": MAX_REVISIONS - 1}
    assert agent._route_after_verifier(state) == "finalize"


async def test_revise_node_increments_count():
    agent = SupervisorAgent()
    result = await agent._revise_node({"revision_count": 1})
    assert result["revision_count"] == 2
    assert any("重新生成" in t for t in result["agent_trace"])


# ---------------------------------------------------------------------- #
# finalize：通过则输出草稿，未通过则诚实拒答
# ---------------------------------------------------------------------- #
async def test_finalize_outputs_draft_when_passed():
    agent = SupervisorAgent()
    result = await agent._finalize_node({"verdict_passed": True, "draft": "正确答案"})
    assert result["response_text"] == "正确答案"


async def test_finalize_refuses_when_exhausted():
    """
    这是本项目「不瞎说」的最终保证：
    审查始终不通过时，输出的必须是拒答话术，而不是那份可能编造的草稿。
    """
    agent = SupervisorAgent()
    result = await agent._finalize_node({
        "verdict_passed": False,
        "draft": "知识库中有 3 份文档（这是编造的）",
        "verdict_reason": "草稿中的文档数量在参考资料中找不到依据",
    })
    text = result["response_text"]
    assert "无法可靠地回答" in text
    assert "编造的" not in text           # 原草稿绝不能被输出
    assert "找不到依据" in text


def test_iter_chunks_reassembles_original_text():
    chunks = SupervisorAgent._iter_chunks("这是一段测试文本", size=2)
    assert "".join(chunks) == "这是一段测试文本"
    assert all(len(c) <= 2 for c in chunks)


# ---------------------------------------------------------------------- #
# 文本提取
# ---------------------------------------------------------------------- #
def test_chunk_text_handles_str_and_list():
    class C:
        def __init__(self, content):
            self.content = content

    assert SupervisorAgent._chunk_text(C("hello")) == "hello"
    assert SupervisorAgent._chunk_text(C([{"type": "text", "text": "a"}, "b"])) == "ab"
    assert SupervisorAgent._chunk_text(C(None)) == ""


def test_build_context_keeps_filename():
    from langchain_core.documents import Document

    ctx = SupervisorAgent()._build_context([
        Document(page_content="内容A", metadata={"filename": "a.md"}),
        Document(page_content="内容B", metadata={}),
    ])
    assert "a.md" in ctx and "内容A" in ctx
    assert "未知文档" in ctx and "内容B" in ctx
