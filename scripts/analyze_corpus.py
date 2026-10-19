"""
语料结构分析：一次算出切分策略需要的全部指标，替代手工统计。

用途
----
设计切分策略前，需要知道语料的真实形态：行长分布、有没有超过目标块长的行、
有多少章节级标记。以前这些是手工敲命令数的，换一份语料就得重来。

用法
----
    # 统计目录下所有 txt
    python scripts/analyze_corpus.py --dir E:\\桌面

    # 只统计清单里列出的文件（清单里写文件名，相对 --dir）
    python scripts/analyze_corpus.py --dir E:\\桌面 --list eval/tech_notes.txt

    # 顺便导出 JSON，便于写进文档或做前后对比
    python scripts/analyze_corpus.py --dir E:\\桌面 --list eval/tech_notes.txt --json eval/corpus_stats.json

只依赖标准库，宿主机没有项目依赖也能跑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, List

# 与 app/rag/structured_splitter.py 保持一致的标记定义
SECTIONS = ("\u3010",)          # 【
RULE_CHARS = ("=", "-")


def read_lines(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8", errors="ignore").lstrip("\ufeff")
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def is_rule(line: str) -> bool:
    s = line.strip()
    return len(s) >= 5 and set(s) <= set(RULE_CHARS)


def is_chapter(line: str) -> bool:
    s = line.strip()
    return s.startswith(SECTIONS[0]) and s.endswith("\u300d") is False and s.endswith("\u3011")


def is_subsection(line: str) -> bool:
    s = line.strip()
    return len(s) >= 6 and s.startswith("-") and s.endswith("-") and s.count("-") >= 6


def analyze(files: List[Path]) -> Dict:
    per_file = []
    all_lens: List[int] = []
    total_nonempty = 0
    chapters = subsections = rules = 0

    for path in files:
        lines = read_lines(path)
        nonempty = [line for line in lines if line.strip()]
        total_nonempty += len(nonempty)

        f_chap = f_sub = f_rule = 0
        for line in nonempty:
            if is_chapter(line):
                f_chap += 1
            elif is_subsection(line):
                f_sub += 1
            elif is_rule(line):
                f_rule += 1
            all_lens.append(len(line))

        chapters += f_chap
        subsections += f_sub
        rules += f_rule
        per_file.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "lines": len(lines),
                "nonempty": len(nonempty),
                "chapters": f_chap,
                "subsections": f_sub,
                "rules": f_rule,
                "max_line": max((len(x) for x in nonempty), default=0),
                "structured": (f_chap + f_sub + f_rule) >= 2,
            }
        )

    lens_sorted = sorted(all_lens)
    n = len(lens_sorted)

    def pct(p: float) -> int:
        if not n:
            return 0
        return lens_sorted[min(n - 1, int(n * p))]

    return {
        "documents": len(files),
        "total_bytes": sum(f["bytes"] for f in per_file),
        "total_nonempty_lines": total_nonempty,
        "line_length": {
            "p50": pct(0.50),
            "p90": pct(0.90),
            "p99": pct(0.99),
            "max": lens_sorted[-1] if n else 0,
            "over_512": sum(1 for x in lens_sorted if x > 512),
            "over_200": sum(1 for x in lens_sorted if x > 200),
        },
        "markers": {
            "chapter": chapters,       # 【一、xxx】
            "subsection": subsections, # ---- xxx ----
            "rule": rules,             # ===== / -----
        },
        "structured_documents": sum(1 for f in per_file if f["structured"]),
        "per_file": per_file,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="语料结构分析")
    ap.add_argument("--dir", required=True, help="文档目录")
    ap.add_argument("--pattern", default="*.txt", help="文件匹配，默认 *.txt")
    ap.add_argument("--list", help="文件名清单（每行一个，相对 --dir）")
    ap.add_argument("--json", help="把结果写成 JSON")
    args = ap.parse_args()

    base = Path(args.dir)
    if not base.is_dir():
        print(f"目录不存在: {base}", file=sys.stderr)
        return 2

    if args.list:
        wanted = [ln.strip() for ln in Path(args.list).read_text(encoding="utf-8").splitlines() if ln.strip()]
        files = [base / name for name in wanted]
        missing = [str(p) for p in files if not p.is_file()]
        for m in missing:
            print(f"清单中的文件不存在: {m}", file=sys.stderr)
        files = [p for p in files if p.is_file()]
    else:
        files = sorted(base.glob(args.pattern))

    if not files:
        print("没有可分析的文档", file=sys.stderr)
        return 1

    stats = analyze(files)

    print(f"{'文件':<30}{'行数':>6}{'非空':>6}{'章':>4}{'节':>4}{'最长行':>7}")
    print("-" * 60)
    for f in stats["per_file"]:
        print(f"{f['file']:<30}{f['lines']:>6}{f['nonempty']:>6}"
              f"{f['chapters']:>4}{f['subsections']:>4}{f['max_line']:>7}")
    print("-" * 60)

    ll = stats["line_length"]
    mk = stats["markers"]
    print(f"文档数            : {stats['documents']}（其中结构化 {stats['structured_documents']}）")
    print(f"总大小            : {stats['total_bytes'] / 1024:.1f} KB")
    print(f"非空行合计        : {stats['total_nonempty_lines']}")
    print(f"行长 P50/P90/P99  : {ll['p50']} / {ll['p90']} / {ll['p99']}")
    print(f"最长行            : {ll['max']}")
    print(f"超过 512 字符的行 : {ll['over_512']}")
    print(f"章级【】标记      : {mk['chapter']}")
    print(f"节级 ---- 标记    : {mk['subsection']}")
    print(f"分隔线 ====/----  : {mk['rule']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
