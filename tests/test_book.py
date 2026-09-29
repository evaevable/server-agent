"""书稿一致性：BOOK.md 必须与 docs/chapters 同步，目录锚点与相对链接必须有效。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_book_is_in_sync_with_chapters():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_book.py"), "--check"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr or r.stdout


def test_chapters_have_no_conversation_residue():
    residue = ("回到对话里", "我就开讲", "下一章预告", "本章导读", "积木", "本课程", "以练带学")
    for f in sorted((ROOT / "docs" / "chapters").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        hits = [w for w in residue if w in text]
        assert not hits, f"{f.name} 残留对话式讲义用语：{hits}"
