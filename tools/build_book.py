#!/usr/bin/env python3
"""生成全书目录与单文件版。

产出：
- BOOK.md：篇 / 章 / 小节三级可点击目录 + 全文合并（适合离线阅读、打印、全文搜索）
- docs/chapters/SUMMARY.md：每章一行的目录（GitHub 上浏览章节目录用；也兼容 mdBook）

锚点一律显式写成 <a id="..."></a>，不依赖 GitHub 对中文标题的 slug 规则。
用法：python3 tools/build_book.py [--check]   # --check：只校验 BOOK.md 是否与章节同步，不写文件
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = ROOT / "docs" / "chapters"
DOCS = ROOT / "docs"

PARTS = [
    ("第一部分 地基：Agent 最小闭环", range(1, 5)),
    ("第二部分 产品化：让别人也能用", range(5, 8)),
    ("第三部分 可信：记得住、管得住、够得着、隔得开", range(8, 12)),
    ("第四部分 进阶：更聪明、更博学、更开放", range(12, 15)),
    ("第五部分 工程化：证明它靠谱，然后交付", range(15, 18)),
]
APPENDICES = ["appendix-a-frameworks.md", "appendix-b-glossary.md", "appendix-c-cheatsheet.md"]


def chapter_files() -> dict[int, Path]:
    files = {}
    for f in sorted(CHAPTERS.glob("[0-9][0-9]-*.md")):
        files[int(f.name[:2])] = f
    return files


def fence_aware_lines(text: str):
    inside = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            inside = not inside
            yield line, True
            continue
        yield line, inside


def sections(text: str) -> list[str]:
    """一章里的二级小节标题（N.M ...），跳过代码块里的 #。"""
    return [line[3:].strip() for line, code in fence_aware_lines(text) if not code and line.startswith("## ")]


def shift_headings(text: str, anchor_prefix: str) -> str:
    """并入 BOOK.md 时标题整体下沉一级，并在每个二级小节前放显式锚点。"""
    out, n = [], 0
    for line, code in fence_aware_lines(text):
        if not code and re.match(r"^#{1,5} ", line):
            if line.startswith("## "):
                n += 1
                out.append(f'<a id="{anchor_prefix}-{n}"></a>')
                out.append("")
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def fix_links(text: str, source: Path) -> str:
    """章节里的相对链接（../adr/x.md、../../README.md）在 BOOK.md（仓库根）里要改成相对根目录。"""
    def repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        if re.match(r"^(https?:|#|mailto:)", target):
            return m.group(0)
        resolved = (source.parent / target.split("#")[0]).resolve()
        try:
            rel = resolved.relative_to(ROOT)
        except ValueError:
            return m.group(0)
        frag = "#" + target.split("#", 1)[1] if "#" in target else ""
        return f"[{label}]({rel.as_posix()}{frag})"
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", repl, text)


def build() -> tuple[str, str]:
    chaps = chapter_files()
    missing = [n for _, r in PARTS for n in r if n not in chaps]
    if missing:
        raise SystemExit(f"缺少章节文件：{missing}")

    toc = ["# 用一个 Server Agent 学会 Agent", "",
           "> 本文件由 `tools/build_book.py` 从 `docs/chapters/` 生成，请勿手改。", "", "## 目录", ""]
    body: list[str] = []
    summary = ["# 目录", "", "- [全书大纲](../SYLLABUS.md)", ""]

    for pi, (part, nums) in enumerate(PARTS, 1):
        toc.append(f"- [{part}](#part-{pi})")
        summary += [f"## {part}", ""]
        body += ["---", "", f'<a id="part-{pi}"></a>', "", f"## {part}", ""]
        for n in nums:
            f = chaps[n]
            text = f.read_text(encoding="utf-8")
            title = text.split("\n", 1)[0].lstrip("# ").strip()
            toc.append(f"  - [{title}](#ch-{n:02d})")
            for si, sec in enumerate(sections(text), 1):
                toc.append(f"    - [{sec}](#ch-{n:02d}-{si})")
            summary.append(f"- [{title}]({f.name})")
            body += [f'<a id="ch-{n:02d}"></a>', "", fix_links(shift_headings(text, f"ch-{n:02d}"), f).rstrip(), ""]
        summary.append("")

    toc.append("- [附录](#appendix)")
    summary += ["## 附录", ""]
    body += ["---", "", '<a id="appendix"></a>', "", "## 附录", ""]
    for ai, name in enumerate(APPENDICES, 1):
        f = DOCS / name
        text = f.read_text(encoding="utf-8")
        title = text.split("\n", 1)[0].lstrip("# ").strip()
        toc.append(f"  - [{title}](#app-{ai})")
        summary.append(f"- [{title}](../{name})")
        body += [f'<a id="app-{ai}"></a>', "", fix_links(shift_headings(text, f"app-{ai}"), f).rstrip(), ""]

    book = "\n".join(toc + [""] + body).rstrip() + "\n"
    return book, "\n".join(summary).rstrip() + "\n"


def check_anchors(book: str) -> list[str]:
    anchors = set(re.findall(r'<a id="([^"]+)"></a>', book))
    refs = set(re.findall(r"\]\(#([^)]+)\)", book))
    bad = sorted(refs - anchors)
    for target in set(re.findall(r"\]\(((?!https?:|#)[^)\s#]+)(?:#[^)]*)?\)", book)):
        if not (ROOT / target).exists():
            bad.append(f"文件不存在: {target}")
    return bad


def main() -> int:
    book, summary = build()
    bad = check_anchors(book)
    if bad:
        print("目录/链接校验失败：\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    if "--check" in sys.argv:
        current = (ROOT / "BOOK.md").read_text(encoding="utf-8") if (ROOT / "BOOK.md").exists() else ""
        if current != book:
            print("BOOK.md 与章节不同步，请运行 python3 tools/build_book.py", file=sys.stderr)
            return 1
        print("BOOK.md 已同步")
        return 0
    (ROOT / "BOOK.md").write_text(book, encoding="utf-8")
    (CHAPTERS / "SUMMARY.md").write_text(summary, encoding="utf-8")
    print(f"BOOK.md {len(book.splitlines())} 行；SUMMARY.md 已更新；链接校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
