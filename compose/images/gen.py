"""Direct image-to-image call to the relay, driven from the command line.

Reuses the app's own payload builders so what we send here is byte-for-byte
what the UI would send.

凭据只从环境变量读取（2026-09-25 起，原先写死的 key 已清除）：
    RELAY_API_KEY    必填，没有就明确报错并给出设置方法
    RELAY_BASE_URL   选填，默认 https://image-direct.geiliapi.com/v1
    RELAY_MODEL      选填，默认 gpt-image-2
密钥不写入任何文件、不进仓库。

Usage:
    python gen.py <image> <prompt-file|prompt-text> [--mode whole|mask]
                  [--size 1024x1536] [--quality low] [--pad crop]
                  [--protect x0,y0,x1,y1] [--out out.png]

Examples:
    python gen.py base_v1.png "改成灰绿调" --mode whole
    python gen.py base_v1.png "换背景" --mode mask --protect 230,150,640,1243
"""
import argparse
import base64
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mask_edit_app as app
from image_payload import normalise_to_size

# Transport credentials are process-env only. Do not consult user config or registry.
BASE = os.environ.get("RELAY_BASE_URL", "https://image-direct.geiliapi.com/v1").rstrip("/")
KEY = os.environ.get("RELAY_API_KEY", "")
KEY_HD = os.environ.get("RELAY_API_KEY_HD", "") or KEY
MODEL = os.environ.get("RELAY_MODEL", "gpt-image-2")


def key_for(quality: str) -> str:
    """按档位选 key：low → KEY（1K）；medium/high → KEY_HD（高档）。

    为什么要分：用户给了两个 key，明确说第一个是 1K 用的。
    ⚠️ 两个 key 与**端点**是配套的——换 key 必须同时换 RELAY_BASE_URL，
    否则会全是 401（实测：新 key 在 api-slb.micuapi.ai 上 401、在 geiliapi 上 200）。
    """
    return KEY_HD if quality in ("medium", "high") else KEY
SIZES = {
    "low":    ["1024x1536", "1024x1024", "1536x1024"],
    "medium": ["1152x2048", "2048x2048", "2048x1152"],
    "high":   ["2160x3840", "2880x2880", "3840x2160"],
}


def build_whole(src, tw, th, pad, style=None):
    fit = normalise_to_size(src, tw, th, pad)
    b = io.BytesIO(); fit.save(b, "PNG", optimize=True)
    files = {"image": ("image.png", b.getvalue(), "image/png")}
    # Extra reference images ride the documented image[1..3] fields. This relay
    # has NO mask field at all; multi-image is its real conditioning mechanism.
    for i, s in enumerate(style or [], start=1):
        sfit = app.normalise_to_size(s, tw, th, pad)
        sb = io.BytesIO(); sfit.save(sb, "PNG", optimize=True)
        files[f"image[{i}]"] = (f"style{i}.png", sb.getvalue(), "image/png")
    return files, fit


def build_masked(src, protect, tw, th, pad):
    """protect = (x0,y0,x1,y1) in ORIGINAL coords, kept untouched.
    Everything OUTSIDE it is painted as editable."""
    W, H = src.size
    mx = np.zeros((H, W, 4), np.uint8)
    mx[:, :, 3] = 255
    x0, y0, x1, y1 = protect
    mx[y0:y1, x0:x1, 3] = 0
    canvas_mask = Image.fromarray(mx, "RGBA")

    vis_png, pad_paint = app.make_visual_mask(src, canvas_mask, tw, th, pad, invert=False)
    if not pad_paint.any():
        raise SystemExit("mask is empty - protect rectangle covers nothing")
    alpha = app.build_mask_alpha(pad_paint)
    fit = normalise_to_size(src, tw, th, pad)
    b = io.BytesIO(); fit.save(b, "PNG", optimize=True)
    return ({"image": ("image.png", b.getvalue(), "image/png"),
             "image[1]": ("mask_ref.png", vis_png, "image/png"),
             "mask": ("mask.png", alpha, "image/png")},
            fit, float(pad_paint.mean()) * 100)


def require_config() -> None:
    """凭据只从环境变量读，任何位置都不落盘。

    **故意抛 SystemExit 而不是返回错误码**：`run_round.post_with_retry` 用
    `except Exception` 把异常映射成 `-1` 再重试 3 次，而缺少凭据是配置错误、
    重试毫无意义。SystemExit 继承自 BaseException，不会被那个 except 吞掉，
    于是能一路冒泡、带着原因直接终止。
    """
    if not KEY:
        raise SystemExit(
            "未配置 API Key。请先设置环境变量再重跑（密钥只从环境变量读取，不写入任何文件）：\n"
            "  PowerShell:  $env:RELAY_API_KEY = 'sk-...'\n"
            "  永久:        [Environment]::SetEnvironmentVariable('RELAY_API_KEY','sk-...','User')\n"
            "  可选覆盖:    RELAY_BASE_URL（默认 %s）、RELAY_MODEL（默认 %s）" % (
                "https://image-direct.geiliapi.com/v1", "gpt-image-2"))


def call_structured(fields, files, timeout=900, key=None, *, marker=None,
                    report_path=None, opener=None, operation="edit", endpoint=None):
    """Submit exactly once; operation=generate uses JSON generations endpoint."""
    import image_transport as T
    started = time.monotonic()
    try:
        selected = KEY if key is None else key
        if not selected:
            result = T.CallResult(classification="missing_config")
            T.write_report(report_path, result)
            return result
        endpoint = endpoint or ("/images/generations" if operation == "generate" else "/images/edits")
        if operation == "generate":
            body = json.dumps(dict(fields), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            req = urllib.request.Request(BASE + endpoint, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
        else:
            bnd = ("----Gen" + uuid.uuid4().hex).encode()
            body = app.build_multipart(fields, files, bnd)
            req = urllib.request.Request(BASE + endpoint, data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={bnd.decode()}")
        req.add_header("Authorization", "Bearer " + selected)
        req.add_header("User-Agent", T.BROWSER_UA)
        req.add_header("Accept", "application/json")
    except Exception:
        result = T.CallResult(classification="local_error",
                              elapsed_s=round(time.monotonic() - started, 3))
        T.write_report(report_path, result)
        return result
    return T.post_once(req, timeout=timeout, marker=marker,
                       report_path=report_path, opener=opener)


def call(fields, files, timeout=900, key=None):
    """Compatible (status, text) interface; exactly one network invocation."""
    return call_structured(fields, files, timeout=timeout, key=key).legacy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("prompt")
    ap.add_argument("--mode", choices=["whole", "mask"], default="whole")
    ap.add_argument("--size", default="1024x1536")
    ap.add_argument("--quality", default="low")
    ap.add_argument("--pad", default="crop", choices=["pad", "crop"])
    ap.add_argument("--protect", default="", help="x0,y0,x1,y1 in original pixels")
    ap.add_argument("--out", default="")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--style", default="", help="style reference image -> sent as image[1]")
    a = ap.parse_args()

    if a.size not in SIZES[a.quality]:
        print(f"WARNING: {a.size} is not in the {a.quality} whitelist {SIZES[a.quality]}")

    src = Image.open(a.image).convert("RGB")
    W, H = src.size
    prompt = a.prompt
    if os.path.isfile(prompt):
        prompt = open(prompt, encoding="utf-8").read().strip()
    tw, th = (int(v) for v in a.size.split("x"))

    print(f"source  : {a.image}  {W}x{H}")
    print(f"target  : {a.size}  quality={a.quality}  pad={a.pad}  mode={a.mode}")
    print(f"model   : {a.model}")
    print(f"prompt  : {prompt[:90]}{'...' if len(prompt) > 90 else ''}")

    if a.mode == "whole":
        style_imgs = []
        if a.style:
            style_imgs.append(Image.open(a.style).convert("RGB"))
            print(f"style   : {a.style}  {style_imgs[0].size}")
        files, fit = build_whole(src, tw, th, a.pad, style=style_imgs)
        fields = {"model": a.model, "prompt": prompt, "n": "1",
                  "size": a.size, "quality": a.quality}
        painted = None
    else:
        if not a.protect:
            raise SystemExit("--mode mask needs --protect x0,y0,x1,y1")
        protect = tuple(int(v) for v in a.protect.split(","))
        print(f"protect : {protect} (kept untouched)")
        files, fit, painted = build_masked(src, protect, tw, th, a.pad)
        fields = {"model": a.model, "prompt": app.SCHEMA_PROMPT + prompt, "n": "1",
                  "size": a.size, "quality": a.quality}
        print(f"editable: {painted:.1f}% of frame")

    import image_transport as T
    from pathlib import Path
    out = Path(a.out or ("out_" + os.path.splitext(os.path.basename(a.image))[0] + ".png"))
    report = str(out) + ".transport.json"
    if out.exists():
        print("FAILED: output_exists")
        return 1
    result = call_structured(fields, files, key=key_for(a.quality),
                             marker=str(out) + ".submission", report_path=report)
    if result.classification != "response_complete":
        print("FAILED: " + result.classification)
        return 1
    T.private_snapshot(str(out) + ".response.private.json", result.text, secrets=(KEY, KEY_HD))
    try:
        item = T.result_item(result.text)
        result.phase = "decode_image"
        if item.get("b64_json"):
            T.save_b64(item["b64_json"], out)
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "style-distill" / "round_lib"))
            from run_round import download
            result.phase = "download"
            download(item["url"], out)
    except T.ResultError as exc:
        result.classification = str(exc)
        T.write_report(report, result)
        print("FAILED: " + result.classification)
        return 1
    result.phase = "saved"
    result.classification = "saved"
    T.write_report(report, result)
    res = Image.open(out).convert("RGB")
    print(f"\n   saved: {out}   {res.size}   {out.stat().st_size//1024} KB")

    # ---- numeric report (no human eye needed) ----
    ref = fit.convert("RGB")
    A = np.asarray(ref.resize(res.size)).astype(float)
    B = np.asarray(res).astype(float)
    diff = np.abs(A - B).mean(axis=2)
    print(f"   平均差异: {diff.mean():.1f}   变化>12 的面积: {(diff>12).mean()*100:.1f}%")

    def stats(arr, m, label):
        px = arr[m]
        if not len(px):
            return
        print(f"   {label:22s} R{px[:,0].mean():6.1f} G{px[:,1].mean():6.1f} B{px[:,2].mean():6.1f}"
              f"  彩度(R-B) {np.abs(px[:,0]-px[:,2]).mean():5.1f}")

    if painted is not None:
        # rebuild the protect mask in output coords
        W2, H2 = W, H
        px0, py0, px1, py1 = protect
        pm = np.zeros((H2, W2), bool); pm[py0:py1, px0:px1] = True
        ar, tr = W2 / H2, res.size[0] / res.size[1]
        if ar > tr:
            nw = int(H2 * tr); box = ((W2 - nw)//2, 0, (W2 - nw)//2 + nw, H2)
        else:
            nh = int(W2 / tr); box = (0, (H2 - nh)//2, W2, (H2 - nh)//2 + nh)
        pmr = np.asarray(Image.fromarray((pm*255).astype(np.uint8)).crop(box).resize(res.size, Image.NEAREST)) > 127
        print(f"   保护区(应当保持) 平均差异 {diff[pmr].mean():.1f}   未变>88%: {(diff[pmr]<12).mean()*100:.1f}%")
        print(f"   可改区           平均差异 {diff[~pmr].mean():.1f}")
        stats(A, pmr, "原图 保护区")
        stats(B, pmr, "结果 保护区")

    # cast check
    cast = B.reshape(-1, 3).mean(axis=0)
    print(f"   全图均值: R{cast[0]:.0f} G{cast[1]:.0f} B{cast[2]:.0f}"
          f"  -> {'灰绿/橄榄' if cast[1] >= cast[2] else '偏冷蓝'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
