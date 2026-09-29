"""文档切块：RAG 的第一步。

切块的取舍：
- **太碎**：检索到的片段缺少上下文（「重启它」——重启谁？）；
- **太大**：噪声多、占用 token，检索精度下降。

本项目的做法：按 Markdown 标题切（`##` 二级为准），保留标题路径作为上下文前缀，
超长段落再按字符滑窗切分并保留重叠（overlap），避免把一句话劈成两半。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_CHARS = 900
OVERLAP = 120


@dataclass
class Chunk:
    doc: str                    # 来源文件（相对路径）
    heading: str = ""           # 标题路径，如 "磁盘满排查 > 定位"
    text: str = ""
    index: int = 0              # 在文档内的序号

    def to_dict(self) -> dict:
        return {"doc": self.doc, "heading": self.heading, "text": self.text, "index": self.index}

    def label(self) -> str:
        return f"{self.doc}#{self.heading}" if self.heading else self.doc


def _split_long(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[str]:
    """超长段落按滑窗切分，保留 overlap，尽量避免切断语义。"""
    if len(text) <= max_chars:
        return [text]
    parts, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # 尽量在句号/换行处断开
        window = text[start:end]
        cut = max(window.rfind("\n"), window.rfind("。"), window.rfind(". "))
        if cut > max_chars * 0.5:
            end = start + cut + 1
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in parts if p]


def chunk_markdown(text: str, doc: str) -> list[Chunk]:
    """按 `##`/`###` 标题切块，标题路径写进 chunk 作为上下文。"""
    chunks: list[Chunk] = []
    heading_stack: list[str] = []
    buffer: list[str] = []
    idx = 0

    def flush() -> None:
        nonlocal idx, buffer
        body = "\n".join(buffer).strip()
        buffer = []
        if not body:
            return
        heading = " > ".join(heading_stack)
        for piece in _split_long(body):
            chunks.append(Chunk(doc=doc, heading=heading, text=piece, index=idx))
            idx += 1

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            heading_stack = heading_stack[: max(0, level - 1)]
            while len(heading_stack) < level - 1:
                heading_stack.append("")
            heading_stack.append(title)
            continue
        buffer.append(line)
    flush()
    return [c for c in chunks if len(c.text) >= 20]     # 太短的碎片丢掉
