"""style-distill skill 一键同步：仓库镜像 → 用户运行时目录，逐字节校验。

用法：python sync_skill.py [--check]
默认：同步全部跟踪文件并逐字节校验；--check 只校验不同步。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]          # style-distill/_skill/style-distill
DST = Path.home() / ".agents/skills/style-distill"
FILES = [
    "SKILL.md",
    "LESSONS.md",
    "references/template.md",
    "references/negative-library.md",
    "references/identity-library.md",
    "references/pipeline-notes.md",
    "scripts/style_metrics.py",
    "scripts/series_audit.py",
    "scripts/extract_source.py",
]


def main() -> int:
    check_only = "--check" in sys.argv
    ok = True
    lines = 0
    for name in FILES:
        s, d = SRC / name, DST / name
        if not s.exists():
            print(f"MISSING in mirror: {name}")
            ok = False
            continue
        if name.endswith(".md") and name == "SKILL.md":
            lines = len(s.read_text(encoding="utf-8").splitlines())
        if d.exists() and s.read_bytes() == d.read_bytes():
            print(f"OK  {name}")
            continue
        if check_only:
            print(f"DIFF {name}")
            ok = False
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        same = s.read_bytes() == d.read_bytes()
        ok = ok and same
        print(f"{'OK ' if same else 'FAIL'} {name} (synced)")
    print(f"SKILL.md 行数: {lines}（>350 考虑压缩实测故事段）")
    print("ALL OK" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
