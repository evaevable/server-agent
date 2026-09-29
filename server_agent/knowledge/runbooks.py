"""Runbook：把「怎么排查」写成可加载的手册。

Runbook 与 Prompt 的区别：
- 提示词是「思维方式」（先宏观后微观、USE 方法）；
- Runbook 是「具体流程」（磁盘满了：先 df，再 du，再看 logrotate）。

与第 13 章 RAG 的区别：
- Runbook 是**结构化、可信、随仓库走**的程序性知识（自己写的）；
- RAG 面向**开放的文档集合**（可能是别人写的、会过期的）。

格式：Markdown + frontmatter（YAML）。frontmatter 里的 symptoms 用来匹配问题描述。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class Runbook:
    name: str
    title: str = ""
    symptoms: list[str] = field(default_factory=list)
    body: str = ""
    path: str = ""

    def to_dict(self, *, with_body: bool = True) -> dict:
        d = {"name": self.name, "title": self.title, "symptoms": self.symptoms, "path": self.path}
        if with_body:
            d["body"] = self.body
        return d

    def matches(self, text: str) -> int:
        """命中分数：按 symptoms 里出现在文本中的关键词个数计分。"""
        low = (text or "").lower()
        return sum(1 for kw in self.symptoms if kw.lower() in low)


class RunbookLibrary:
    def __init__(self, runbooks: list[Runbook] | None = None):
        self.runbooks = {r.name: r for r in (runbooks or [])}

    def names(self) -> list[str]:
        return list(self.runbooks)

    def get(self, name: str) -> Runbook | None:
        return self.runbooks.get(name)

    def search(self, question: str, limit: int = 3) -> list[Runbook]:
        scored = [(r.matches(question), r) for r in self.runbooks.values()]
        hits = [r for score, r in sorted(scored, key=lambda x: -x[0]) if score > 0]
        return hits[:limit]

    def load_dir(self, path: str | Path) -> "RunbookLibrary":
        root = Path(path)
        if not root.is_dir():
            return self
        for file in sorted(root.glob("*.md")):
            rb = parse_runbook(file)
            if rb:
                self.runbooks[rb.name] = rb
        return self


def parse_runbook(path: str | Path) -> Runbook | None:
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None                       # 没有 frontmatter 的文件不是 runbook
    try:
        _, front, body = text.split("---", 2)
    except ValueError:
        return None
    meta = (yaml.safe_load(front) if yaml else {}) or {}
    name = str(meta.get("name") or Path(path).stem)
    return Runbook(name=name, title=str(meta.get("title") or name),
                   symptoms=[str(s) for s in (meta.get("symptoms") or [])],
                   body=body.strip(), path=str(path))


_library: RunbookLibrary | None = None


def default_runbooks_dir() -> Path:
    """默认手册目录：先看当前工作目录，再看仓库根（测试/服务的工作目录可能不同）。"""
    from server_agent.config import get_settings

    configured = Path(get_settings().runbooks_dir)
    if configured.is_dir():
        return configured
    repo_root = Path(__file__).resolve().parents[2]   # server_agent/knowledge/runbooks.py -> 仓库根
    return repo_root / "runbooks"


def get_library(path: str | Path | None = None) -> RunbookLibrary:
    """加载（并缓存）手册库。显式传的路径不存在时，退回默认目录而不是返回空库。"""
    global _library
    if _library is None:
        candidate = Path(path) if path else None
        if candidate is None or not candidate.is_dir():
            candidate = default_runbooks_dir()
        _library = RunbookLibrary().load_dir(candidate)
    return _library


def reset_library(lib: RunbookLibrary | None = None) -> None:
    global _library
    _library = lib
