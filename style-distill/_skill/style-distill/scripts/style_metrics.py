#!/usr/bin/env python3
"""风格量化对比：WD14 打标 + colorgram 调色板。

用法：
    python style_metrics.py <模板图> <产出图> [--threshold 0.35] [--out report.json]

输出：
    - 两图各 8 色调色板（hex + 占比）
    - WD14 ≥阈值 的通用标签：共同 / 仅模板 / 仅产出 / 重合率
    - 判读指引：重合率与「仅模板」清单分别回答画风保真与内容差异。

依赖：pip install wdtagger colorgram.py（wdtagger 首次运行经 HF_ENDPOINT 下载模型，CPU 可跑）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import colorgram  # noqa: E402
from PIL import Image  # noqa: E402


def palettes(path: str, n: int = 8) -> list[dict]:
    return [
        {
            "hex": f"#{c.rgb.r:02x}{c.rgb.g:02x}{c.rgb.b:02x}",
            "rgb": [c.rgb.r, c.rgb.g, c.rgb.b],
            "占比": round(c.proportion, 3),
        }
        for c in colorgram.extract(path, n)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("output")
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--out", default="")
    ap.add_argument("--model-repo", default="SmilingWolf/wd-eva02-large-tagger-v3",
                    help="打标引擎（2026-10-07 默认切 EVA02-Large v3，F1 0.4772 为 WD 系最高；"
                         "旧引擎 SmilingWolf/wd-swinv2-tagger-v3 可选回退）")
    a = ap.parse_args()

    report = {"调色板": {a.template: palettes(a.template), a.output: palettes(a.output)},
              "打标引擎": a.model_repo}

    from wdtagger import Tagger

    tagger = Tagger(model_repo=a.model_repo)
    tagsets = {}
    for name, path in (("模板", a.template), ("产出", a.output)):
        result = tagger.tag(Image.open(path).convert("RGB"))
        tags = [t for t, p in result.general_tag_data.items() if p >= a.threshold]
        tagsets[name] = set(tags)
        report[f"WD14标签·{name}"] = tags

    t, o = tagsets["模板"], tagsets["产出"]
    report["标签对比"] = {
        "共同": sorted(t & o),
        "仅模板": sorted(t - o),
        "仅产出": sorted(o - t),
        "重合率": round(len(t & o) / max(len(t | o), 1), 3),
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
