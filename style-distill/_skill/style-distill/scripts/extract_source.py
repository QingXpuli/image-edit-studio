#!/usr/bin/env python3
"""提取期先量：对源图填卡 I / 卡 II 的数字底稿。不调用上游，不生成。

用法：
    python extract_source.py <图> [<图> ...] [--out-dir DIR] [--threshold 0.35]

输出（默认写到 <第一张图所在目录>/extract-<stem>/，多图则写到 --out-dir）：
    measure.json    尺寸 / 哈希 / 调色板 / 亮区色 / edge / WD14
    card.md         卡 I 身份普查 + 卡 II 手法填空（观察槽留「未识别」）
    lineart-<stem>.png

纪律：
    WD14 只做身份 / 水印普查，禁止用标签重合率当画风。
    白底组的 edge 不能拿另一张图的门槛套用。
    卡 II 的 LINE / FACE 等槽必须人填；本脚本只给【实测】数字和线稿。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import colorgram  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

IDENTITY_HINTS = (
    "1girl", "1boy", "solo", "multiple_girls",
    "blonde_hair", "silver_hair", "black_hair", "brown_hair", "white_hair",
    "blue_eyes", "red_eyes", "green_eyes", "purple_eyes", "yellow_eyes",
    "pointy_ears", "elf_ears", "animal_ears", "cat_ears", "fox_ears",
    "forehead_mark", "facial_mark", "mole", "scar",
    "earrings", "jewelry", "necklace", "choker", "hair_ornament",
    "long_hair", "short_hair", "twintails", "ponytail", "bun",
    "blush", "smile", "closed_eyes",
)
WATERMARK_HINTS = ("watermark", "signature", "username", "artist_name", "copyright")
TECHNIQUE_HINTS = (
    "lineart", "sketch", "watercolor", "monochrome", "greyscale",
    "flat_color", "cel_shading", "thick_outline", "thin_outline",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        q = Path(p)
        if q.is_dir():
            out += sorted(f for f in q.iterdir() if f.suffix.lower() in IMG_EXT)
        elif q.is_file():
            out.append(q)
        else:
            print(f"找不到: {p}", file=sys.stderr)
    return out


def palettes(path: Path, n: int = 8) -> list[dict]:
    return [
        {
            "hex": f"#{c.rgb.r:02x}{c.rgb.g:02x}{c.rgb.b:02x}",
            "rgb": [c.rgb.r, c.rgb.g, c.rgb.b],
            "占比": round(c.proportion, 3),
        }
        for c in colorgram.extract(str(path), n)
    ]


def bright_hex(path: Path) -> str:
    im = Image.open(path).convert("RGB").resize((256, 256), Image.Resampling.BILINEAR)
    a = np.asarray(im, dtype=np.float32)
    lum = a.mean(axis=2)
    sel = lum >= np.quantile(lum, 0.80)
    mean = a[sel].mean(axis=0).astype(int)
    return "#{:02x}{:02x}{:02x}".format(*mean)


def paper_white_ratio(path: Path) -> float:
    a = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    mask = (a[:, :, 0] > 0.94) & (a[:, :, 1] > 0.94) & (a[:, :, 2] > 0.94)
    return round(float(mask.mean()) * 100, 2)


def edge_density(path: Path) -> tuple[float, float]:
    g = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    gx = np.abs(np.diff(g, axis=1))[:-1, :]
    gy = np.abs(np.diff(g, axis=0))[:, :-1]
    grad = np.maximum(gx, gy)
    return round(float((grad > 12).mean()) * 100, 2), round(float((grad > 30).mean()) * 100, 2)


def classify_tags(tags: list[str]) -> dict:
    ident, wm, tech, other = [], [], [], []
    for t in tags:
        low = t.lower()
        if any(h in low for h in WATERMARK_HINTS):
            wm.append(t)
        elif low in IDENTITY_HINTS or any(low.startswith(h) for h in IDENTITY_HINTS):
            ident.append(t)
        elif any(h in low for h in TECHNIQUE_HINTS):
            tech.append(t)
        else:
            other.append(t)
    return {"身份向": ident, "水印向": wm, "技法向": tech, "未分类": other}


def lineart_one(im: Image.Image, dest: Path) -> str | None:
    try:
        from controlnet_aux import LineartDetector
    except Exception as e:  # noqa: BLE001
        return f"未识别（controlnet_aux 不可用：{e}）"
    try:
        det = LineartDetector.from_pretrained("lllyasviel/Annotators")
        out = det(im.convert("RGB"))
        if not isinstance(out, Image.Image):
            out = Image.fromarray(np.asarray(out))
        out.convert("RGB").save(dest)
        return None
    except Exception as e:  # noqa: BLE001
        return f"未识别（Lineart 失败：{e}）"


def measure_one(path: Path, out_dir: Path, tagger, threshold: float) -> dict:
    with Image.open(path) as im:
        size = im.size
        mode = im.mode
        rgb = im.convert("RGB")
    digest = sha256_file(path)
    weak, strong = edge_density(path)
    paper = paper_white_ratio(path)
    pal = palettes(path)
    bright = bright_hex(path)
    result = tagger.tag(rgb)
    tags = sorted(t for t, p in result.general_tag_data.items() if p >= threshold)
    classes = classify_tags(tags)
    lineart_name = f"lineart-{path.stem}.png"
    lineart_err = lineart_one(rgb, out_dir / lineart_name)
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "size": list(size),
        "mode": mode,
        "sha256": digest,
        "sha256_12": digest[:12],
        "palette": pal,
        "bright": bright,
        "paper_white_pct": paper,
        "edge_weak_pct": weak,
        "edge_strong_pct": strong,
        "wd14_tags": tags,
        "wd14_classes": classes,
        "lineart": None if lineart_err else str((out_dir / lineart_name).resolve()),
        "lineart_error": lineart_err,
        "notes": [
            "WD14 标签是内容普查，不是画风保真。",
            "亮区色在白纸图上往往是纸色，不是肤色。",
            "白底会压低弱边缘，禁止拿另一张图的 edge 当这张的门槛。",
        ],
    }


SLOTS = (
    ("LINE", "粗细、匀不匀、笔锋、闭合还是断、内部排线"),
    ("EDGE", "靠线还是靠色阶；是否渗出线外"),
    ("FILL", "平涂 / 淡洗 / 薄釉；色块内部有没有变化"),
    ("VALUE", "几档明暗；谁最亮；同色相加深还是冷灰"),
    ("FACE", "渲染密度：色块眼 / 勾线虹膜+高光 / 玻璃多层。只记这张看见的"),
    ("HAIR", "色块+分束 / 逐缕 / 扫笔；长平行曲线还是碎钩"),
    ("FABRIC", "不透明几褶还是薄纱"),
    ("GROUND", "白纸+符号还是画出的景"),
    ("FINISH", "草稿还是完成稿；能不能当组内同一套手法"),
)


def card_md(rows: list[dict]) -> str:
    lines = [
        "# 提取卡（先量后写）",
        "",
        "本文件由 `extract_source.py` 生成。数字标【实测】。卡 II 观察槽默认「未识别」，必须目测后改写。",
        "冻结卡 I / 卡 II 之前，不得编译卡 III 改图词。不得声称还原原提示词。",
        "",
        "## 卡 I · 身份普查（不迁移）",
        "",
        "发色、瞳色、耳、额记、常驻饰品写这里。禁止写进画法。",
        "",
    ]
    for r in rows:
        pal = ", ".join(f"{c['hex']} ({c['占比']})" for c in r["palette"][:8])
        ident = ", ".join(r["wd14_classes"]["身份向"]) or "（无）"
        wm = ", ".join(r["wd14_classes"]["水印向"]) or "（无）"
        tech = ", ".join(r["wd14_classes"]["技法向"]) or "（无；WD14 经常打不出技法）"
        other = ", ".join(r["wd14_classes"]["未分类"][:24]) or "（无）"
        extra = "" if len(r["wd14_classes"]["未分类"]) <= 24 else " …"
        lineart = r["lineart"] or r["lineart_error"] or "未识别"
        lines += [
            f"### {r['name']}",
            "",
            f"- 路径：`{r['path']}`",
            f"- 尺寸：{r['size'][0]}×{r['size'][1]}  mode={r['mode']}  【实测】",
            f"- 哈希：`{r['sha256_12']}`  【实测】",
            f"- 调色板：{pal}  【实测】",
            f"- 亮区色：{r['bright']}  【实测】（白纸图上这常是纸色）",
            f"- 纸白占比：{r['paper_white_pct']}%  【实测】",
            f"- 弱边缘 {r['edge_weak_pct']}% / 强边缘 {r['edge_strong_pct']}%  【实测】",
            f"- WD14 身份向：{ident}",
            f"- WD14 水印向：{wm}  （无标签仍要目检签名）",
            f"- WD14 技法向：{tech}",
            f"- WD14 未分类：{other}{extra}",
            f"- Lineart：{lineart}",
            "- 目测常驻锁：未识别（发色 / 瞳色 / 耳 / 额记 / 常驻饰品）",
            "- 本张才有：未识别",
            "- 不锁：未识别",
            "",
        ]
    lines += [
        "## 卡 II · 手法观察（每槽四列）",
        "",
        "每槽必须填：观察 / 反例（不是什么） / 组内变化 / 是否量过。看不出就保留未识别并写原因。",
        "禁止把图一的「细深线 / 平涂 / 同色相加深 / 水彩眼」粘进空槽。大眼、发色是卡 I，不是 LINE。",
        "",
        "| 槽 | 观察 | 反例 | 组内变化 | 是否量过 |",
        "|---|---|---|---|---|",
    ]
    for name, hint in SLOTS:
        measured = "否"
        if name == "VALUE":
            measured = "部分（纸白 / 亮区色 / edge，明暗档位仍要目测）"
        elif name == "GROUND":
            measured = "部分（纸白占比）"
        elif name == "LINE":
            measured = "部分（edge + Lineart 文件，线宽仍要目测）"
        lines.append(f"| {name} | 未识别（{hint}） | 未识别 | 未识别 | {measured} |")
    lines += [
        "",
        "## 裁决（人填）",
        "",
        "- 🔵 手法（可迁移）：未识别",
        "- 🟠 内容（禁搬）：未识别",
        "- 🔴 身份 / 水印：见卡 I",
        "- 🟡 半风格（默认不迁）：未识别",
        "",
        "## 卡 III · 改图编译",
        "",
        "本脚本不写卡 III。卡 I / 卡 II 冻结后，按 SKILL 从观察槽编译 STYLE，禁止感觉词。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument(
        "--model-repo",
        default="SmilingWolf/wd-eva02-large-tagger-v3",
    )
    ap.add_argument("--skip-lineart", action="store_true")
    a = ap.parse_args()
    files = collect(a.paths)
    if not files:
        print("没有可检查的图片。", file=sys.stderr)
        return 1
    if a.out_dir:
        out_dir = Path(a.out_dir)
    elif len(files) == 1:
        out_dir = files[0].parent / f"extract-{files[0].stem}"
    else:
        out_dir = files[0].parent / "extract-source"
    out_dir.mkdir(parents=True, exist_ok=True)

    from wdtagger import Tagger

    tagger = Tagger(model_repo=a.model_repo)
    rows = []
    for p in files:
        print(f"measuring {p}")
        row = measure_one(p, out_dir, tagger, a.threshold)
        if a.skip_lineart:
            row["lineart"] = None
            row["lineart_error"] = "跳过（--skip-lineart）"
        rows.append(row)
        print(
            f"  {row['sha256_12']}  {row['size'][0]}x{row['size'][1]}  "
            f"weak={row['edge_weak_pct']} strong={row['edge_strong_pct']}  "
            f"tags={len(row['wd14_tags'])} wm={row['wd14_classes']['水印向']}"
        )

    report = {
        "engine": a.model_repo,
        "threshold": a.threshold,
        "count": len(rows),
        "files": rows,
    }
    (out_dir / "measure.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "card.md").write_text(card_md(rows), encoding="utf-8")
    print(f"wrote {out_dir / 'measure.json'}")
    print(f"wrote {out_dir / 'card.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
