"""Compare the repository skill mirror with the runtime copy.

Default and --check are read-only.  Use --apply only after reviewing the
complete manifest; differing runtime files are copied to a preserve folder
before replacement and installed-only files are never deleted.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
DST = Path.home() / ".agents/skills/style-distill"
EXCLUDED_PARTS = {"history-content"}


def manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or EXCLUDED_PARTS.intersection(path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def show(source: dict[str, str], installed: dict[str, str]) -> bool:
    source_names, installed_names = set(source), set(installed)
    source_only = sorted(source_names - installed_names)
    installed_only = sorted(installed_names - source_names)
    different = sorted(
        name for name in source_names & installed_names if source[name] != installed[name]
    )
    same = sorted(source_names & installed_names - set(different))
    for name in same:
        print(f"OK       {name}")
    for name in source_only:
        print(f"SOURCE   {name}  (runtime missing; suggest review)")
    for name in different:
        print(f"DIFF     {name}  (preserve runtime copy before apply)")
    for name in installed_only:
        print(f"INSTALLED {name}  (runtime-only; preserve, never delete)")
    print(
        "MANIFEST source=%d installed=%d same=%d source-only=%d different=%d installed-only=%d"
        % (len(source), len(installed), len(same), len(source_only), len(different), len(installed_only))
    )
    if source_only or different or installed_only:
        print("SUGGESTION review this report; no runtime files were changed.")
    else:
        print("ALL OK; no runtime changes needed.")
    return not (source_only or different)


def apply(source: dict[str, str], installed: dict[str, str]) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    preserve = DST / ".sync-preserve" / stamp
    changed = sorted(
        name for name, digest in source.items() if installed.get(name) != digest
    )
    for name in changed:
        src, dst = SRC / name, DST / name
        if dst.exists():
            backup = preserve / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup)
            print(f"PRESERVE {name} -> {backup}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"APPLIED  {name}")
    print("No installed-only files were deleted.")
    return 0


def main() -> int:
    source = manifest(SRC)
    installed = manifest(DST) if DST.exists() else {}
    clean = show(source, installed)
    if "--apply" in sys.argv:
        if "--check" in sys.argv:
            print("REFUSED --apply cannot be combined with --check")
            return 2
        return apply(source, installed)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
