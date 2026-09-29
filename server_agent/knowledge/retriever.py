"""检索层：把索引与「引用溯源」包在一起，供工具调用。

引用溯源是 RAG 能不能被信任的关键：**结论必须能指回原文**。
所以检索结果一定带 `source`（文件#标题）与 `score`，提示词里也要求模型在引用时写出处。
"""

from __future__ import annotations

from pathlib import Path

from server_agent.knowledge.index import BM25Index, ScoredChunk, build_from_dir


class KnowledgeBase:
    def __init__(self, root: str | Path = "knowledge", *, index: BM25Index | None = None):
        self.root = Path(root)
        self.index = index if index is not None else build_from_dir(self.root)

    @property
    def size(self) -> int:
        return len(self.index)

    def search(self, query: str, top_k: int = 5, *, min_score: float = 0.0) -> list[ScoredChunk]:
        return [s for s in self.index.search(query, top_k=top_k) if s.score > min_score]

    def documents(self) -> list[str]:
        return sorted({c.doc for c in self.index.chunks})

    def rebuild(self) -> int:
        self.index = build_from_dir(self.root)
        return len(self.index)


_kb: KnowledgeBase | None = None


def default_knowledge_dir() -> Path:
    from server_agent.config import get_settings

    configured = Path(get_settings().knowledge_dir)
    if configured.is_dir():
        return configured
    return Path(__file__).resolve().parents[2] / "knowledge"


def get_knowledge_base(path: str | Path | None = None) -> KnowledgeBase:
    global _kb
    if _kb is None:
        candidate = Path(path) if path else default_knowledge_dir()
        if not candidate.is_dir():
            candidate = default_knowledge_dir()
        _kb = KnowledgeBase(candidate)
    return _kb


def reset_knowledge_base(kb: KnowledgeBase | None = None) -> None:
    global _kb
    _kb = kb
