"""BM25 检索：零依赖的关键词检索。

为什么不上向量检索？三个现实理由：
1. 依赖与成本：向量需要 embedding 服务（或本地模型）+ 向量库；
2. 运维文档的特点是**关键词很具体**（`logrotate`、`502`、`/var/log/nginx`）——BM25 在这类查询上很强；
3. 先有可用的基线，再按评测结果决定是否需要升级——**没有评测的升级是赌博**。

中文处理：不做分词器依赖，改用「字符二元组（bigram）+ 英文/数字整词」的混合切分，
对「磁盘满了」这类查询效果足够，且零依赖、跨平台一致。

BM25 公式（k1=1.5, b=0.75，业界常用默认值）：
    score(q, d) = Σ_t IDF(t) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * |d| / avgdl))
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from server_agent.knowledge.chunker import Chunk, chunk_markdown

TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+")
K1, B = 1.5, 0.75
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """混合切分：英文/数字整词（小写）+ 中文字符二元组。"""
    text = text or ""
    tokens = [t.lower() for t in TOKEN_RE.findall(text) if len(t) > 1 or t.isdigit()]
    cjk = [ch for ch in text if CJK_RE.match(ch)]
    tokens += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    tokens += cjk                                     # 单字也留一份，避免两字词无法命中
    return tokens


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.chunk.to_dict()
        d.update({"score": round(self.score, 3), "source": self.chunk.label(),
                  "matched": self.matched[:8]})
        return d


class BM25Index:
    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avgdl: float = 0.0

    def __len__(self) -> int:
        return len(self.chunks)

    def add_chunks(self, chunks: list[Chunk]) -> None:
        self.chunks.extend(chunks)
        self._rebuild()

    def _rebuild(self) -> None:
        self._tf = [Counter(tokenize(c.heading + "\n" + c.text)) for c in self.chunks]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        self._avgdl = (sum(sum(tf.values()) for tf in self._tf) / len(self._tf)) if self._tf else 0.0

    def search(self, query: str, top_k: int = 5) -> list[ScoredChunk]:
        if not self.chunks:
            return []
        q_tokens = tokenize(query)
        n = len(self.chunks)
        scored: list[ScoredChunk] = []
        for i, tf in enumerate(self._tf):
            dl = sum(tf.values()) or 1
            score, matched = 0.0, []
            for term in set(q_tokens):
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / (self._avgdl or 1)))
                matched.append(term)
            if score > 0:
                scored.append(ScoredChunk(self.chunks[i], score, matched))
        scored.sort(key=lambda s: -s.score)
        return scored[:top_k]

    # ---------- 持久化 ----------
    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(
            {"chunks": [c.to_dict() for c in self.chunks]}, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "BM25Index":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        idx = cls()
        idx.add_chunks([Chunk(**c) for c in data.get("chunks", [])])
        return idx


def build_from_dir(root: str | Path, patterns: tuple[str, ...] = ("*.md", "*.txt")) -> BM25Index:
    idx = BM25Index()
    root_path = Path(root)
    if not root_path.is_dir():
        return idx
    for pattern in patterns:
        for file in sorted(root_path.rglob(pattern)):
            if file.name.upper().startswith("README"):
                continue          # README 多半是目录说明，噪声大
            text = file.read_text(encoding="utf-8", errors="replace")
            idx.add_chunks(chunk_markdown(text, str(file.relative_to(root_path))))
    return idx
