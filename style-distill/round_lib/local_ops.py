#!/usr/bin/env python3
"""确定性图像操作合集 —— 凡"改这一张图"的需求都走这里，不调接口。

为什么有它：本项目最贵的一类浪费，是把**确定性可解**的需求交给了生成
（实测：把"这一张的线稿调淡"交给生成白烧 3 次；把"这一张的 4K"交给生成，多花一次调用＋近一小时取回，
而本地放大 1 分钟就能给、且内容一像素不差）。规则写进了 SKILL.md 的开工四问第 0 条，
这个脚本是它的**落地工具**。

子命令：
  lighten-lines  线稿减淡（保护大块深色：黑领结、瞳孔；只抬升细线）
  upscale        放大（Lanczos ＋ **只在线条区域内**锐化，补偿插值软化）
  crop-to        裁成目标比例（可选锚点与显式裁剪带，用于"别把人脸切掉"）
  contact-sheet  多图拼版对照（等高并排，可加等分参考线）
  tone-report    调子剖面（转发 round_lib/tone_report.py）
  degrain        水彩砂粒/斑驳：丢掉各向同性颗粒，发丝窗框等定向结构留下（不调接口）

每个子命令都带**自检**：写出后校验能否完整解码、并报告实际改动范围。
"""
from __future__ import annotations
import sys as _sys
# Windows 上 stdout 默认 cp936：打印 GBK 编不出的字符会直接崩，故脚本自带 UTF-8 前置。
# 这样不依赖 PYTHONIOENCODING 环境变量，也不会影响其它程序（见 SKILL.md 的「作业纪律」）。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ---- 线稿减淡的调好参数（来自实测：先闭后开，才抓得住"网点填充的实心块"）----
# k_dilate 必须与验证过的原脚本一致 = 7；写成 k_open//2 会差 0.05 个百分点（回归实测：3.64% vs 3.59%）
LIGHTEN = dict(t_dark=0.50, k_close=5, k_open=13, k_dilate=7, t_line=0.35)
LEVELS = {"light": 0.20, "medium": 0.34, "strong": 0.50}
# ---- 放大的锐化参数 ----
UPSCALE = dict(sharpen=0.55, radius=2.0)
# ---- 水彩去砂：频率分流参数（2026-10-09 实测：medium 发丝梯度保留 94%，平坦区高频约减半；
# 整幅双边/保边滤波会把发丝一起糊掉，故不用）----
DEGRAIN = {
    "light": dict(sigma_mid=1.6, sigma_low=7.0, blotch=0.35, grain=0.85, sharp=0.18),
    "medium": dict(sigma_mid=1.8, sigma_low=8.0, blotch=0.55, grain=0.92, sharp=0.22),
    "strong": dict(sigma_mid=2.1, sigma_low=9.0, blotch=0.72, grain=0.96, sharp=0.20),
}


def _open(p: Path) -> Image.Image:
    im = Image.open(p)
    im.load()                      # 完整解码，截断文件会在这里报错
    return im.convert("RGB")


def _save_verify(im: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    chk = Image.open(out)
    chk.load()
    print(f"  写出 {out}  {im.width}x{im.height}  {out.stat().st_size/1048576:.2f} MB  ✓ 完整可解码")


def _line_mask(lum: np.ndarray, k_close: int, k_open: int, t_dark: float, t_line: float,
               k_dilate: int = 7) -> np.ndarray:
    """细线掩膜：先闭（把网点小点连成块）后开（把细线整条去掉），剩下的实心块被保护。"""
    dark = (lum < t_dark).astype(np.uint8)
    merged = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((k_close, k_close), np.uint8))
    solid = cv2.morphologyEx(merged, cv2.MORPH_OPEN, np.ones((k_open, k_open), np.uint8))
    solid_d = cv2.dilate(solid, np.ones((k_dilate, k_dilate), np.uint8))
    return (lum < t_line) & (solid_d == 0), solid


def cmd_lighten_lines(a) -> int:
    src, out = Path(a.src), Path(a.out)
    im = _open(src)
    base = np.asarray(im).astype(np.float32) / 255.0
    lum = base.mean(axis=2)
    line, solid = _line_mask(lum, LIGHTEN["k_close"], LIGHTEN["k_open"], LIGHTEN["t_dark"], LIGHTEN["t_line"])
    alpha = a.alpha if a.alpha is not None else LEVELS[a.level]
    m = line[..., None]
    res = np.clip(base + (1.0 - base) * alpha * m, 0, 1)
    print(f"  细线占比 {line.mean()*100:.2f}%  被保护的大块深色 {solid.mean()*100:.2f}%  alpha={alpha}")
    before = np.asarray(im).astype(np.int16)
    after = (res * 255).astype(np.uint8).astype(np.int16)
    d = (before != after).any(axis=2)
    brighter = int((d & (after.sum(2) > before.sum(2))).sum())
    back = after.copy(); back[d] = before[d]
    print(f"  改动像素 {int(d.sum())} / {d.size}（{d.mean()*100:.2f}%），其中变亮 {brighter}、变暗 "
          f"{int((d & (after.sum(2) < before.sum(2))).sum())}；还原后与原图逐像素一致 = {bool((back==before).all())}")
    _save_verify(Image.fromarray(after.astype(np.uint8)), out)
    return 0


def cmd_upscale(a) -> int:
    src, out = Path(a.src), Path(a.out)
    im = _open(src)
    side = a.side or max(im.size)
    if a.scale:
        side = round(max(im.size) * a.scale)
    ratio = side / max(im.size)
    target = (round(im.width * ratio), round(im.height * ratio))
    up = im.resize(target, Image.LANCZOS)
    arr = np.asarray(up).astype(np.float32) / 255.0
    lum = arr.mean(axis=2)
    k = max(3, round(LIGHTEN["k_close"] * ratio)) | 1
    ko = max(5, round(LIGHTEN["k_open"] * ratio)) | 1
    line, _ = _line_mask(lum, k, ko, 0.50, 0.45, max(3, round(LIGHTEN["k_dilate"] * ratio)) | 1)
    blur = np.asarray(up.filter(ImageFilter.GaussianBlur(UPSCALE["radius"]))).astype(np.float32) / 255.0
    sharp = np.clip(arr + a.sharpen * (arr - blur), 0, 1)
    m = line[..., None].astype(np.float32)
    res = np.clip(arr * (1 - m) + sharp * m, 0, 1)
    out_im = Image.fromarray((res * 255).astype(np.uint8))

    def grad_energy(img, mask):
        g = np.asarray(img.convert("L")).astype(np.float32)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3); gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        mag = np.hypot(gx, gy)
        mm = cv2.resize(mask.astype(np.uint8), (g.shape[1], g.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
        return float(mag[mm].mean())
    e0, e1 = grad_energy(up, line), grad_energy(out_im, line)
    print(f"  放大 {im.size} → {target}  线条掩膜 {line.mean()*100:.2f}%")
    print(f"  线条区梯度能量 {e0:.1f} → {e1:.1f}（提升 {100*(e1-e0)/max(e0,1e-6):.1f}%，说明没被插值糊掉）")
    back = out_im.resize(im.size, Image.LANCZOS)
    diff = np.abs(np.asarray(back).astype(np.int16) - np.asarray(im).astype(np.int16))
    print(f"  回缩校验（放大→缩回原尺寸）平均绝对差 {diff.mean():.2f}/255（只来自插值与锐化）")
    _save_verify(out_im, out)
    return 0


def cmd_crop_to(a) -> int:
    src, out = Path(a.src), Path(a.out)
    im = _open(src)
    W, H = im.size
    if a.band:
        x0, y0, x1, y1 = (int(v) for v in a.band.split(","))
    else:
        want = a.aspect
        if W / H > want:                       # 太宽 → 裁宽度
            nw = round(H * want); nh = H
        else:                                  # 太高 → 裁高度
            nw = W; nh = round(W / want)
        band = {"left": 0.0, "center": 0.5, "right": 1.0} if nw < W else {"top": 0.0, "center": 0.5, "bottom": 1.0}
        pos = band.get(a.anchor, 0.5)
        x0 = round((W - nw) * (pos if nw < W else 0.5))
        y0 = round((H - nh) * (pos if nh < H else 0.5))
        x1, y1 = x0 + nw, y0 + nh
    c = im.crop((x0, y0, x1, y1))
    print(f"  {im.size} → {c.size}（比例 {c.width/c.height:.3f}）  裁剪带 x{x0}-{x1} y{y0}-{y1}  锚点={a.anchor}")
    _save_verify(c, out)
    return 0


def cmd_contact_sheet(a) -> int:
    ims = [_open(Path(p)) for p in a.images]
    labels = a.labels.split(",") if a.labels else [Path(p).name for p in a.images]
    if len(labels) != len(ims):
        raise SystemExit("--labels 个数必须与图片个数一致")
    H = a.height
    panels = [(l, im.resize((max(1, round(im.width * H / im.height)), H), Image.LANCZOS)) for l, im in zip(labels, ims)]
    gap = a.gap
    W = sum(im.width for _, im in panels) + gap * (len(panels) - 1)
    canvas = Image.new("RGB", (W, H + 24), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    x = 0
    for lab, im in panels:
        canvas.paste(im, (x, 24))
        d.text((x + 4, 6), lab, fill=(200, 0, 0))
        if a.grid:
            for k in range(1, a.grid):
                y = 24 + round(H * k / a.grid)
                d.line([(x, y), (x + im.width, y)], fill=(0, 140, 255), width=1)
        x += im.width + gap
    print(f"  拼版 {len(panels)} 张，等高 {H}（{'含 ' + str(a.grid) + ' 等分参考线' if a.grid else '无参考线'}）")
    _save_verify(canvas, Path(a.out))
    return 0


def cmd_tone_report(a) -> int:
    here = Path(__file__).resolve().parent
    cmd = [sys.executable, str(here / "tone_report.py"), *a.images]
    if a.ref:
        cmd += ["--ref", a.ref]
    return subprocess.call(cmd)


def _gauss(img: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return img.astype(np.float32)
    return cv2.GaussianBlur(img.astype(np.float32), (0, 0), sigma)


def _sobel_mag(g: np.ndarray):
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return gx, gy, np.hypot(gx, gy)


def _structure_keep(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """定向结构掩膜：发丝/窗框/衣褶为 1，各向同性砂粒为 0。"""
    gx, gy, mag = _sobel_mag(gray)
    rho, rho_m = 2.2, 5.0
    jxx = _gauss(gx * gx, rho)
    jxy = _gauss(gx * gy, rho)
    jyy = _gauss(gy * gy, rho)
    tmp = np.sqrt(np.maximum((jxx - jyy) ** 2 + 4.0 * jxy * jxy, 0))
    mu1 = 0.5 * (jxx + jyy + tmp)
    mu2 = 0.5 * (jxx + jyy - tmp)
    coh = (mu1 - mu2) / (mu1 + mu2 + 1e-6)
    vx = jxy
    vy = mu1 - jxx
    n = np.sqrt(vx * vx + vy * vy) + 1e-8
    vx, vy = vx / n, vy / n
    along = np.zeros_like(gray)
    across = np.zeros_like(gray)
    yy, xx = np.indices(gray.shape)
    half, count = 6, 0
    for t in range(-half, half + 1):
        if t == 0:
            continue
        count += 1
        xa = np.clip(np.rint(xx + t * vx).astype(np.int32), 0, gray.shape[1] - 1)
        ya = np.clip(np.rint(yy + t * vy).astype(np.int32), 0, gray.shape[0] - 1)
        xb = np.clip(np.rint(xx + t * (-vy)).astype(np.int32), 0, gray.shape[1] - 1)
        yb = np.clip(np.rint(yy + t * vx).astype(np.int32), 0, gray.shape[0] - 1)
        along += np.abs(gray[ya, xa] - gray)
        across += np.abs(gray[yb, xb] - gray)
    persist = (across / count) / (along / count + 1.0)
    ves = np.zeros_like(gray)
    for sigma in (0.8, 1.2, 1.8, 2.5):
        k = max(3, int(round(sigma * 6)) | 1)
        gs = cv2.GaussianBlur(gray, (k, k), sigma)
        lx = cv2.Sobel(gs, cv2.CV_32F, 1, 0, ksize=3)
        ly = cv2.Sobel(gs, cv2.CV_32F, 0, 1, ksize=3)
        lxx = cv2.Sobel(lx, cv2.CV_32F, 1, 0, ksize=3) * (sigma ** 2)
        lxy = cv2.Sobel(lx, cv2.CV_32F, 0, 1, ksize=3) * (sigma ** 2)
        lyy = cv2.Sobel(ly, cv2.CV_32F, 0, 1, ksize=3) * (sigma ** 2)
        dt = np.sqrt(np.maximum((lxx - lyy) ** 2 + 4 * lxy * lxy, 0))
        a1 = 0.5 * (lxx + lyy + dt)
        a2 = 0.5 * (lxx + lyy - dt)
        swap = np.abs(a1) > np.abs(a2)
        lam1 = np.where(swap, a2, a1)
        lam2 = np.where(swap, a1, a2)
        rb = np.abs(lam1) / (np.abs(lam2) + 1e-6)
        s = np.sqrt(lam1 * lam1 + lam2 * lam2)
        cc = float(np.percentile(s, 90) * 0.5 + 1e-6)
        v = np.exp(-(rb ** 2) / (2 * 0.5 * 0.5)) * (1.0 - np.exp(-(s ** 2) / (2 * cc * cc)))
        ves = np.maximum(ves, v)
    ves = ves / (float(ves.max()) + 1e-6)
    fine = np.clip(coh, 0, 1) * np.clip((persist - 0.45) / 0.55, 0, 1)
    fine = np.maximum(fine, ves)
    fine = np.clip(fine * np.clip(mag / 16.0, 0, 1), 0, 1)
    fine = np.maximum(fine, 0.65 * cv2.dilate(fine, np.ones((3, 3), np.uint8)))
    jxx_m = _gauss(gx * gx, rho_m)
    jxy_m = _gauss(gx * gy, rho_m)
    jyy_m = _gauss(gy * gy, rho_m)
    tmp_m = np.sqrt(np.maximum((jxx_m - jyy_m) ** 2 + 4.0 * jxy_m * jxy_m, 0))
    mu1_m = 0.5 * (jxx_m + jyy_m + tmp_m)
    mu2_m = 0.5 * (jxx_m + jyy_m - tmp_m)
    coh_m = (mu1_m - mu2_m) / (mu1_m + mu2_m + 1e-6)
    energy_m = np.sqrt(np.maximum(mu1_m, 0))
    mid = np.clip((coh_m - 0.35) / 0.50, 0, 1) * np.clip(energy_m / 12.0, 0, 1)
    mid = np.maximum(mid, np.clip((mag - 22) / 40, 0, 1))
    return fine.astype(np.float32), mid.astype(np.float32)


def cmd_degrain(a) -> int:
    """频率分流去砂：低频色块全留；中频斑驳/高频砂只在定向结构上加回。"""
    src, out = Path(a.src), Path(a.out)
    im = _open(src)
    bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR).astype(np.float32)
    gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY).astype(np.float32)
    p = DEGRAIN[a.level]
    fine, mid = _structure_keep(gray)
    low = _gauss(bgr, p["sigma_low"])
    midb = _gauss(bgr, p["sigma_mid"])
    mid_band = midb - low
    high = bgr - midb
    blotch, grain, sharp = p["blotch"], p["grain"], p["sharp"]
    mid_out = mid_band * ((1.0 - blotch) + blotch * mid[..., None])
    high_out = high * ((1.0 - grain) + grain * fine[..., None])
    res = low + mid_out + high_out
    if sharp > 0:
        res = res + sharp * (res - _gauss(res, 0.75)) * fine[..., None]
    res_u8 = np.clip(res, 0, 255).astype(np.uint8)
    out_im = Image.fromarray(cv2.cvtColor(res_u8, cv2.COLOR_BGR2RGB))

    gx0, gy0, mag0 = _sobel_mag(gray)
    g1 = cv2.cvtColor(res_u8, cv2.COLOR_BGR2GRAY).astype(np.float32)
    _, _, mag1 = _sobel_mag(g1)
    hf0 = np.abs(gray - _gauss(gray, 1.2))
    hf1 = np.abs(g1 - _gauss(g1, 1.2))
    flat = mag0 < np.percentile(mag0, 40)
    hair = (fine > 0.55) & (mag0 > np.percentile(mag0, 80))
    edge = mag0 > np.percentile(mag0, 85)
    mse = float(np.mean((g1 - gray) ** 2))
    print(f"  档位 {a.level}  定向结构 {fine.mean()*100:.1f}%  中频结构 {mid.mean()*100:.1f}%")
    print(f"  平坦区高频 {hf0[flat].mean():.3f} → {hf1[flat].mean():.3f}（砂应下降）")
    print(f"  发丝区梯度保留 {mag1[hair].mean()/(mag0[hair].mean()+1e-6):.3f}  "
          f"强边保留 {mag1[edge].mean()/(mag0[edge].mean()+1e-6):.3f}  MSE {mse:.2f}")
    _save_verify(out_im, out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性图像操作（不调接口）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lighten-lines", help="线稿减淡，保护大块深色")
    p.add_argument("src"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--level", choices=list(LEVELS), default="light")
    p.add_argument("--alpha", type=float, default=None, help="直接给强度（0=不动，1=线变白）")
    p.set_defaults(fn=cmd_lighten_lines)

    p = sub.add_parser("upscale", help="放大（只在线条区锐化）")
    p.add_argument("src"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--side", type=int, default=None, help="目标长边像素")
    p.add_argument("--scale", type=float, default=None, help="或按倍数")
    p.add_argument("--sharpen", type=float, default=UPSCALE["sharpen"])
    p.set_defaults(fn=cmd_upscale)

    p = sub.add_parser("crop-to", help="裁成目标比例")
    p.add_argument("src"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--aspect", type=float, default=1.0, help="宽/高，例如 1.5；默认 1.0（方形）")
    p.add_argument("--anchor", default="center", choices=["top", "center", "bottom", "left", "right"])
    p.add_argument("--band", default="", help="直接给裁剪带 x0,y0,x1,y1（原图像素）")
    p.set_defaults(fn=cmd_crop_to)

    p = sub.add_parser("contact-sheet", help="多图拼版对照")
    p.add_argument("images", nargs="+"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--height", type=int, default=400)
    p.add_argument("--labels", default="")
    p.add_argument("--gap", type=int, default=16)
    p.add_argument("--grid", type=int, default=0, help="叠加 N 等分参考线")
    p.set_defaults(fn=cmd_contact_sheet)

    p = sub.add_parser("tone-report", help="调子剖面（转发 tone_report.py）")
    p.add_argument("images", nargs="+"); p.add_argument("--ref", default="")
    p.set_defaults(fn=cmd_tone_report)

    p = sub.add_parser("degrain", help="水彩砂粒/斑驳：定向结构留下，各向同性颗粒丢掉")
    p.add_argument("src"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--level", choices=list(DEGRAIN), default="medium")
    p.set_defaults(fn=cmd_degrain)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
