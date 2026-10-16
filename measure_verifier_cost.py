# -*- coding: utf-8 -*-
"""测量加入审查 Agent 后新增的延迟开销（需要真实 LLM）。"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.agents.supervisor import SupervisorAgent  # noqa: E402
from app.core.database import Base  # noqa: E402

PG = os.environ.get(
    "VERIFY_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/codepilot_verify",
)


async def main():
    eng = create_async_engine(PG)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    agent = SupervisorAgent()
    q = "CodePilot 的检索流程是怎样的？"
    evidence = "【design.md】\nCodePilot 采用两阶段检索：向量召回 Top-10，再用 Cross-Encoder 重排。" * 10

    t0 = time.perf_counter()
    await agent._supervisor_node({"messages": q, "history": ""})
    t_cls = time.perf_counter() - t0

    t0 = time.perf_counter()
    draft = await agent._generate_draft(q, "", evidence, "")
    t_gen = time.perf_counter() - t0

    t0 = time.perf_counter()
    v = await agent.verifier.review(q, draft, evidence, "rag_worker", 1)
    t_ver = time.perf_counter() - t0

    print("意图分类          : %.2fs" % t_cls)
    print("草稿生成          : %.2fs" % t_gen)
    print("审查调用          : %.2fs   (%s)" % (t_ver, v.label))
    print("-" * 46)
    print("改造前首字延迟    : ~%.2fs" % (t_cls + t_gen))
    print("改造后首字延迟    : ~%.2fs" % (t_cls + t_gen + t_ver))
    print("审查新增开销      : +%.2fs  (+%.0f%%)" % (t_ver, t_ver / (t_cls + t_gen) * 100))

    await eng.dispose()


if __name__ == "__main__":
    asyncio.run(main())
