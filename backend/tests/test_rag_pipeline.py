"""
知识库 / 缓存 / 图路由的单元测试。

这些用例刻意**不依赖真实 LLM 与真实向量模型**：
意图路由用构造的 state 直接调用节点方法，
缓存用内存降级路径，分块用纯文本处理。
这样在 CI 里无需任何外部密钥即可运行。
"""

from langchain_core.documents import Document

from app.agents.supervisor import SupervisorAgent
from app.rag.processor import DocumentProcessor
from app.services.cache import CacheService


# ---------------------------------------------------------------------- #
# 图路由
# ---------------------------------------------------------------------- #
def test_route_after_supervisor():
    """条件路由应严格按 next_worker 分发。"""
    agent = SupervisorAgent()
    for worker in ("rag_worker", "tool_worker", "direct_llm"):
        assert agent._route_after_supervisor({"next_worker": worker}) == worker
    # 缺字段时兜底到 direct_llm，避免 KeyError 让请求 500
    assert agent._route_after_supervisor({}) == "direct_llm"


def test_graph_nodes_registered():
    """图应包含 4 个节点，避免误删节点导致路由目标不存在。"""
    agent = SupervisorAgent()
    nodes = set(agent.graph.get_graph().nodes)
    assert {"supervisor", "rag_worker", "tool_worker", "direct_llm"} <= nodes


def test_build_context_keeps_filename():
    """组装上下文时应带上来源文件名，便于生成回答时引用。"""
    agent = SupervisorAgent()
    docs = [
        Document(page_content="内容A", metadata={"filename": "a.md"}),
        Document(page_content="内容B", metadata={}),
    ]
    context = agent._build_context(docs)
    assert "a.md" in context and "内容A" in context
    assert "未知文档" in context and "内容B" in context


def test_chunk_text_handles_str_and_list():
    """流式分片的 content 可能是 str 或分块列表，两种都要能取到文本。"""

    class Chunk:
        def __init__(self, content):
            self.content = content

    assert SupervisorAgent._chunk_text(Chunk("hello")) == "hello"
    assert SupervisorAgent._chunk_text(Chunk([{"type": "text", "text": "a"}, "b"])) == "ab"
    assert SupervisorAgent._chunk_text(Chunk(None)) == ""


# ---------------------------------------------------------------------- #
# 文档分块
# ---------------------------------------------------------------------- #
def test_processor_chunks_and_metadata():
    processor = DocumentProcessor()
    text = "第一段内容。" * 200
    chunks = processor.process(text, metadata={"filename": "t.md", "user_id": "u1"})

    assert len(chunks) > 1, "长文本应被切成多块"
    for index, chunk in enumerate(chunks):
        assert chunk.metadata["filename"] == "t.md"
        assert chunk.metadata["user_id"] == "u1"
        assert chunk.metadata["chunk_index"] == index
        assert chunk.metadata["total_chunks"] == len(chunks)


def test_processor_cleans_whitespace():
    processor = DocumentProcessor()
    chunks = processor.process("a   b\n\n\n c", metadata={})
    assert "  " not in chunks[0].page_content


def test_processor_empty_text():
    processor = DocumentProcessor()
    assert processor.process("", metadata={}) == []


# ---------------------------------------------------------------------- #
# 缓存与降级
# ---------------------------------------------------------------------- #
async def test_cache_set_get_and_miss():
    cache = CacheService()
    await cache.clear()
    assert await cache.get("未缓存的问题") is None
    await cache.set("问题1", "答案1")
    assert await cache.get("问题1") == "答案1"


async def test_cache_key_is_exact_match_not_semantic():
    """
    明确当前缓存的语义边界：SHA-256 摘要做 key 属于**精确匹配**，
    措辞不同即视为不同问题——这是已知的设计取舍，不是语义缓存。
    """
    cache = CacheService()
    await cache.clear()
    await cache.set("如何配置 Redis", "答案")
    assert await cache.get("如何配置 Redis") == "答案"
    assert await cache.get("怎么配置 Redis") is None


async def test_cache_delete():
    cache = CacheService()
    await cache.clear()
    await cache.set("k", "v")
    await cache.delete("k")
    assert await cache.get("k") is None
