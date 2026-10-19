"""
分块器分发：按文档结构自动选择策略。

为什么要分发
------------
本项目语料混合了两类文档：

· 结构化速查文档（`====` 标题、`【一、xxx】` 章、`----节----`、一行一条命令）
  → 适合 StructuredSplitter：行不切、节不跨、块首注入标题路径。

· 无结构或弱结构文档（纯段落、单行长文本、HTML 片段）
  → 仍用 DocumentProcessor（递归字符切分），避免新策略在退化语料上表现更差。

两者对上层暴露同一个 `process(text, metadata) -> List[Document]` 接口，
因此接入方只需要把 `DocumentProcessor()` 换成 `get_processor()`，其余代码不动。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from langchain_core.documents import Document

from app.rag.processor import DocumentProcessor
from app.rag.structured_splitter import StructuredSplitter

# 结构标记：章级【…】、节级 ----、文档级 ====
_RE_CHAPTER = re.compile(r"^\s*【[^】]+】\s*$")
_RE_SUBSECTION = re.compile(r"^\s*-{3,}\s*\S.*?-{3,}\s*$")
_RE_RULE = re.compile(r"^\s*[=\-]{5,}\s*$")

# 判定为「结构化文档」的门槛：
#   至少 2 个结构标记，且标记行占非空行比例 ≥ 1%。
# 实测 17 份语料中，结构化的那些比例普遍在 2% 以上，普通段落文档接近 0。
MIN_MARKERS = 2
MIN_MARKER_RATIO = 0.01


def looks_structured(text: str) -> bool:
    """粗判文档是否存在可用的章节结构。"""
    if not text:
        return False
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return False

    markers = 0
    for line in lines:
        if _RE_CHAPTER.match(line) or _RE_SUBSECTION.match(line) or _RE_RULE.match(line):
            markers += 1

    return markers >= MIN_MARKERS and (markers / len(lines)) >= MIN_MARKER_RATIO


class AdaptiveProcessor:
    """按文档结构在两种分块策略之间分派。"""

    def __init__(self) -> None:
        self.structured = StructuredSplitter()
        self.plain = DocumentProcessor()

    def process(self, text: str, metadata: Optional[Dict] = None) -> List[Document]:
        if looks_structured(text):
            docs = self.structured.split(text, metadata)
            if docs:
                return docs
            # 结构化解析没产出（例如只有分隔线没有正文），回落字符切分
        return self.plain.process(text, metadata=metadata)

    @property
    def description(self) -> str:
        return "自适应分块（结构化→结构感知；否则→递归字符切分）"


def get_processor() -> AdaptiveProcessor:
    """接入点统一入口。"""
    return AdaptiveProcessor()
