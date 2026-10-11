"""画风蒸馏:把一张或多张参考图拆解成"去内容的画风证据包"。

三类样例(P12):
  exemplar-<i>-stroke-*.png  笔触样例:细线/勾线高频纹理区(低泄漏风险)
  exemplar-<i>-fabric-*.png  材质样例:织物/大色块渐变区(低泄漏风险)
  exemplar-<i>-face-*.png    五官样例:面部画法区(高风险,必须人工批准)

其余产物:
  measure.json / lineart-<i>.png / tone.json(多图池化)/ style-card.md /
  model_prompt.md(无 hex、无人工段)/ manifest.json / package.json

引擎全部本地,不请求上游。OpenPose 面部关键点用于人脸定位(动漫可用),
Haar 级联作回退;两者都不可用时 face 类为空并标记 detector=none。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
EXTRACT = HERE.parent.parent / "style-distill" / "_skill" / "style-distill" / "scripts" / "extract_source.py"

_pose_cache: dict = {}


# ---- 量测(复用 style-distill 的 extract_source) ----

def run_measure(image: Path, out_dir: Path) -> dict:
    # 不加 --skip-lineart:蒸馏包需要线稿层
    cmd = [sys.executable, str(EXTRACT), str(image), "--out-dir", str(out_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, creationflags=_CREATE_NO_WINDOW)
    measure = out_dir / "measure.json"
    if proc.returncode != 0 or not measure.is_file():
        raise RuntimeError(f"量测失败:{(proc.stdout or '') + (proc.stderr or '')}"[-800:])
    return json.loads(measure.read_text(encoding="utf-8"))


def pick_lineart(image: Path, sub_dir: Path, out_dir: Path, idx: int) -> str | None:
    """对单图跑 LineartDetector 输出线稿层(经 extract_source 的 lineart 产物)。"""
    for p in sorted(sub_dir.glob("lineart-*.png")):
        dest = out_dir / f"lineart-{idx:02d}.png"
        p.replace(dest)
        return str(dest)
    return None


# ---- 人脸定位:OpenPose 面部关键点优先,Haar 回退 ----

def _get_openpose():
    if "det" not in _pose_cache:
        try:
            from controlnet_aux import OpenposeDetector
            _pose_cache["det"] = OpenposeDetector.from_pretrained("lllyasviel/Annotators")
        except Exception:
            _pose_cache["det"] = None
    return _pose_cache["det"]


def _face_boxes(image: Path) -> tuple[list[tuple[int, int, int, int]], str]:
    try:
        det = _get_openpose()
        if det is not None:
            with Image.open(image) as im:
                W, H = im.size
                arr = np.asarray(im.convert("RGB"))
            poses = det.detect_poses(arr, include_face=True, include_hand=False)
            boxes = []
            for pose in poses:
                face = pose.face
                if face is None or len(face) == 0:
                    continue
                xs, ys = [], []
                for k in face:
                    try:
                        xs.append(float(k.x)); ys.append(float(k.y))
                    except AttributeError:
                        xs.append(float(k[0])); ys.append(float(k[1]))
                if not xs:
                    continue
                x0, x1, y0, y1 = min(xs) * W, max(xs) * W, min(ys) * H, max(ys) * H
                pad = 0.2 * max(x1 - x0, y1 - y0, 1)
                boxes.append((int(max(0, x0 - pad)), int(max(0, y0 - pad)),
                              int(min(W, x1 + pad) - max(0, x0 - pad)), int(min(H, y1 + pad) - max(0, y0 - pad))))
            if boxes:
                return boxes, "openpose"
    except Exception:
        pass
    try:
        import cv2
        bgr = cv2.imread(str(image))
        if bgr is None:
            return [], "none"
        cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        faces = cascade.detectMultiScale(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 1.1, 4)
        return [(int(x), int(y), int(w), int(h)) for x, y, w, h in faces], "haar"
    except Exception:
        return [], "none"


def _overlaps(box: tuple[int, int, int, int], boxes) -> bool:
    x, y, w, h = box
    for fx, fy, fw, fh in boxes:
        if x < fx + fw and x + w > fx and y < fy + fh and y + h > fy:
            return True
    return False


# ---- 三类样例选区 ----

def _grad_and_colorvar(rgb: np.ndarray):
    gray = rgb.mean(axis=2)
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    grad = gx + gy
    colorvar = rgb.std(axis=2)  # 每像素通道间差异:灰底≈0,彩色区高
    return grad, colorvar


def _integral(m):
    ii = np.cumsum(np.cumsum(m, axis=0), axis=1)
    return np.pad(ii, ((1, 0), (1, 0)))


def _win_sum(ii, y0, x0, ps):
    return ii[y0 + ps, x0 + ps] - ii[y0, x0 + ps] - ii[y0 + ps, x0] + ii[y0, x0]


def pick_exemplars_classified(image: Path, out_dir: Path, idx: int,
                              face_boxes: list, detector: str,
                              k_stroke: int = 2, k_fabric: int = 2, patch: int = 384) -> dict:
    rgb = np.asarray(Image.open(image).convert("RGB"), dtype=np.float32)
    H, W, _ = rgb.shape
    grad, colorvar = _grad_and_colorvar(rgb)
    ig, ic = _integral(grad), _integral(colorvar)
    ps = min(patch, H, W)
    step = max(16, ps // 8)
    cands = []
    for y in range(0, H - ps + 1, step):
        for x in range(0, W - ps + 1, step):
            cands.append({"y": y, "x": x, "grad": _win_sum(ig, y, x, ps) / (ps * ps),
                          "cvar": _win_sum(ic, y, x, ps) / (ps * ps)})

    def free(c, taken):
        return not any(abs(c["y"] - ty) < ps and abs(c["x"] - tx) < ps for ty, tx in taken)

    picked: dict[str, list] = {"stroke": [], "fabric": [], "face": []}
    taken: list[tuple[int, int]] = []
    # 笔触:细线高频纹理最密处(排除人脸)
    for c in sorted(cands, key=lambda c: -c["grad"]):
        if len(picked["stroke"]) >= k_stroke:
            break
        if _overlaps((c["x"], c["y"], ps, ps), face_boxes) or not free(c, taken):
            continue
        picked["stroke"].append(c); taken.append((c["y"], c["x"]))
    # 材质:剩余窗口里通道间色彩差异最大处(织物/点缀色),排除人脸与已选
    for c in sorted((c for c in cands if free(c, taken)), key=lambda c: -c["cvar"]):
        if len(picked["fabric"]) >= k_fabric:
            break
        if _overlaps((c["x"], c["y"], ps, ps), face_boxes):
            continue
        picked["fabric"].append(c); taken.append((c["y"], c["x"]))
    # 五官:人脸框中心裁片(高风险类;无人脸检测则该类为空)
    im = Image.open(image).convert("RGB")
    for fi, (fx, fy, fw, fh) in enumerate(face_boxes):
        cx, cy = fx + fw // 2, fy + fh // 2
        half = min(patch, H, W) // 2
        x0 = int(min(max(0, cx - half), max(0, W - ps)))
        y0 = int(min(max(0, cy - half), max(0, H - ps)))
        dest = out_dir / f"exemplar-{idx:02d}-face-{fi}.png"
        im.crop((x0, y0, min(W, x0 + 2 * half), min(H, y0 + 2 * half))).save(dest)
        picked["face"].append({"path": str(dest), "box": [fx, fy, fw, fh]})

    out = {"stroke": [], "fabric": [], "face": []}
    for cls, items in picked.items():
        for j, c in enumerate(items):
            if cls == "face":
                dest = out_dir / f"exemplar-{idx:02d}-face-{j}.png"
            else:
                dest = out_dir / f"exemplar-{idx:02d}-{cls}-{j}.png"
                im.crop((c["x"], c["y"], c["x"] + ps, c["y"] + ps)).save(dest)
            out[cls].append({"path": str(dest), "cls": cls,
                             "identity_risk": "high" if cls == "face" else "low"})
    return {"exemplars": out, "detector": detector,
            "face_found": len(face_boxes) > 0,
            "risk_detail": {"detector": detector, "face_windows": len(face_boxes)}}


# ---- 色调指纹与对色 ----

def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    lin = c @ np.array([[0.4124, 0.3576, 0.1805],
                        [0.2126, 0.7152, 0.0722],
                        [0.0193, 0.1192, 0.9505]]).T
    xyz = lin / np.array([0.9505, 1.0, 1.0890])
    eps, kap = 216 / 24389, 24389 / 27
    f = np.where(xyz > eps, np.cbrt(xyz), (kap * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def tone_stats(image: Path) -> dict:
    rgb = np.asarray(Image.open(image).convert("RGB"), dtype=np.float32)
    lab = _srgb_to_lab(rgb).reshape(-1, 3)
    mean, std = lab.mean(axis=0), lab.std(axis=0)
    return {"mean": [round(v, 3) for v in mean], "std": [round(v, 3) for v in std]}


def apply_tone(src: Path, tone_json: Path, dest: Path) -> Path:
    st = json.loads(tone_json.read_text(encoding="utf-8"))["tone"]
    rgb = np.asarray(Image.open(src).convert("RGB"), dtype=np.float32)
    lab = _srgb_to_lab(rgb)
    m_in, s_in = lab.reshape(-1, 3).mean(axis=0), lab.reshape(-1, 3).std(axis=0)
    m_t, s_t = np.array(st["mean"]), np.array(st["std"])
    out_lab = (lab - m_in) * (s_t / np.maximum(s_in, 1e-6)) + m_t
    L, A, B = out_lab[..., 0], out_lab[..., 1], out_lab[..., 2]
    fy = (L + 16) / 116
    fx, fz = fy + A / 500, fy - B / 200
    eps, kap = 216 / 24389, 24389 / 27
    fxyz = np.stack([np.where(fx**3 > eps, fx**3, (116 * fx - 16) / kap),
                     np.where(fy**3 > eps, fy**3, L / kap),
                     np.where(fz**3 > eps, fz**3, (116 * fz - 16) / kap)], axis=-1)
    xyz = fxyz * np.array([0.9505, 1.0, 1.0890])
    lin = xyz @ np.array([[3.2406, -1.5372, -0.4986],
                          [-0.9689, 1.8758, 0.0415],
                          [0.0557, -0.2040, 1.0570]]).T
    srgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.clip(lin, 0, None) ** (1 / 2.4) - 0.055)
    Image.fromarray((np.clip(srgb, 0, 1) * 255).astype(np.uint8)).save(dest)
    return dest


# ---- 画风卡编译 ----

def _strip_hex(s: str) -> str:
    s = re.sub(r"#([0-9a-fA-F]{6})", "", s)
    return re.sub(r"（\s*）|\(\s*\)", "", s)


def _model_card(rows: list[dict], n_samples: int) -> str:
    sys.path.insert(0, str(HERE))
    import board_app
    if len(rows) == 1:
        text = board_app.compile_extract_text("style", rows[0], "参考图")
    else:
        agg = aggregate_rows(rows)
        text = board_app.compile_extract_text("style", agg, f"{n_samples}张样本聚合")
        weak = [r.get("edge_weak_pct") for r in rows if isinstance(r.get("edge_weak_pct"), (int, float))]
        if len(weak) > 1:
            text += (f"\n\nMULTI(N={len(rows)})\n- 跨图波动【实测】:弱边缘 {min(weak)}%~{max(weak)}%;"
                     "波动大说明样本画风不稳定,需人工复核。\n- 每图量测见 src-N/measure.json。")
    blocks = [b for b in text.split("\n\n") if not b.startswith("DO NOT INCLUDE")]
    lines = []
    for line in "\n\n".join(blocks).splitlines():
        if line.strip().startswith("- 观察调色板"):
            lines.append("- 色彩气氛:见人工复核底稿(精确色板不进模型文本)。")
        else:
            lines.append(_strip_hex(line))
    model = "\n".join(lines).replace("亮区色 （", "亮区色接近纯白（")
    return ("MODEL_PROMPT\n" + model +
            "\n\nDISTILLED\n- 蒸馏样例与线稿审核后才可作为附加参考。\n- 对色需显式授权。\n")


def _human_review(rows: list[dict]) -> str:
    ident, tags = [], []
    for r in rows:
        ident += (r.get("wd14_classes") or {}).get("身份向", [])
        tags += r.get("wd14_tags", [])
    seen = []
    for t in ident + tags:
        if t not in seen:
            seen.append(t)
    return ("HUMAN_REVIEW (不发送给模型)\n- 身份泄漏对照: " + ", ".join(seen[:60]) +
            "\n- 精确 hex 见 src-N/measure.json。\n- 样例默认未审核;逐张确认后批准。\n")


def aggregate_rows(rows: list[dict]) -> dict:
    def avg(key):
        vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    pool: dict[str, float] = {}
    for r in rows:
        for c in r.get("palette") or []:
            pool[c["hex"]] = pool.get(c["hex"], 0.0) + float(c.get("占比", 0)) / len(rows)
    palette = [{"hex": h, "占比": round(v, 3)} for h, v in sorted(pool.items(), key=lambda kv: -kv[1])[:6]]

    def union(key, cap=None):
        out = []
        for r in rows:
            for t in (r.get("wd14_classes") or {}).get(key, []):
                if t not in out:
                    out.append(t)
        return out[:cap] if cap else out

    tags = []
    for r in rows:
        for t in r.get("wd14_tags") or []:
            if t not in tags:
                tags.append(t)
    brights = [r.get("bright") for r in rows if r.get("bright")]
    bright = max(set(brights), key=brights.count) if brights else None
    return {"palette": palette,
            "edge_weak_pct": avg("edge_weak_pct"), "edge_strong_pct": avg("edge_strong_pct"),
            "paper_white_pct": avg("paper_white_pct"), "bright": bright,
            "size": rows[0].get("size") if rows else [0, 0],
            "wd14_classes": {"身份向": union("身份向"), "水印向": union("水印向"),
                              "技法向": union("技法向"), "未分类": union("未分类", 24)},
            "wd14_tags": tags[:60]}


def _pool_tone(tones: list[dict], sizes: list[int]) -> dict:
    total = sum(sizes) or 1
    mean = [sum(t["mean"][c] * s for t, s in zip(tones, sizes)) / total for c in range(3)]
    var = [sum((t["std"][c] ** 2 + t["mean"][c] ** 2) * s for t, s in zip(tones, sizes)) / total - mean[c] ** 2
           for c in range(3)]
    return {"mean": [round(v, 3) for v in mean], "std": [round(max(v, 0.0) ** 0.5, 3) for v in var]}


# ---- 主流程:单图/多图统一走 distill_images ----

def distill_images(images: list[Path], out_dir: Path, k_stroke: int, k_fabric: int, patch: int) -> dict:
    per, risks = [], []
    for i, img in enumerate(images):
        sub = out_dir / f"src-{i}"
        sub.mkdir(parents=True, exist_ok=True)
        measure = run_measure(img, sub)
        lineart = pick_lineart(img, sub, out_dir, i)
        with Image.open(img) as sim:
            W, H = sim.size
        face_boxes, detector = _face_boxes(img)
        cls = pick_exemplars_classified(img, out_dir, i, face_boxes, detector,
                                        k_stroke=k_stroke, k_fabric=k_fabric, patch=patch)
        exs = [e for group in cls["exemplars"].values() for e in group]
        risk = "high" if cls["exemplars"]["face"] else \
               ("low" if detector != "none" else "unknown")
        risks.append(risk)
        per.append({"image": str(img), "hash": hashlib.sha256(img.read_bytes()).hexdigest(),
                    "measure_dir": str(sub), "lineart": lineart, "exemplars": exs,
                    "tone": tone_stats(img), "pixels": W * H, "identity_risk": risk,
                    "row": (measure.get("files") or [{}])[0]})
    rows = [p["row"] for p in per]
    tone = _pool_tone([p["tone"] for p in per], [p["pixels"] for p in per])
    tone_path = out_dir / "tone.json"
    tone_path.write_text(json.dumps({"tone": tone, "sources": [p["image"] for p in per]},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    card_path = out_dir / "style-card.md"
    model_path = out_dir / "model_prompt.md"
    if rows:
        model_path.write_text(_model_card(rows, len(rows)), encoding="utf-8")
        card_path.write_text(model_path.read_text(encoding="utf-8") +
                             "\n\nHUMAN_REVIEW (不发送给模型)\n" + _human_review(rows), encoding="utf-8")
    all_exemplars = [{"path": e["path"], "cls": e["cls"], "identity_risk": e["identity_risk"]}
                     for p in per for e in p["exemplars"]]
    identity_risk = "high" if any(e["cls"] == "face" for e in all_exemplars) else \
                    ("unknown" if "unknown" in risks else "low")
    manifest_path = out_dir / "manifest.json"
    manifest = {"sources": [p["image"] for p in per],
                "source_hashes": [p["hash"] for p in per],
                "style_scope": "transfer", "color_scope": "preserve",
                "exemplars_reviewed": False, "identity_risk": identity_risk,
                "risk_detail": risks, "approved": False}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "out_dir": str(out_dir), "sources": [p["image"] for p in per],
            "source_count": len(images),
            "linearts": [p["lineart"] for p in per if p["lineart"]],
            "exemplars": all_exemplars,
            "tone_json": str(tone_path), "card": str(card_path), "model_prompt": str(model_path),
            "manifest": str(manifest_path), "identity_risk": identity_risk,
            "exemplars_reviewed": False, "approved": False,
            "per_source": [{k: p[k] for k in ("image", "hash", "lineart", "exemplars", "identity_risk")} for p in per],
            "stamp": time.strftime("%Y%m%d-%H%M%S")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", help="一张或多张画风源图")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--stroke", type=int, default=2, help="每图笔触样例数")
    ap.add_argument("--fabric", type=int, default=2, help="每图材质样例数")
    ap.add_argument("--patch", type=int, default=384)
    a = ap.parse_args()
    images = [Path(p) for p in a.images]
    for img in images:
        if not img.is_file():
            print(json.dumps({"ok": False, "message": f"找不到图片:{img}"}, ensure_ascii=False))
            return 1
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    package = distill_images(images, out_dir, a.stroke, a.fabric, a.patch)
    (out_dir / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(package, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
