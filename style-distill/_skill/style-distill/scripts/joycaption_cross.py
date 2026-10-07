"""JoyCaption Danbooru 反推 × WD14 交叉验证（验收第 6.5 步，可选）。

用法：
    python joycaption_cross.py <图片> [--threshold 0.35] [--out report.json]

输出：JoyCaption Danbooru 分层反推 ＋ WD14 标签，两者交叉对比——
    共同＝双源一致（可信）；仅单源＝分歧标签，人工复查点。

依赖：pip install wdtagger gradio_client requests
注意：调用 HuggingFace Space（免费），走本机代理时自动注入 HTTP(S)_PROXY。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")

import requests  # noqa: E402
from PIL import Image  # noqa: E402

BASE = "https://fancyfeast-joy-caption-beta-one.hf.space"
PROXY = {"http": os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"),
         "https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:7890")}
s = requests.Session()
s.proxies = PROXY
EXTRA = [
    "Include information about lighting.",
    "If there is a watermark, you must mention it.",
    "Do NOT use any ambiguous language.",
    "ONLY describe the most important elements of the image.",
]


def call_sse(endpoint: str, data: list, timeout: int = 300) -> list:
    r = s.post(f"{BASE}/gradio_api/call/{endpoint}", json={"data": data}, timeout=60)
    r.raise_for_status()
    event_id = r.json()["event_id"]
    cmd = ["curl.exe", "-sS", "-x", PROXY["https"],
           f"{BASE}/gradio_api/call/{endpoint}/{event_id}", "--max-time", str(timeout)]
    out = subprocess.run(cmd, capture_output=True)
    sse = out.stdout.decode("utf-8", "replace")
    results = []
    for line in sse.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[COMPLETE]":
                break
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return results


def joycaption_danbooru(image_path: Path) -> list:
    built = call_sse("build_prompt", ["Danbooru tag list", "long", EXTRA, ""], timeout=60)
    prompt = built[-1][0] if built and isinstance(built[-1], list) else None
    if not prompt:
        raise RuntimeError("build_prompt failed")
    r = s.post(f"{BASE}/gradio_api/upload",
               files={"files": ("image.png", image_path.read_bytes(), "image/png")}, timeout=120)
    r.raise_for_status()
    server_path = r.json()[0]
    infer = call_sse("chat_joycaption", [
        {"path": server_path, "meta": {"_type": "gradio.FileData"},
         "orig_name": "image.png", "mime_type": "image/png"},
        prompt, 0.6, 0.9, 512, False,
    ], timeout=300)
    raw = infer[-1][0] if infer and isinstance(infer[-1], list) and infer[-1] else ""
    return [t.strip() for t in raw.replace("copyright:", "").replace("meta:", "").split(",") if t.strip()]


def wd14_tags(image_path: Path, threshold: float = 0.35) -> set:
    from wdtagger import Tagger
    result = Tagger().tag(Image.open(image_path).convert("RGB"))
    return {t for t, p in result.general_tag_data.items() if p >= threshold}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    image_path = Path(a.image)
    jc = joycaption_danbooru(image_path)
    wd = wd14_tags(image_path)

    report = {
        "JoyCaption反推": sorted(jc),
        "WD14标签": sorted(wd),
        "交叉对比": {
            "双源一致": sorted({t.replace("_", " ") for t in jc} & {w.replace("_", " ") for w in wd}),
            "仅JoyCaption": sorted(jc),
            "仅WD14": sorted(wd),
        },
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
