"""
结构感知分块器（Structure-Aware Splitter）。

为什么需要它
------------
本项目实际的语料是「技术速查手册 / 命令笔记」，形态是：

    ========================================
            Docker 常用命令速查表
    ========================================
    ---------- Docker 安装与下载 ----------
    docker --version                       查看 Docker 版本
    docker info                            查看系统详细信息

对这种文档，`RecursiveCharacterTextSplitter` 式的「按字符数 + 按标点切」有两个硬伤：

1. **切点落在行中间**。实测本项目 17 份文档、2358 个非空行：行长中位数 30 字符、
   P90 为 64 字符、最长 372 字符。目标块 512 字符并不是行长的整数倍，按字符累计
   必然把某一行拦腰截断，于是出现「2. 查看状态」被切成「2」+「. 查看状态」这种残片。

2. **跨节**。字符切分不认识 `【一、xxx】` / `--------` 这类结构标记，会把两个不相关
   章节的内容混进同一个块，检索命中后注入的上下文里夹着别的小节，模型容易答偏。

本模块的做法
------------
· **行是原子单元**：任何一行都整体保留，绝不在行内断开（实测最长 372 < 512，安全）。
· **节是父边界**：`【】`、`====`、`----` 视为结构标记，聚合绝不跨节。
· **按行聚合**：在单个节内，逐行累加到目标长度就落成一块。
· **整行重叠**：用上一块的「最后一行」作为下一块的开头，而不是按 50 个字符截取，
  保证重叠出来的永远是完整行，不会产生半截编号。
· **标题路径注入**：每块正文前拼上 `[文件标题 > 节 > 小节]`，让块脱离全文也能自足，
  这是替代「父子检索」的轻量方案。

不依赖 langchain_text_splitters，只用 langchain_core 的 Document 与标准库。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document

# ---------------------------------------------------------------- #
# 结构标记识别
# ---------------------------------------------------------------- #
# 文件/节级分隔线：连续 5 个以上的 = 或 -
_RE_RULE = re.compile(r"^[=\-]{5,}\s*$")
# 中文节标题：【一、文件与目录操作】
_RE_SECTION_CN = re.compile(r"^【(?P<title>[^】]+)】\s*$")
# 小标题：---------- Docker 安装与下载 ----------
_RE_SUBSECTION = re.compile(r"^-{3,}\s*(?P<title>\S.*?)\s*-{3,}$")


class StructuredSplitter:
    """按「节 → 行」两级结构切分，行不可分割。"""

    def __init__(
        self,
        target_chars: int = 480,
        max_chars: int = 640,
        overlap_lines: int = 1,
        inject_path: bool = True,
    ) -> None:
        if target_chars <= 0 or max_chars < target_chars:
            raise ValueError("要求 0 < target_chars <= max_chars")
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.overlap_lines = max(0, overlap_lines)
        self.inject_path = inject_path

    # ------------------------------------------------------------ #
    def split(self, text: str, metadata: Optional[Dict] = None) -> List[Document]:
        """切分整篇文档，返回带结构元数据的 Document 列表。"""
        if not text or not text.strip():
            return []

        doc_title, segments = self._parse_structure(text)

        documents: List[Document] = []
        for section_path, lines in segments:
            for chunk_lines, start_line in self._pack(section_path, lines):
                body = "\n".join(chunk_lines).strip()
                if not body:
                    continue
                content = f"[{section_path}]\n{body}" if self.inject_path else body
                meta = dict(metadata or {})
                meta.update(
                    {
                        "doc_title": doc_title,
                        "section_path": section_path,
                        "start_line": start_line,
                        "line_count": len(chunk_lines),
                        "char_count": len(content),
                        "chunk_type": "structured",
                    }
                )
                documents.append(Document(page_content=content, metadata=meta))

        # 补一个全局序号，便于按原始顺序回溯
        for index, doc in enumerate(documents):
            doc.metadata["chunk_index"] = index
            doc.metadata["total_chunks"] = len(documents)
        return documents

    # ------------------------------------------------------------ #
    # 结构解析
    # ------------------------------------------------------------ #
    def _parse_structure(self, text: str) -> Tuple[str, List[Tuple[str, List[str]]]]:
        """
        把全文解析成 (文档标题, [(节路径, 该节的所有行), ...])。

        节路径形如 `Docker 常用命令速查表 > Docker 安装与下载`。
        """
        # 去掉 UTF-8 BOM，否则首行会带上 \ufeff，标题与分隔线都识别不出来
        text = text.lstrip("\ufeff")
        raw_lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]

        doc_title = ""
        segments: List[Tuple[str, List[str]]] = []
        current_section = ""
        current_sub = ""
        buffer: List[str] = []

        def path_of() -> str:
            parts = [p for p in (doc_title, current_section, current_sub) if p]
            return " > ".join(parts) if parts else "正文"

        def flush() -> None:
            nonlocal buffer
            if buffer:
                segments.append((path_of(), buffer))
                buffer = []

        # 找出正文行的索引序列（跳过空行）
        non_empty = [i for i, line in enumerate(raw_lines) if line.strip()]

        # 文档标题识别，兼容三种常见写法：
        #   A. 下划线式      「标题」下一行是 ==== 或 ----
        #   B. 上下包夹式    首行就是 ====，标题在规则线之后
        #   C. 无任何分隔线   直接取第一行非空文本
        doc_title = ""
        if non_empty:
            first = non_empty[0]
            second = non_empty[1] if len(non_empty) > 1 else None
            if _RE_RULE.match(raw_lines[first].strip()):
                # B：首行是规则线，取其后第一行
                for idx in non_empty[1:]:
                    if not _RE_RULE.match(raw_lines[idx].strip()):
                        doc_title = raw_lines[idx].strip()
                        break
            elif second is not None and _RE_RULE.match(raw_lines[second].strip()):
                # A：下划线式
                doc_title = raw_lines[first].strip()
            else:
                # C：无分隔线
                doc_title = raw_lines[first].strip()

        for line in raw_lines:
            stripped = line.strip()

            # 分隔线：本身不入正文
            if not stripped or _RE_RULE.match(stripped):
                continue

            section_match = _RE_SECTION_CN.match(stripped)
            if section_match:
                flush()
                current_section = section_match.group("title").strip()
                current_sub = ""
                continue

            sub_match = _RE_SUBSECTION.match(stripped)
            if sub_match:
                flush()
                current_sub = sub_match.group("title").strip()
                continue

            buffer.append(line)

        flush()
        return doc_title, segments

    # ------------------------------------------------------------ #
    # 节内按行聚合
    # ------------------------------------------------------------ #
    def _pack(self, section_path: str, lines: List[str]) -> List[Tuple[List[str], int]]:
        """在单个节内把行聚合成块；返回 [(块内行, 起始行号), ...]。"""
        result: List[Tuple[List[str], int]] = []
        current: List[str] = []
        current_len = 0
        start = 0

        for offset, line in enumerate(lines):
            line_len = len(line) + 1  # +1 为换行符

            # 单行超长（本项目实测不存在，兜底保留整行不切）
            if line_len > self.max_chars:
                if current:
                    result.append((current, start))
                    current, current_len = [], 0
                result.append(([line], offset))
                continue

            if not current:
                start = offset

            if current_len + line_len > self.target_chars and current:
                result.append((current, start))
                # 整行重叠：用上一块最后 1 行作为新块开头
                carry = current[-self.overlap_lines:] if self.overlap_lines else []
                current = list(carry)
                current_len = sum(len(x) + 1 for x in current)
                start = offset - len(carry)

            current.append(line)
            current_len += line_len

        if current:
            result.append((current, start))
        return result

    # ------------------------------------------------------------ #
    @property
    def description(self) -> str:
        return (
            f"结构感知分块 target={self.target_chars} max={self.max_chars} "
            f"overlap_lines={self.overlap_lines} inject_path={self.inject_path}"
        )
