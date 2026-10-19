"""
切分质量验证：对真实文档跑一遍分块，输出结构体检指标。

替代以前手工敲命令的做法。检查项：
  · 行切断  —— 块内每一行是否都能在原文里原样找到（切坏会出现失配）
  · 残缺开头 —— 块首行是否以 . 、 ) 这类断口开头（字符切分的典型症状）
  · 标题识别 —— 每份文档是否都识别出了标题
  · 块长分布 —— min / avg / max

用法（需在容器内或装有项目依赖的环境里跑）：

    docker compose run --rm -v "<repo>/backend:/app" -v "<docs dir>:/docs:ro" \
        backend python /path/to/verify_split.py --dir /docs --list /app/../eval/tech_notes.txt
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.rag.splitter_factory import looks_structured, get_processor  # noqa: E402

ORPHAN = re.compile(r"^\s*[\.\u3001\)]")


def normalize(text: str) -> str:
    text = text.lstrip("\ufeff")
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--list", required=True)
    ap.add_argument("--strategy", default="structured", choices=["structured", "adaptive"])
    args = ap.parse_args()

    base = Path(args.dir)
    names = [ln.strip() for ln in Path(args.list).read_text(encoding="utf-8").splitlines() if ln.strip()]

    proc = get_processor() if args.strategy == "adaptive" else None
    if proc is None:
        from app.rag.structured_splitter import StructuredSplitter

        proc = StructuredSplitter()

    print(f"{'文件':<30}{'块数':>5}{'平均':>6}{'最长':>6}{'切断':>5}{'残缺':>5}  标题")
    print("-" * 76)

    total_chunks = 0
    total_broken = 0
    total_orphan = 0
    titles_ok = 0
    lengths = []

    for name in names:
        path = base / name
        if not path.is_file():
            print(f"{name:<30}  <缺失>")
            continue
        text = normalize(path.read_text(encoding="utf-8", errors="ignore"))
        docs = proc.split(text, {"source": name}) if args.strategy == "structured" else proc.process(text, {"source": name})

        broken = orphan = 0
        for doc in docs:
            body = doc.page_content
            if body.startswith("[") and "\n" in body:
                body = body.split("\n", 1)[1]
            for line in body.split("\n"):
                if line.strip() and line not in text:
                    broken += 1
            first = body.split("\n")[0] if body else ""
            if ORPHAN.match(first):
                orphan += 1

        lens = [len(d.page_content) for d in docs] or [0]
        lengths.extend(lens)
        title = docs[0].metadata.get("doc_title", "") if docs else ""
        if title:
            titles_ok += 1
        total_chunks += len(docs)
        total_broken += broken
        total_orphan += orphan

        print(f"{name:<30}{len(docs):>5}{sum(lens)//len(lens):>6}{max(lens):>6}{broken:>5}{orphan:>5}  {title[:24]}")

    print("-" * 76)
    print(f"文档数        : {len(names)}")
    print(f"总块数        : {total_chunks}")
    print(f"被切断的行    : {total_broken}   (应为 0)")
    print(f"残缺开头块    : {total_orphan}   (应为 0)")
    print(f"标题识别      : {titles_ok}/{len(names)}")
    print(f"块长 min/avg/max: {min(lengths)} / {sum(lengths)//len(lengths)} / {max(lengths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
