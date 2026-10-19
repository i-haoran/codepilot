"""
文档清洗与分块。

为什么不用 langchain_text_splitters
---------------------------------
原实现依赖 `from langchain_text_splitters import RecursiveCharacterTextSplitter`，
但该包在 langchain 1.x 生态中已不再是默认安装项（当前环境即不存在该模块），
一旦缺失，`app.rag.processor` 导入失败 → 路由层 import 失败 → 整个服务起不来。

这里改为自实现递归分块：逻辑与 RecursiveCharacterTextSplitter 一致
（按分隔符优先级递归切分、带重叠），但**零外部依赖**，任何环境都能工作。
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from langchain_core.documents import Document

from app.core.config import settings

logger = logging.getLogger(__name__)

# 分隔符按"语义边界从强到弱"排列：段落 → 换行 → 中英文句末 → 逗号 → 空格
DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ".", "!", "?", "；", ";", "，", ",", " ", ""]

# 连续 3 个以上换行 → 压成 2 个（保留段落边界）。一次替换，避免反复扫描。
_RE_BLANK_LINES = re.compile(r"\n{3,}")


class DocumentProcessor:
    """文档清洗 + 递归分块。"""

    def __init__(
        self,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        separators: Optional[List[str]] = None,
    ) -> None:
        self.chunk_size = chunk_size or settings.CHUNK_SIZE
        self.chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.CHUNK_OVERLAP
        self.separators = separators or DEFAULT_SEPARATORS

        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size，否则分块无法终止")

    # ------------------------------------------------------------------ #
    def process(self, text: str, metadata: Optional[Dict] = None) -> List[Document]:
        """清洗 → 分块 → 包装为带元数据的 Document 列表。"""
        cleaned = self._clean_text(text)
        if not cleaned:
            return []

        chunks = self.split_text(cleaned)
        documents: List[Document] = []
        for index, chunk in enumerate(chunks):
            doc_metadata = dict(metadata or {})
            doc_metadata["chunk_index"] = index
            doc_metadata["total_chunks"] = len(chunks)
            doc_metadata["char_count"] = len(chunk)
            documents.append(Document(page_content=chunk, metadata=doc_metadata))
        return documents

    # ------------------------------------------------------------------ #
    def split_text(self, text: str) -> List[str]:
        """递归切分，返回长度不超过 chunk_size 的片段列表。"""
        if not text:
            return []
        return self._split(text, self.separators)

    def _split(self, text: str, separators: List[str]) -> List[str]:
        # 已足够短：直接返回
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []

        separator = separators[0] if separators else ""
        remaining = separators[1:] if len(separators) > 1 else [""]

        # 无分隔符可切：硬切
        if separator == "":
            return self._hard_split(text)

        parts = text.split(separator)
        if len(parts) == 1:
            # 当前分隔符无效，降级到下一优先级
            return self._split(text, remaining)

        chunks: List[str] = []
        buffer = ""
        for part in parts:
            # 保留分隔符，避免句间语义粘连
            piece = part + separator if part is not parts[-1] else part
            if len(piece) > self.chunk_size:
                # 单段超长：先冲刷缓冲，再对这段继续降级切分
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                chunks.extend(self._split(piece, remaining))
                continue

            if len(buffer) + len(piece) <= self.chunk_size:
                buffer += piece
            else:
                if buffer:
                    chunks.append(buffer)
                buffer = piece

        if buffer:
            chunks.append(buffer)

        return self._apply_overlap([c for c in chunks if c.strip()])

    def _hard_split(self, text: str) -> List[str]:
        """无任何分隔符时的定长切分。"""
        size = self.chunk_size
        return [text[i:i + size] for i in range(0, len(text), size)]

    def _apply_overlap(self, chunks: List[str]) -> List[str]:
        """
        为相邻分块添加重叠，避免答案正好落在两个分块的交界处而检索不到。
        """
        if self.chunk_overlap <= 0 or len(chunks) <= 1:
            return chunks

        result = [chunks[0]]
        for chunk in chunks[1:]:
            tail = result[-1][-self.chunk_overlap:]
            result.append((tail + chunk)[: self.chunk_size + self.chunk_overlap])
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_text(text: str) -> str:
        """
        清洗文本。

        原实现用 `" ".join(text.split())` 把**所有换行也压成空格**，
        这会让 `\\n\\n`、`\\n` 这两个最重要的分块分隔符彻底失效。
        这里只规整空白字符，保留换行结构。
        """
        if not text:
            return ""

        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        # 去掉行尾空白
        lines = [line.rstrip() for line in text.split("\n")]
        text = "\n".join(lines)
        # 3 个以上连续换行压成 2 个（保留段落边界）。
        # 用正则一次替换，避免原先 `while ... replace` 的 O(n²) 问题：
        # 那个写法每次只把 3 个换行压成 2 个，遇到长串空行要反复扫描全文。
        text = _RE_BLANK_LINES.sub("\n\n", text)
        # 行内多空格压缩为单空格（不动换行）。
        # 注意必须是 split() 不带参数——按任意空白拆分并丢弃空串，才真正压缩；
        # 写成 split(" ") 与 " ".join 互为逆运算，等于什么都没做。
        normalized_lines = [
            " ".join(line.split()) if "  " in line else line
            for line in text.split("\n")
        ]
        return "\n".join(normalized_lines).strip()

    @property
    def description(self) -> str:
        return f"递归分块 chunk_size={self.chunk_size} overlap={self.chunk_overlap}"
