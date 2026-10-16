# -*- coding: utf-8 -*-
"""
评测 RAG 检索质量（真数据，用于简历与面试）。

方法：
1. 从知识库中取一份文档，按其分块内容人工/半自动构造「问题 → 应命中的分块」标注集；
2. 分别评测两个阶段：
     · 仅向量召回（Top-5）的 Hit@5
     · 向量召回 Top-10 + Cross-Encoder 重排 Top-5 的 Hit@5
3. 另外评测「相关性阈值过滤」的准确率（是否把无关查询正确识别为未命中）。

输出为可直接写进简历的实测数字。
用法：python eval_retrieval.py
"""
import asyncio
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))

from app.core.config import settings  # noqa: E402
from app.rag.reranker import Reranker  # noqa: E402
from app.rag.retriever import RAGRetriever  # noqa: E402

# 标注集：问题 → 该问题应命中的分块中包含的关键词（判定命中的依据）
# 取自 Git命令速查手册.txt 的实际内容，关键词均真实存在于该文档中
LABELED = [
    ("怎么查看哪些文件被修改了", ["查看状态", "git status", "等待提交"]),
    ("如何把指定分支合并到当前分支", ["合并分支", "git merge", "切回主分支"]),
    ("怎么撤销已经提交的改动", ["撤销", "reset", "回退"]),
    ("远程仓库怎么关联和推送", ["远程仓库", "git remote", "push"]),
    ("怎么创建并切换到新分支", ["创建分支", "branch", "切换"]),
    ("如何查看提交历史", ["提交历史", "git log", "日志"]),
    ("把改动暂时存起来", ["暂存", "stash"]),
    ("分支冲突怎么解决", ["冲突", "conflict"]),
    ("怎么删除一个分支", ["删除分支", "branch -d"]),
    ("给某次提交打标签", ["标签", "tag"]),
]

# 无关查询：知识库中确实没有对应内容
IRRELEVANT = [
    "什么是redis",
    "介绍下ai应用开发",
    "今天天气怎么样",
    "如何做红烧肉",
    "推荐几部电影",
    "Python 的装饰器怎么用",
]


def hit_at_k(results, keywords) -> bool:
    """Top-K 中是否存在包含任一关键词的分块。"""
    for doc in results:
        text = doc.page_content
        if any(k in text for k in keywords):
            return True
    return False


async def main():
    r = RAGRetriever()
    rk = Reranker()
    th = settings.RETRIEVE_MIN_SCORE

    print(f"知识库: {r.collection.name} | 分块总数 {r.count()}")
    print(f"embedding: {r.embedding_source} | 相关性阈值 {th}\n")

    if r.count() == 0:
        print("知识库为空，无法评测。请先上传文档或运行 reindex_documents.py")
        return

    # ---------- 1. 召回阶段 vs 重排阶段 ----------
    vector_hits = 0
    rerank_hits = 0
    for q, kws in LABELED:
        raw = r.search(q, top_k=10)
        # 仅向量：取前 5
        if hit_at_k(raw[:5], kws):
            vector_hits += 1
        # 重排后取 5
        rr = rk.rerank(q, raw, top_k=5)
        if hit_at_k(rr, kws):
            rerank_hits += 1

    n = len(LABELED)
    print("=== 检索命中率（标注集 %d 条）===" % n)
    print(f"  仅向量召回 Top-5        Hit@5 = {vector_hits}/{n} = {vector_hits/n:.0%}")
    print(f"  向量Top-10 + 重排 Top-5 Hit@5 = {rerank_hits}/{n} = {rerank_hits/n:.0%}")
    delta = rerank_hits - vector_hits
    print(f"  重排带来的变化: {delta:+d} 条 ({delta/n:+.0%})")

    # ---------- 2. 相关性阈值过滤的准确率 ----------
    tp = fn = 0   # 相关查询：正确保留 / 误判为未命中
    for q, _ in LABELED:
        raw = r.search(q, top_k=10)
        kept = [d for d in raw
                if isinstance(d.metadata.get("vector_score"), float)
                and d.metadata["vector_score"] >= th]
        if kept:
            tp += 1
        else:
            fn += 1

    fp = tn = 0   # 无关查询：误判为命中 / 正确识别为未命中
    for q in IRRELEVANT:
        raw = r.search(q, top_k=10)
        kept = [d for d in raw
                if isinstance(d.metadata.get("vector_score"), float)
                and d.metadata["vector_score"] >= th]
        if kept:
            fp += 1
        else:
            tn += 1

    total = tp + fn + fp + tn
    acc = (tp + tn) / total if total else 0
    precision = tp / (tp + fp) if (tp + fp) else 0
    print(f"\n=== 相关性阈值过滤（阈值 {th}，共 {total} 条查询）===")
    print(f"  相关查询正确保留 {tp}/{tp+fn}   无关查询正确过滤 {tn}/{tn+fp}")
    print(f"  判定准确率 {(tp+tn)}/{total} = {acc:.0%}")
    print(f"  精确率（判为命中的里真的相关的比例）= {precision:.0%}")


if __name__ == "__main__":
    asyncio.run(main())
