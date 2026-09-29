"""切块、BM25 检索、知识工具测试。"""

import pytest

from server_agent.knowledge.chunker import chunk_markdown, _split_long
from server_agent.knowledge.index import BM25Index, build_from_dir, tokenize
from server_agent.knowledge.retriever import KnowledgeBase, get_knowledge_base, reset_knowledge_base
from server_agent.tools import registry


DOC = """# 运维手册

## 备份约定
数据库备份目录是 /data/backup/dbname/，每天 03:00 执行。
备份保留 14 天，不要手工删除。

## 日志轮转
logrotate 配置在 /etc/logrotate.d/，访问日志暴涨通常是被扫描。
"""


def test_tokenize_mixes_cjk_bigrams_and_ascii_words():
    tokens = tokenize("nginx 502 磁盘满了")
    assert "nginx" in tokens and "502" in tokens
    assert "磁盘" in tokens and "盘满" in tokens and "满了" in tokens   # 中文二元组
    assert "盘" in tokens                                              # 单字兜底


def test_chunk_markdown_keeps_heading_path():
    chunks = chunk_markdown(DOC, "ops.md")
    assert [c.heading for c in chunks][:2] == ["运维手册 > 备份约定", "运维手册 > 日志轮转"]
    assert "每天 03:00" in chunks[0].text and chunks[0].label() == "ops.md#运维手册 > 备份约定"
    assert all(len(c.text) >= 20 for c in chunks)


def test_split_long_overlaps_and_stays_under_limit():
    text = "句子。" * 800
    parts = _split_long(text, max_chars=200, overlap=50)
    assert len(parts) > 1 and all(len(p) <= 220 for p in parts)
    assert len(parts) == len({id(p) for p in parts})


def test_bm25_ranks_relevant_chunk_first():
    idx = BM25Index()
    idx.add_chunks(chunk_markdown(DOC, "ops.md"))
    hits = idx.search("备份目录在哪", top_k=3)
    assert hits and "备份" in hits[0].chunk.heading
    assert hits[0].score > 0 and hits[0].matched

    hits = idx.search("logrotate", top_k=3)
    assert hits and hits[0].chunk.heading.endswith("日志轮转")


def test_bm25_no_hit_returns_empty():
    idx = BM25Index()
    idx.add_chunks(chunk_markdown(DOC, "ops.md"))
    assert idx.search("kubernetes 集群证书") == []
    assert BM25Index().search("anything") == []


def test_index_save_load_roundtrip(tmp_path):
    idx = BM25Index()
    idx.add_chunks(chunk_markdown(DOC, "ops.md"))
    path = tmp_path / "idx.json"
    idx.save(path)
    restored = BM25Index.load(path)
    assert len(restored) == len(idx)
    assert restored.search("备份")[0].chunk.heading == idx.search("备份")[0].chunk.heading


def test_build_from_dir_skips_readme_and_handles_missing(tmp_path):
    (tmp_path / "a.md").write_text(DOC, encoding="utf-8")
    (tmp_path / "README.md").write_text("# 目录说明\n这里只是说明", encoding="utf-8")
    (tmp_path / "note.txt").write_text("这是一个普通文本笔记，记录的是端口分配约定与责任人信息。", encoding="utf-8")
    idx = build_from_dir(tmp_path)
    docs = {c.doc for c in idx.chunks}
    assert docs == {"a.md", "note.txt"}
    assert len(build_from_dir(tmp_path / "nope")) == 0


def test_knowledge_base_search_and_documents(tmp_path):
    (tmp_path / "handbook.md").write_text(DOC, encoding="utf-8")
    kb = KnowledgeBase(tmp_path)
    assert kb.size > 0 and kb.documents() == ["handbook.md"]
    assert kb.search("备份", top_k=1)[0].chunk.doc == "handbook.md"
    assert kb.search("", top_k=1) == []          # 空查询不返回结果
    assert kb.rebuild() == kb.size


def test_repo_knowledge_dir_loads():
    reset_knowledge_base(None)
    kb = get_knowledge_base()
    assert kb.size > 0
    docs = kb.documents()
    assert any("ops-handbook" in d for d in docs) and any("incidents" in d for d in docs)


async def test_search_knowledge_tool(monkeypatch):
    kb = KnowledgeBase.__new__(KnowledgeBase)     # 用内存索引，避免依赖文件
    from pathlib import Path

    idx = BM25Index()
    idx.add_chunks(chunk_markdown(DOC, "handbook.md"))
    kb.root = Path(".")
    kb.index = idx
    monkeypatch.setattr("server_agent.tools.knowledge_tools.get_knowledge_base", lambda: kb)

    r = await registry.call("search_knowledge", {"query": "备份目录", "top_k": 2})
    assert r.ok and r.data["hits"]
    hit = r.data["hits"][0]
    assert hit["source"] == "handbook.md#运维手册 > 备份约定" and hit["score"] > 0
    assert "每天 03:00" in hit["text"]
    assert r.data["documents"] == ["handbook.md"]

    r = await registry.call("list_knowledge_docs")
    assert r.ok and r.data["count"] == 1


async def test_search_knowledge_when_empty(monkeypatch):
    kb = KnowledgeBase.__new__(KnowledgeBase)
    from pathlib import Path

    kb.root, kb.index = Path("."), BM25Index()
    monkeypatch.setattr("server_agent.tools.knowledge_tools.get_knowledge_base", lambda: kb)
    r = await registry.call("search_knowledge", {"query": "任意"})
    assert r.ok and r.data["hits"] == [] and "知识库为空" in r.data["hint"]

    r = await registry.call("list_knowledge_docs")
    assert r.ok and r.data["count"] == 0


async def test_search_knowledge_no_hit_hint(monkeypatch):
    idx = BM25Index()
    idx.add_chunks(chunk_markdown(DOC, "handbook.md"))
    kb = KnowledgeBase.__new__(KnowledgeBase)
    from pathlib import Path

    kb.root, kb.index = Path("."), idx
    monkeypatch.setattr("server_agent.tools.knowledge_tools.get_knowledge_base", lambda: kb)
    r = await registry.call("search_knowledge", {"query": "kubernetes prometheus 监控告警"})
    assert r.ok and r.data["hits"] == [] and "没有命中" in r.data["hint"]


def test_repo_search_finds_expected_docs():
    """真实的仓库文档要能被搜到（端到端排障依赖这一点）。"""
    reset_knowledge_base(None)
    kb = get_knowledge_base()
    assert "backup" in str(kb.search("备份目录在哪", top_k=2)[0].chunk.text) or \
        "备份" in kb.search("备份目录在哪", top_k=2)[0].chunk.heading
    assert "logrotate" in kb.search("日志轮转配置", top_k=3)[0].chunk.text.lower() or \
        any("logrotate" in h.chunk.text.lower() for h in kb.search("logrotate", top_k=3))
    names = {h.chunk.doc for h in kb.search("端口占用 起不来", top_k=5)}
    assert any("incidents" in n for n in names)
