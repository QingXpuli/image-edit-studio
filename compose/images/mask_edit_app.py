"""Local masked image-edit web tool for gpt-image via an OpenAI-compatible relay.

Why this exists: chat clients and relay web consoles do not expose a mask
canvas, so "keep the character, change only the background" is impossible
there. This runs locally, gives you a real paint-on-mask canvas, and calls
POST /images/edits with a mask parameter and a custom base_url.

Zero third-party dependencies at runtime: Python stdlib + Pillow + numpy
(already present on this machine). No Docker, no GPU.

Usage:
    python app.py                 # http://127.0.0.1:8000
    python app.py --port 8080
"""
import argparse
import json
import os                     # 模块级也要：画廊的"用户输入索引"在模块级拼路径（曾在函数内 import，导致 NameError）
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image
    import numpy as np
except ImportError as e:  # pragma: no cover
    print(f"ERROR: needs Pillow + numpy ({e})")
    sys.exit(1)

MAX_UPLOAD = 120 * 1024 * 1024   # 120 MB per part (base64 inflates ~33%)


def env_cred(name: str, default: str = "") -> str:
    """读凭据：先看进程环境，读不到再回退到**用户级环境变量的注册表值**。

    为什么需要这层回退：进程的环境块是**启动时**从父进程继承并固定的。若 ZCode（或任何终端）
    在设置环境变量**之前**就已启动，它的子进程永远看不到那个变量——表现为"同一个命令，
    有的终端里能跑、有的报缺 key"，极难排查。直接读一次注册表就消除这个坑。

    只读 User 作用域（不读 Machine，免得把系统级配置当成本用户的）；非 Windows 或读取失败时
    安静地返回 default。**密钥依旧不写进任何文件。**
    """
    val = os.environ.get(name)
    if val:
        return val
    if os.name == "nt":
        try:
            import winreg          # 仅 Windows 有；放函数内，免得影响其它平台
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v, _ = winreg.QueryValueEx(k, name)
                if isinstance(v, str) and v:
                    return v
        except Exception:
            pass
    return default


# --------------------------------------------------------------------------
# Engine profiles. Everything below was MEASURED against the live relay:
#
#   gpt  (gpt-image-2)
#     - /images/edits is multipart and has NO `mask` field; masking is achieved
#       with a red-marked reference image sent on image[1] plus an explicit
#       instruction. Verified 100% on the protected region.
#     - returns b64_json.
#     - MULTI-IMAGE: 3 images verified working on the live relay as
#       image + image[1] + image[2] (2026-09-17, via probe_502.py and a real UI
#       run). Previously only 2 had been verified.
#     - SIZE IS NOT STRICTLY HONOURED: asking for 1024x1536 returned 1029x1528,
#       and a 1024x1024 request once returned 1254x1254. So any "follow the source
#       aspect ratio" helper can only promise an approximate ratio, never exact
#       pixels — do not build exact-size assumptions on top of it.
#     - the account only exposes ONE model from GET /models (gpt-image-2), so the
#       "获取模型列表" button showing a single entry is expected, not a bug.
#     - HTTP 502 {"error":{"message":"Upstream access forbidden, please contact
#       administrator","type":"upstream_error"}} is a TRANSIENT upstream failure,
#       not a bad request (that would be 400). The identical request succeeded on
#       retry. Retry before investigating.
#
#   grok (grok-imagine / grok-imagine-edit)
#     - text-to-image works, but ONLY with response_format="b64_json": the
#       default reply is a url on imgen.x.ai, which this network cannot reach
#       (resolves to 162.125.1.8, connect times out) so the image could never be
#       downloaded.
#     - /images/edits IGNORES response_format, so image-to-image always comes
#       back as such an unreachable url -> effectively unusable from here.
#     - grok-imagine-edit rejects more than one reference image (HTTP 400).
#     - the spec says not to send OpenAI `quality`; `resolution` (1k/2k) drives
#       output size instead.
# --------------------------------------------------------------------------
# 凭据只从环境变量读（2026-09-25 起：原先写死在下面的 key 已清除，因为要能安全转发）。
# 页面上仍可手填并只存浏览器 localStorage；这里给的是**服务端默认值**——
# 客户端没传 base_url/api_key 时会回退到它（见 api_edit 里的 `eng["base"]` / `eng["key"]`）。
# 留空则页面必须手填、服务端会明确报错，不会静默传空串。
_RELAY_BASE = env_cred("RELAY_BASE_URL", "https://image-direct.geiliapi.com/v1").rstrip("/")
_RELAY_KEY = env_cred("RELAY_API_KEY", "")
_GROK_BASE = env_cred("RELAY_GROK_BASE_URL", "").rstrip("/")
_GROK_KEY = env_cred("RELAY_GROK_API_KEY", "")

ENGINES = {
    "gpt": {
        "label": "GPT Image 2 · 文生图 + 图生图（支持遮罩）",
        "member": "gpt",
        "base": _RELAY_BASE,
        "key": _RELAY_KEY,
        "t2i_model": "gpt-image-2",
        "i2i_model": "gpt-image-2",
        "uses_quality": True,
        "uses_resolution": False,
        "edits_b64": True,
        "multi_image": True,
        "mask": True,
        "sizes": {
            "low": ["1024x1536", "1024x1024", "1536x1024"],
            "medium": ["1152x2048", "2048x2048", "2048x1152"],
            "high": ["2160x3840", "2880x2880", "3840x2160"],
        },
    },
    "grok": {
        "label": "Grok Imagine · 文生图 + 图生图",
        "member": "grok",
        "base": _GROK_BASE,
        "key": _GROK_KEY,
        "t2i_model": env_cred("RELAY_GROK_T2I_MODEL", "grok-imagine"),
        "i2i_model": env_cred("RELAY_GROK_I2I_MODEL", "grok-imagine-edit"),
        # spec: do NOT send OpenAI `quality` to Grok models
        "uses_quality": False,
        "uses_resolution": True,     # 1k / 2k (4k may be rejected upstream)
        "edits_b64": False,          # /images/edits ignores response_format
        "multi_image": False,        # measured 2026-10-06: image + image[1] -> HTTP 400
        "mask": False,
        "sizes": {
            "1k": ["1024x1024", "1024x1024", "1024x1024"],
            "2k": ["2048x2048", "2048x2048", "2048x2048"],
            "4k": ["3840x2160", "2880x2880", "2160x3840"],
        },
    },
}


def engine_of(name: str):
    return ENGINES.get((name or "gpt").strip().lower(), ENGINES["gpt"])


def engine_fetch_image(item, timeout=180):
    """Return (b64, error). Inline b64 stays local; URL results use the shared downloader."""
    import base64 as _b64
    import tempfile
    from pathlib import Path
    if item.get("b64_json"):
        return item["b64_json"], None
    url = item.get("url")
    if not url:
        return None, "响应里既没有 b64_json 也没有 url"
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "style-distill" / "round_lib"))
        from run_round import download
        with tempfile.TemporaryDirectory(prefix="engine-image-") as directory:
            path = Path(directory) / "result.img"
            download(url, path, timeout=min(timeout, 300))
            return _b64.b64encode(path.read_bytes()).decode(), None
    except Exception as e:
        return None, f"图片下载失败（{type(e).__name__}），结果域名 {host}"


def force_size_b64(b64: str, size: str):
    """Resize the returned image to exactly the requested size when needed.

    Measured: the upstream honours non-square sizes (1024x1536 -> 1024x1536) but
    rewrites a 1:1 request (1024x1024) into 1254x1254. Aspect ratio is preserved,
    so this only rescales; returns the input untouched when it already matches.
    """
    import base64 as _b64
    import io as _io
    try:
        want = tuple(int(v) for v in size.lower().split("x"))
    except Exception:
        return b64
    try:
        raw = _b64.b64decode(b64)
        im = Image.open(_io.BytesIO(raw))
        im.load()
        if im.size == want:
            return b64
        fixed = im.convert("RGB").resize(want, Image.LANCZOS)
        buf = _io.BytesIO()
        fixed.save(buf, "PNG", optimize=True)
        return _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64



# --------------------------------------------------------------------------
# multipart/form-data parsing (stdlib only; cgi is removed in 3.13+)
# --------------------------------------------------------------------------
def parse_multipart(body: bytes, boundary: bytes):
    """Return (fields: dict[str, str], files: dict[str, (filename, bytes)])."""
    fields, files = {}, {}
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = filename = None
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if not line.lower().startswith("content-disposition"):
                continue
            for part in line.split(";"):
                part = part.strip()
                if part.startswith("name="):
                    name = part[5:].strip('"')
                elif part.startswith("filename="):
                    filename = part[9:].strip('"')
        if name is None:
            continue
        if filename is not None:
            files[name] = (filename, content)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def build_multipart(fields, files, boundary):
    """Build a multipart/form-data body."""
    out = bytearray()
    for k, v in fields.items():
        out += b"--" + boundary + b"\r\n"
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += str(v).encode("utf-8") + b"\r\n"
    for k, (fname, data, ctype) in files.items():
        out += b"--" + boundary + b"\r\n"
        out += f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'.encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += b"--" + boundary + b"--\r\n"
    return bytes(out)


# --------------------------------------------------------------------------
# image helpers
# --------------------------------------------------------------------------
def normalise_to_size(img: Image.Image, tw: int, th: int, pad_mode: str):
    """Fit img into (tw,th) preserving aspect ratio; pad the slack.

    gpt-image only accepts exact sizes (1024x1024 / 1024x1536 / 1536x1024),
    so an arbitrary upload must be letterboxed rather than stretched.
    """
    img = img.convert("RGB")
    src_ar, tgt_ar = img.width / img.height, tw / th
    if pad_mode == "crop":
        if src_ar > tgt_ar:
            nw = int(img.height * tgt_ar)
            box = ((img.width - nw) // 2, 0, (img.width - nw) // 2 + nw, img.height)
        else:
            nh = int(img.width / tgt_ar)
            box = (0, (img.height - nh) // 2, img.width, (img.height - nh) // 2 + nh)
        return img.crop(box).resize((tw, th), Image.LANCZOS)
    scale = min(tw / img.width, th / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (tw, th), (255, 255, 255))
    canvas.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def make_visual_mask(image: "Image.Image", canvas_mask: "Image.Image",
                     tw: int, th: int, pad_mode: str, invert: bool):
    """Build the reference image the relay actually understands.

    Verified by live A/B testing against image-direct.geiliapi.com:
      - a `mask` form field is SILENTLY IGNORED (0% effect)
      - the documented fields are image, image[1], image[2], image[3]
      - feeding a red/white "visual mask" as image[1] gave 100% agreement
        (edit region changed, protected region untouched)

    So we composite the uploaded image with the painted region tinted red,
    which the model can read as "this area is the target".
    """
    import io as _io
    img_fit = normalise_to_size(image, tw, th, pad_mode)
    src_rgb = image.convert("RGB")
    if pad_mode == "pad":
        s = min(tw / src_rgb.width, th / src_rgb.height)
        nw, nh = max(1, round(src_rgb.width * s)), max(1, round(src_rgb.height * s))
        ox, oy = (tw - nw) // 2, (th - nh) // 2
    else:
        nw, nh, ox, oy = tw, th, 0, 0

    cm = canvas_mask.resize((nw, nh), Image.LANCZOS)
    painted = np.asarray(cm.split()[3]) > 127
    if invert:
        painted = ~painted

    pad_paint = np.zeros((th, tw), bool)
    pad_paint[oy:oy + nh, ox:ox + nw] = painted

    vis = np.asarray(img_fit).astype(np.float32)
    red = np.zeros_like(vis)
    red[:, :, 0] = 255.0                      # RGB red
    m = pad_paint[..., None].astype(np.float32)
    vis = vis * (1 - m) + red * m             # hard red overlay in the edit zone
    buf = _io.BytesIO()
    Image.fromarray(np.clip(vis, 0, 255).astype(np.uint8), "RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue(), pad_paint


def build_mask_alpha(pad_paint: np.ndarray):
    """OpenAI-convention alpha mask (transparent = editable). Kept for relays
    that DO honour it; this one does not, but sending it is harmless."""
    import io as _io
    th, tw = pad_paint.shape
    out = np.zeros((th, tw, 4), np.uint8)
    out[:, :, 3] = np.where(pad_paint, 0, 255).astype(np.uint8)
    buf = _io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


SCHEMA_PROMPT = (
    "[MASK INSTRUCTION] The second reference image is the same picture with the region to "
    "modify marked in flat red. Red areas are the ONLY places you may change. Every pixel "
    "outside the red areas must be reproduced exactly as in the first image - same shapes, "
    "same colours, same line art, same lighting, nothing redrawn, nothing restyled, nothing "
    "moved. Do not draw red into the output; the red is only a marker. "
    "[WHAT TO DO] "
)


# --------------------------------------------------------------------------
# Image-slot labelling (added 2026-09-17).
#
# WHY THIS EXISTS — measured from server.log: the client used to upload N images
# as a BATCH, one request per image, and every request contained only
# `parts: ['image_file']`. So a user who uploaded a style reference "as the
# second image" never had it sent as a reference at all — it became a second
# independent job — and a prompt saying "the second image is the style donor"
# bound to nothing, which is exactly how character identity got lost.
#
# Fix: the client now sends explicit REFERENCE parts with a role each, and the
# server builds an AUTHORITATIVE slot map from what it is actually about to
# send, then prepends it to the prompt. Image numbering can no longer disagree
# with reality, because the same code path computes both.
# --------------------------------------------------------------------------

# The relay documents image, image[1], image[2], image[3] -> four parts max.
MAX_IMAGE_SLOTS = 4

REF_ROLES = {
    "style": {
        "cn": ("画法 / 风格参考",
               "只取它的画法：笔触、线稿、明暗处理、色彩处理强度、材质与质感。"
               "绝不要采用它的人物、构图、道具、背景或任何具体色相。"),
        "en": ("STYLE / TECHNIQUE reference",
               "take ONLY its rendering technique: brushwork, lineart, tonal handling, "
               "colour-treatment strength, material and texture. Never adopt its subject, "
               "composition, props, background or any specific hue."),
    },
    "pose": {
        "cn": ("姿态参考",
               "只取它人物的姿态：身体朝向、肩线、头部角度、视线方向、手臂与手的动作。"
               "不要采用它的人物身份、服装、面貌或颜色。"),
        "en": ("POSE reference",
               "take ONLY its figure's pose: body orientation, shoulder line, head angle, "
               "gaze direction, arm and hand gesture. Do not adopt its character identity, "
               "clothing, face or colours."),
    },
    "content": {
        "cn": ("内容 / 身份参考",
               "提供人物的身份、五官、发型、服饰与配色。"),
        "en": ("CONTENT / IDENTITY reference",
               "provides the character identity, facial features, hairstyle, garments and colours."),
    },
    "other": {
        "cn": ("参考图", "按提示词里的说明使用。"),
        "en": ("REFERENCE image", "use it as described in the prompt text."),
    },
}

SLOT_CONTENT_CN = ("要修改的内容图",
                   "身份与颜色的唯一来源。只有它可以决定内容。")
SLOT_CONTENT_EN = ("CONTENT image to be edited",
                   "the sole source of identity and colour; only it may decide content.")
SLOT_MASK_CN = ("红色标记图",
                "涂成纯红的区域 = 唯一允许改动的地方；红色以外的每个像素必须原样保留"
                "（形状、颜色、线稿、光影都不许重画）。不要把红色画进结果。")
SLOT_MASK_EN = ("RED-MARKED mask guide",
                "flat red areas are the ONLY places you may change; every pixel outside the red "
                "must be reproduced exactly (same shapes, colours, lineart, lighting - nothing "
                "redrawn). Do not paint red into the output.")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def build_slot_header(slots: list, chinese: bool) -> str:
    """Render the authoritative image-slot map that gets prepended to the prompt.

    `slots` is a list of (index, label_cn, instr_cn, label_en, instr_en).
    """
    if not slots:
        return ""
    if chinese:
        out = ["【本次请求实际随附的图片编号 —— 严格按此对应，不要按顺序猜测】"]
        for idx, lcn, icn, _, _ in slots:
            out.append(f"  图{idx} = {lcn}：{icn}")
        out.append("【以上编号与本请求附带的图片顺序完全一致。】")
    else:
        out = ["[IMAGE SLOTS IN THIS REQUEST - map each image exactly as listed; "
               "do not infer roles from order]"]
        for idx, _, _, len_, ien in slots:
            out.append(f"  Image {idx} = {len_}: {ien}")
        out.append("[The numbering above matches the order of the images attached to this "
                   "request exactly.]")
    return "\n".join(out) + "\n\n"


def take_reference_images(fields: dict, files: dict):
    """Pull `ref_file_<i>` parts + `ref_roles` JSON out of a parsed multipart body.

    Returns a list of (role, filename, bytes). Missing/unknown roles fall back to
    'other' rather than failing, so a mismatched client cannot break a run.
    """
    try:
        count = int(fields.get("ref_count") or 0)
    except Exception:
        count = 0
    try:
        roles = json.loads(fields.get("ref_roles") or "[]")
    except Exception:
        roles = []
    if not isinstance(roles, list):
        roles = []

    out = []
    for i in range(max(0, min(count, MAX_IMAGE_SLOTS))):
        key = f"ref_file_{i}"
        if key not in files:
            continue
        role = roles[i] if i < len(roles) else "other"
        if role not in REF_ROLES:
            role = "other"
        out.append((role, files[key][0], files[key][1]))
    return out


def decode_data_url(data_url: str):
    """data:image/png;base64,.... -> bytes"""
    import base64
    if "," not in data_url:
        raise ValueError("not a data URL")
    head, b64 = data_url.split(",", 1)
    return base64.b64decode(b64)


# --------------------------------------------------------------------------
# relay call
# --------------------------------------------------------------------------
def relay_post(url: str, body: bytes, content_type: str, api_key: str, timeout=300):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}".encode()


# --------------------------------------------------------------------------
# HTML UI
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>本地遮罩改图 · gpt-image</title>
<style>
:root{--bg:#14161a;--panel:#1c2026;--line:#2c3440;--fg:#e6e9ee;--dim:#8b95a3;
      --acc:#4a9eff;--ok:#3ecf8e;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;font:13px/1.6 "Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;
       gap:12px;align-items:center;background:var(--panel)}
header h1{font-size:14px;margin:0;font-weight:600}
header .sp{flex:1}
.wrap{display:grid;grid-template-columns:minmax(360px,1fr) 360px;gap:14px;padding:14px}
@media(max-width:1080px){.wrap{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.panel h2{font-size:12px;margin:0 0 8px;color:var(--dim);font-weight:600;letter-spacing:.04em}
canvas{max-width:100%;border-radius:6px;background:#0c0e11;display:block;cursor:crosshair}
.row{display:flex;gap:8px;align-items:center;margin:7px 0;flex-wrap:wrap}
.row label{color:var(--dim);min-width:64px}
input[type=text],input[type=password],select,textarea{
  background:#0e1116;border:1px solid var(--line);color:var(--fg);border-radius:5px;
  padding:6px 8px;font:inherit;flex:1;min-width:0}
textarea{width:100%;resize:vertical;min-height:72px;flex:none}
button{background:#28313d;color:var(--fg);border:1px solid var(--line);border-radius:5px;
       padding:6px 12px;cursor:pointer;font:inherit}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button:disabled{opacity:.45;cursor:not-allowed}
input[type=range]{flex:1}
.hint{color:var(--dim);font-size:11px;line-height:1.5}
.msg{padding:7px 10px;border-radius:5px;margin-top:8px;font-size:12px;white-space:pre-wrap;
     word-break:break-all;display:none;max-height:340px;overflow:auto;user-select:text;
     font-family:Consolas,"Courier New",monospace;line-height:1.5}
.msg.ok{display:block;background:#12351f;border:1px solid #1f6b3f;color:#b6f0cd}
.msg.bad{display:block;background:#3a1717;border:1px solid #7a2c2c;color:#ffc9c9}
.pill{font-size:11px;padding:2px 7px;border-radius:10px;background:#232a34;color:var(--dim)}
#drop{border:1px dashed var(--line);border-radius:6px;padding:22px;text-align:center;
      color:var(--dim);cursor:pointer}
#drop.over{border-color:var(--acc);color:var(--fg)}
#thumbs{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.th{width:60px;height:60px;border:2px solid var(--line);border-radius:5px;overflow:hidden;
    cursor:pointer;position:relative;background:#0c0e11;flex:none}
.th img{width:100%;height:100%;object-fit:cover;display:block}
.th.on{border-color:var(--acc)}
.th .dot{position:absolute;right:2px;bottom:2px;width:8px;height:8px;border-radius:50%;
         background:#444;border:1px solid #000}
.th.masked .dot{background:var(--ok)}
/* Numbering badges on the thumbnails. Two DIFFERENT numbering systems must stay
   visually distinct, or they get read as one:
     .num  = batch order of the images to edit   (#1, #2, ...)
     .slot = this image's position inside ONE request
             (图1 = content, 图2 = red mask guide, 图3+ = references)   */
.th .num{position:absolute;left:2px;top:2px;font-size:10px;line-height:1;font-weight:700;
         padding:2px 4px;border-radius:3px;background:rgba(0,0,0,.68);color:#fff;
         font-variant-numeric:tabular-nums}
.th.on .num{background:var(--acc);color:#08111c}
.th .slot{position:absolute;right:2px;top:2px;font-size:10px;line-height:1;font-weight:700;
          padding:2px 4px;border-radius:3px;background:rgba(74,158,255,.92);color:#08111c;
          font-variant-numeric:tabular-nums}
.slotLegend{font-size:11px;line-height:1.75;white-space:pre-wrap}
.slotLegend b{color:var(--fg)}
.slotLegend .k{color:var(--acc);font-weight:700}
.out{max-width:100%;border-radius:6px;display:block;margin-bottom:6px}
.small{width:78px!important;flex:none!important}
</style></head><body>

<header>
  <h1>本地遮罩改图</h1>
  <span class="pill" id="imgInfo">未加载图片</span>
  <span class="sp"></span>
  <span class="pill" id="maskStat">遮罩覆盖率 0%</span>
</header>

<div class="wrap">
  <div class="panel">
    <h2>步骤 1 · 上传「要改的图」，涂出要改动的地方</h2>
    <label id="pickBox" for="file" style="display:block;cursor:pointer">
      <div id="drop" style="border:1px dashed var(--line);border-radius:6px;padding:22px;
           text-align:center;color:var(--dim)">
        <b style="color:var(--fg);font-size:14px">点击这里选择图片</b><br>
        <span style="font-size:11px">或把图片文件拖到这块区域</span>
      </div>
    </label>
    <input type="file" id="file" accept="image/*" multiple
           style="margin-top:8px;width:100%;color:var(--dim)">
    <div class="hint" id="fileName"></div>
    <div id="thumbs"></div>
    <div class="row" id="batchRow" style="display:none">
      <span class="pill" id="batchInfo"></span>
      <button id="addMore">继续添加图片</button>
      <button id="copyMask">把当前遮罩复制到其他图</button>
      <button id="clearList">清空列表</button>
    </div>
    <div id="editor" style="display:none;margin-top:10px">
      <canvas id="cv"></canvas>
      <div id="maskTools">
        <div class="row" style="margin-top:10px">
          <label>画笔</label>
          <input type="range" id="brush" min="10" max="240" value="80">
          <span class="pill" id="brushVal">80</span>
        </div>
        <div class="row">
          <button id="erase">橡皮模式</button>
          <button id="clear">清除遮罩</button>
          <button id="invert">反选遮罩</button>
          <button id="fit">适应窗口</button>
        </div>
        <div class="hint">
          涂过的地方 = <b>允许被修改</b>（例如只涂背景）。未涂的区域会通过「第二张红色标记参考图
          + 明确指令」双重方式要求模型保持原样。红色半透明是本地显示的遮罩。
        </div>
      </div>
    </div>
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
      <div style="font-weight:600;font-size:13px;margin-bottom:2px">
        参考图（可选）· 每张指定角色
      </div>
      <div class="hint" style="margin-top:0">
        上面选的是<b>要改的图</b>（多张 = 批量，各自单独跑）。这里加的是<b>参考图</b>：
        它们会作为 image[1..] 随<b>每一次</b>请求一起发送，并在提示词前面自动写入编号说明，
        所以模型能确切知道第几张图是干什么的。
      </div>
      <label for="refFile" style="display:block;cursor:pointer">
        <div style="border:1px dashed var(--line);border-radius:6px;padding:16px;
             text-align:center;color:var(--dim)">
          <b style="color:var(--fg);font-size:13px">点击添加参考图</b><br>
          <span style="font-size:11px">可多选</span>
        </div>
      </label>
      <input type="file" id="refFile" accept="image/*" multiple
             style="margin-top:8px;width:100%;color:var(--dim)">
      <div id="refList"></div>
      <div class="hint" id="slotPreview" style="white-space:pre-wrap"></div>
      <div class="row" id="refRow" style="display:none">
        <button id="clearRefs">清空参考图</button>
      </div>
    </div>
  </div>

  <div>
    <div class="panel">
      <h2>步骤 2 · 引擎与修改范围</h2>
      <div class="row">
        <select id="engine" style="flex:1;font-size:13px;padding:8px">
          <option value="gpt" selected>GPT Image 2 · 文生图 + 图生图（支持遮罩）</option>
          <option value="grok">Grok Imagine · 文生图 + 图生图</option>
        </select>
      </div>
      <div class="row">
        <select id="scope" style="flex:1;font-size:13px;padding:8px">
          <option value="whole" selected>整张图（无需涂遮罩）</option>
          <option value="mask">仅涂过的区域（需要涂遮罩）</option>
        </select>
      </div>
      <div class="hint" id="scopeHint"></div>
      <div class="hint" id="engineHint"></div>
    </div>

    <div class="panel" style="margin-top:12px">
      <h2>步骤 3 · 连接设置</h2>
      <div class="row"><label>Base URL</label>
        <input type="text" id="baseUrl" value="https://image-direct.geiliapi.com/v1"
               placeholder="https://你的中转站.com/v1"></div>      <div class="row"><label>API Key</label>
        <input type="password" id="apiKey" value=""
               placeholder="sk-...">
        <button id="eye" title="显示/隐藏">👁</button></div>
      <div class="row"><label>模型</label>
        <input type="text" id="model" value="gpt-image-2" list="models">
        <datalist id="models"></datalist></div>
      <div class="row">
        <button id="test">测试连接</button>
        <button id="listModels">获取模型列表</button>
      </div>
      <div class="hint">key 需自己填写（本项目不再预置任何密钥）。它只保存在浏览器本地（localStorage），
        由本地服务转发，不会写入磁盘。</div>
    </div>

    <div class="panel" style="margin-top:12px">
      <h2>步骤 4 · 提示词与参数</h2>
      <textarea id="prompt" placeholder="只描述遮罩区域要变成什么。例：无任何可辨物体的平滑灰绿渐变背景，无纹理、无建筑、无雾纹。"></textarea>
      <div class="row" id="qualityRow"><label>画质</label>
        <select id="quality">
          <option value="low" selected>low（1K，快/便宜）</option>
          <option value="medium">medium（2K）</option>
          <option value="high">high（4K）</option>
        </select></div>
      <div class="row" id="resolutionRow" style="display:none"><label>resolution</label>
        <select id="resolution">
          <option value="1k" selected>1k</option>
          <option value="2k">2k</option>
          <option value="4k">4k（上游可能拒绝）</option>
        </select></div>
      <div class="row"><label>尺寸</label>
        <select id="size"></select></div>
      <div class="row"><label>比例</label>
        <label style="min-width:0;color:var(--fg);display:flex;align-items:center;gap:6px;
                      cursor:pointer;font-size:12px">
          <input type="checkbox" id="followRatio" style="width:auto;margin:0">
          跟随原图比例（只会在当前档位的合法尺寸里挑最接近的）
        </label></div>
      <div class="hint" id="sizeHint"></div>
      <div class="hint" id="ratioHint" style="white-space:pre-wrap"></div>
      <div class="row"><label>适配</label>
        <select id="fitMode">
          <option value="pad" selected>补白边（不裁切）</option>
          <option value="crop">居中裁切</option>
        </select></div>
      <div class="row"><label>遮罩语义</label>
        <select id="maskMode">
          <option value="std" selected>标准：涂过的地方被修改</option>
          <option value="inv">反向：涂过的地方被保留</option>
        </select></div>
      <div class="row">
        <button class="primary" id="run" disabled>生成当前图</button>
        <button id="runAll" disabled>批量生成全部</button>
        <button id="download" disabled>下载当前结果</button>
      </div>
      <div class="msg" id="msg"></div>
    </div>

    <div class="panel" style="margin-top:12px">
      <h2>结果</h2>
      <div id="outBox" class="hint">尚未生成</div>
    </div>
  </div>
</div>

<script>
// This page may be embedded in a sandboxed iframe (observed: "The document is
// sandboxed and lacks the 'allow-same-origin' flag"), in which case ANY
// localStorage/sessionStorage access THROWS a SecurityError. An uncaught throw
// at top level aborts the whole script, so no click handler ever gets bound and
// the page looks dead. Everything therefore goes through this safe shim.
const mem = {};
const store = {
  get(k) {
    try { const v = localStorage.getItem(k); if (v !== null) return v; } catch (_) {}
    return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null;
  },
  set(k, v) {
    mem[k] = String(v);
    try { localStorage.setItem(k, v); } catch (_) {}
  },
  del(k) {
    delete mem[k];
    try { localStorage.removeItem(k); } catch (_) {}
  },
  get available() {
    try { localStorage.setItem('__t', '1'); localStorage.removeItem('__t'); return true; }
    catch (_) { return false; }
  }
};

window.addEventListener('error', ev => {
  const m = document.getElementById('msg');
  if (m) { m.className = 'msg bad'; m.textContent = '页面脚本错误：' + (ev.message || ev.error); }
});
// If any of this leaks out of the handlers below, show it instead of dying
// silently - the user cannot see the browser console.
window.addEventListener('unhandledrejection', ev => {
  const m = document.getElementById('msg');
  const r = ev.reason;
  if (m) {
    m.className = 'msg bad';
    m.textContent = '未捕获异常：' + ((r && (r.name + ': ' + r.message)) || String(r));
  }
});

const $ = id => document.getElementById(id);

// Absolute base for our own API. The page may be embedded in a sandboxed
// iframe on a different origin, where a relative fetch('/api/...') would go to
// the WRONG host (the parent's origin) and fail. The server substitutes
// __APP_ORIGIN__ with its real address when serving this page.
const APP_ORIGIN = "__APP_ORIGIN__";
const api = path => APP_ORIGIN + path;

// Catch network-level failures that would otherwise never reach the UI. The
// browser console is not visible to the user, so surface them here.
window.addEventListener('unhandledrejection', ev => {
  const m = document.getElementById('msg');
  const r = ev.reason;
  if (m) {
    m.className = 'msg bad';
    m.textContent = '请求异常：' + (r && (r.name + ': ' + r.message) || String(r))
      + '\n目标地址：' + APP_ORIGIN;
  }
});
const cv = $('cv'), ctx = cv.getContext('2d');
let img = null, scale = 1, drawing = false, erase = false, hasMask = false;
let resultURL = null;
// each loaded image keeps its OWN mask canvas, so a batch of images can each be
// painted separately while sharing one prompt and one set of parameters
let films = [];        // {name, im, mc, masked, resultB64}
let active = -1;

// diagnostics for the last upload attempt - shown on failure so the cause is
// visible instead of a bare "generation failed"
const diag = {};
function diagText() {
  const lines = [];
  if (diag.engine) lines.push('引擎：' + diag.engine);
  if (diag.image) lines.push('上传图片：' + diag.image);
  if (diag.refs) lines.push('参考图：' + diag.refs);
  if (diag.mask) lines.push('遮罩：' + diag.mask);
  if (diag.ms != null) lines.push('耗时：' + (diag.ms / 1000).toFixed(1) + ' s');
  lines.push('模型：' + ($('model') ? $('model').value : '?')
    + '　画质：' + ($('quality') ? $('quality').value : '?')
    + '　尺寸：' + ($('size') ? $('size').value : '?'));
  lines.push('本地服务：' + api('/api/edit'));
  return lines.join('\n');
}

// ---- quality <-> size must be a legal pair (per the relay's spec) ----
const SIZES = {
  low:    [['1024x1536','1024x1536 竖幅 2:3'],['1024x1024','1024x1024 方形 1:1'],['1536x1024','1536x1024 横幅 3:2']],
  medium: [['1152x2048','1152x2048 竖幅 9:16'],['2048x2048','2048x2048 方形 1:1'],['2048x1152','2048x1152 横幅 16:9']],
  high:   [['2160x3840','2160x3840 竖幅 9:16'],['2880x2880','2880x2880 方形 1:1'],['3840x2160','3840x2160 横幅 16:9']],
};
let savedSize = store.get('mie_size') || '';

// ---- engines: credentials and size rules differ per engine (all measured) ----
// `mask` / `multiImage` mirror the server's ENGINES table and are used by the
// slot planner below, so the UI can show the real image numbering before a run
// instead of guessing.
// NOTE: a file upload is a BATCH queue (one request per image); the 参考图 list
// is different — those are attached to EVERY request as reference images.
const ENGINE_DEFAULTS = {
  gpt: {
    key: '',
    model: 'gpt-image-2',
    kind: 'quality',
    mask: true,
    multiImage: true,
    hint: 'GPT Image 2：文生图与图生图都可用；图生图支持「仅涂过的区域」（红色标记参考图 + 指令）。'
        + ' 画质与尺寸必须同档：low=1K / medium=2K / high=4K。'
        + ' 支持多张参考图（最多 4 张图，含内容图与红色标记图）。',
  },
  grok: {
    key: '',
    model: 'grok-imagine',
    kind: 'resolution',
    mask: false,
    multiImage: false,
    hint: 'Grok Imagine：文生图可用（已强制用 b64 内联返回）。两条实测限制 —— '
        + '① 图生图返回的图片地址在 imgen.x.ai，本机访问不了，会拿不到图；'
        + '② Grok 不支持遮罩，且不接受多于 1 张输入图片（HTTP 400）→ 不能加参考图。'
        + ' 档位用 resolution（1k/2k/4k，4k 上游可能拒绝）。',
  },
};
function currentEngine() {
  return $('engine') ? $('engine').value : 'gpt';
}

// ---- reference images: role-tagged, sent WITH every request ---------------
// NOT batch items. Server side: take_reference_images() + REF_ROLES.
// Send order is always: image (content) -> image[1] red mask guide (if mask
// mode) -> image[2..] these references, in the order added here.
let refs = [];         // {name, im, role}
const MAX_IMAGE_SLOTS_CLIENT = 4;
const REF_ROLES = [
  ['style',   '画法 / 风格', '只取笔触、线稿、明暗、材质；不取内容与配色'],
  ['pose',    '姿态',        '只取人物姿态；不取身份、服装、颜色'],
  ['content', '内容 / 身份', '提供人物身份、五官、发型、服饰与配色'],
  ['other',   '其他',        '按提示词里的说明使用'],
];
const SLOT_LABEL_CONTENT = '要改的内容图（身份与颜色的唯一来源）';
const SLOT_LABEL_MASK = '红色标记图（涂红区域 = 唯一允许改动的地方）';

function engFlags() {
  const d = ENGINE_DEFAULTS[currentEngine()] || ENGINE_DEFAULTS.gpt;
  return { mask: !!d.mask, multi: !!d.multiImage };
}
function wholeScope() { return $('scope') ? $('scope').value === 'whole' : true; }
function maskSlotUsed() { const f = engFlags(); return f.mask && !wholeScope(); }

/** What this request will actually contain, in send order. */
function slotPlan() {
  const plan = [{ n: 1, label: SLOT_LABEL_CONTENT }];
  if (maskSlotUsed()) plan.push({ n: 2, label: SLOT_LABEL_MASK });
  const first = maskSlotUsed() ? 3 : 2;
  refs.forEach((r, i) => {
    const role = REF_ROLES.find(x => x[0] === r.role) || REF_ROLES[3];
    plan.push({ n: first + i, label: '参考图 · ' + role[1] + '（' + r.name + '）' });
  });
  return plan;
}
function maxRefsNow() {
  const used = 1 + (maskSlotUsed() ? 1 : 0);
  return Math.max(0, MAX_IMAGE_SLOTS_CLIENT - used);
}

function renderRefs() {
  const box = $('refList');
  if (!box) return;
  box.innerHTML = '';
  const plan = slotPlan();
  const first = plan.length - refs.length;   // index of this ref's first slot
  refs.forEach((r, i) => {
    const slot = plan[first + i];
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:8px;'
      + 'padding:6px;border:1px solid var(--line);border-radius:6px';

    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;width:52px;height:52px;flex:none';
    const t = document.createElement('img');
    t.src = r.im.src;
    t.style.cssText = 'width:52px;height:52px;object-fit:cover;border-radius:4px;display:block';
    wrap.appendChild(t);
    // the number this image occupies inside every request — shown ON the thumb
    const tb = document.createElement('span');
    tb.className = 'slot';
    tb.textContent = slot ? ('图' + slot.n) : '—';
    wrap.appendChild(tb);
    row.appendChild(wrap);

    const badge = document.createElement('span');
    badge.className = 'pill';
    badge.textContent = slot ? ('图' + slot.n) : '—';
    badge.title = '这张图在请求里的编号（会自动写进提示词）';
    row.appendChild(badge);

    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0';
    const nm = document.createElement('div');
    nm.textContent = r.name;
    nm.style.cssText = 'font-size:11px;color:var(--dim);white-space:nowrap;'
      + 'overflow:hidden;text-overflow:ellipsis';
    info.appendChild(nm);

    const sel = document.createElement('select');
    sel.style.cssText = 'font-size:12px;padding:4px;width:100%;margin-top:3px';
    REF_ROLES.forEach(([v, label, desc]) => {
      const o = document.createElement('option');
      o.value = v; o.textContent = label; o.title = desc;
      if (r.role === v) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => { r.role = sel.value; renderSlotPreview(); };
    info.appendChild(sel);
    row.appendChild(info);

    const del = document.createElement('button');
    del.textContent = '移除';
    del.style.cssText = 'flex:none';
    del.onclick = () => { refs.splice(i, 1); renderRefs(); };
    row.appendChild(del);

    box.appendChild(row);
  });
  if ($('refRow')) $('refRow').style.display = refs.length ? '' : 'none';
  renderSlotPreview();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * Full numbering legend. There are TWO independent numbering systems here and
 * conflating them is the bug this whole feature exists to prevent, so both are
 * spelled out side by side:
 *   #N   = batch order of the images to edit (one request per image)
 *   图N  = position inside a SINGLE request (图1 = content, 图2 = red mask
 *          guide, 图3+ = references)
 */
function renderSlotPreview() {
  const el = $('slotPreview');
  if (!el) return;
  const f = engFlags();
  const cur = (active >= 0 && films[active]) ? films[active] : null;
  const out = [];

  out.push('<b>① 批量队列 ·「要改的图」</b>　共 ' + films.length + ' 张，每张单独发一次请求');
  if (films.length) {
    films.forEach((x, i) => {
      out.push('　<span class="k">#' + (i + 1) + '</span>'
        + (i === active ? ' <b>← 当前选中</b>' : '') + '　' + esc(x.name));
    });
  } else {
    out.push('　（未加载图片 → 将以文生图模式运行，没有内容图）');
  }

  out.push('');
  out.push('<b>② 单次请求里的图片编号</b>（会自动写在提示词最前面）');
  slotPlan().forEach(s => {
    let who = '';
    if (s.n === 1) {
      who = cur
        ? '　← 批量 <span class="k">#' + (active + 1) + '</span> ' + esc(cur.name)
        : '　← 文生图，无输入图片';
    }
    out.push('　<span class="k">图' + s.n + '</span> = ' + esc(s.label) + who);
  });

  const max = maxRefsNow();
  if (refs.length && !f.multi) {
    out.push('<b>⚠️ 当前引擎不接受多于 1 张输入图片（HTTP 400）</b>'
      + ' → 请改用 GPT Image 2，或清空参考图。');
  } else if (refs.length > max) {
    out.push('<b>⚠️ 参考图 ' + refs.length + ' 张超出上限</b>（当前引擎与模式下最多 '
      + max + ' 张）：接口只接受 4 张图，内容图'
      + (maskSlotUsed() ? ' + 红色标记图' : '') + '先占位。'
      + '把「修改范围」改成「整张图」，或减少参考图。');
  }
  if (!refs.length) {
    out.push('　（未添加参考图：请求里只有内容图'
      + (maskSlotUsed() ? ' + 红色标记图' : '') + '，与改动前完全一致）');
  }
  el.className = 'hint slotLegend';
  el.innerHTML = out.join('<br>');
}

if ($('refFile')) {
  $('refFile').addEventListener('change', ev => {
    const picked = Array.from(ev.target.files || []).filter(f => /^image\//.test(f.type));
    if (!picked.length) { setMsg('参考图：没有选中图片文件', 'bad'); return; }
    let pending = picked.length;
    const done = () => { ev.target.value = ''; renderRefs(); };
    picked.forEach(f => {
      const im = new Image();
      im.onerror = () => {
        setMsg('参考图解码失败：' + f.name, 'bad');
        if (--pending === 0) done();
      };
      im.onload = () => {
        refs.push({ name: f.name, im, role: 'style' });
        if (--pending === 0) done();
      };
      im.src = URL.createObjectURL(f);
    });
  });
}
if ($('clearRefs')) $('clearRefs').onclick = () => { refs = []; renderRefs(); };


const GROK_SIZES = {
  '1k': [['1024x1024', '1024x1024 方形 1:1']],
  '2k': [['2048x2048', '2048x2048 方形 1:1']],
  '4k': [['2160x3840', '2160x3840 竖幅 9:16'], ['2880x2880', '2880x2880 方形 1:1'], ['3840x2160', '3840x2160 横幅 16:9']],
};

// ---- "follow the source image's aspect ratio" -----------------------------
// The upstream only accepts a FIXED whitelist of sizes (three per quality tier),
// so this can never produce an arbitrary ratio. What it does instead: pick the
// closest LEGAL size for the image actually being generated, and state exactly
// how close that is — plus which other tier would be closer, since the two
// tiers do not share their portrait/landscape ratios at all.
// While it is on, it owns the 尺寸 select (disabled) and decides the size per
// image, so a batch of mixed ratios each gets its own best fit.
function followRatioOn() {
  return !!($('followRatio') && $('followRatio').checked);
}

function engSizeList() {
  const eng = currentEngine();
  if (eng === 'grok') {
    return GROK_SIZES[$('resolution') ? $('resolution').value : '1k'] || GROK_SIZES['1k'];
  }
  return SIZES[$('quality') ? $('quality').value : 'low'] || SIZES.low;
}

/** Closest simple ratio, for a human-readable "≈2:3". */
function fmtAr(ar) {
  const cands = [[9, 16], [2, 3], [3, 4], [1, 1], [4, 3], [3, 2], [16, 9]];
  let best = null;
  cands.forEach(c => {
    const d = Math.abs(c[0] / c[1] - ar);
    if (!best || d < best.d) best = { s: c[0] + ':' + c[1], d };
  });
  return ar.toFixed(3) + (best && best.d < 0.02 ? '（≈' + best.s + '）' : '');
}

function nearestSizeFor(film, list) {
  const ar = film.im.naturalWidth / film.im.naturalHeight;
  let best = null;
  (list || engSizeList()).forEach(entry => {
    const wh = entry[0].split('x');
    const v = Number(wh[0]) / Number(wh[1]);
    const d = Math.abs(v - ar);
    if (!best || d < best.diff) best = { size: entry[0], label: entry[1], ar: v, diff: d };
  });
  return { ar: ar, best: best };
}

/**
 * Set the size for `film` (defaults to the active one) and explain the choice.
 * Called from refreshSizes, renderThumbs and generateOne, so the value always
 * matches the image about to be generated.
 */
function applyFollowRatio(film) {
  const sel = $('size');
  const hint = $('ratioHint');
  const on = followRatioOn();
  if (sel) sel.disabled = on;
  if (!on) { if (hint) hint.textContent = ''; return; }
  if (!sel) return;

  const f = film || ((active >= 0 && films[active]) ? films[active] : null);
  if (!f) {
    if (hint) hint.textContent = '跟随原图比例已开启；加载图片后会自动选尺寸。';
    return;
  }
  const r = nearestSizeFor(f);
  if (!r.best) return;
  sel.value = r.best.size;

  const lines = ['跟随原图：' + esc(f.name) + ' 比例 ' + fmtAr(r.ar)
    + ' → 尺寸 ' + r.best.size + '（' + r.best.label + '）'];
  if (r.best.diff <= 0.02) {
    lines.push('比例误差 ' + r.best.diff.toFixed(3) + '，基本吻合，不会被补白或裁切。');
  } else {
    lines.push('比例误差 ' + r.best.diff.toFixed(3) + '，比原图'
      + (r.best.ar < r.ar ? '更瘦长' : '更矮胖')
      + ' → 会按「适配」的设置补白边或裁切。');
    // the tiers do not share their ratios, so a different tier can fit better
    const tiers = (currentEngine() === 'grok') ? GROK_SIZES : SIZES;
    let sug = null;
    Object.keys(tiers).forEach(t => {
      (tiers[t] || []).forEach(entry => {
        const wh = entry[0].split('x');
        const d = Math.abs(Number(wh[0]) / Number(wh[1]) - r.ar);
        if (d < r.best.diff - 1e-9 && (!sug || d < sug.diff)) {
          sug = { tier: t, size: entry[0], diff: d };
        }
      });
    });
    if (sug) {
      lines.push('换「' + sug.tier + '」档可得 ' + sug.size
        + '（误差 ' + sug.diff.toFixed(3) + '），更接近原图。');
    } else {
      lines.push('当前引擎没有更接近的比例可选。');
    }
  }
  if (hint) hint.textContent = lines.join('\n');
}

function refreshSizes() {
  const eng = currentEngine();
  let list;
  if (eng === 'grok') {
    list = GROK_SIZES[$('resolution') ? $('resolution').value : '1k'] || GROK_SIZES['1k'];
  } else {
    list = SIZES[$('quality') ? $('quality').value : 'low'] || SIZES.low;
  }
  const sel = $('size');
  sel.innerHTML = '';
  list.forEach(([v, label]) => {
    const o = document.createElement('option'); o.value = v; o.textContent = label;
    sel.appendChild(o);
  });
  const idx = list.findIndex(p => p[0] === savedSize);
  sel.selectedIndex = idx >= 0 ? idx : 0;
  if (!followRatioOn()) {
    // persist only a MANUAL choice: while following, the auto-picked value must
    // not overwrite what the user chose before enabling the checkbox
    savedSize = sel.value;
    store.set('mie_size', sel.value);
  }
  $('sizeHint').textContent = eng === 'grok'
    ? 'Grok：档位由 resolution 决定，size 仅用于计费；上游官方目前列出 1k / 2k，4k 可能被拒。'
    : '画质决定档位，尺寸必须属于同一档：low=1K / medium=2K / high=4K。跨档组合会被接口拒绝。';
  applyFollowRatio();
}

function applyEngine() {
  const eng = currentEngine();
  const d = ENGINE_DEFAULTS[eng] || ENGINE_DEFAULTS.gpt;
  if (d.key) $('apiKey').value = d.key;   // 默认 key 已清空：不再覆盖用户手填的值
  if ($('model')) $('model').value = d.model;
  if ($('engineHint')) $('engineHint').textContent = d.hint;

  const wantQuality = d.kind === 'quality';
  if ($('qualityRow')) $('qualityRow').style.display = wantQuality ? '' : 'none';
  if ($('resolutionRow')) $('resolutionRow').style.display = wantQuality ? 'none' : '';
  if ($('quality') && !wantQuality) $('quality').value = 'low';

  const scopeSel = $('scope');
  const maskOpt = scopeSel.querySelector('option[value="mask"]');
  if (!wantQuality) {
    if (scopeSel.value === 'mask') scopeSel.value = 'whole';
    if (maskOpt) { maskOpt.disabled = true; maskOpt.textContent = '仅涂过的区域（Grok 不支持）'; }
  } else if (maskOpt) {
    maskOpt.disabled = false; maskOpt.textContent = '仅涂过的区域（需要涂遮罩）';
  }
  refreshSizes();
  updateScopeHint();
  // re-render the reference rows, not just the legend: their 图N badges depend
  // on the engine (mask support) and would otherwise go stale and mislead.
  if (typeof renderRefs === 'function') renderRefs();
}

$('quality').addEventListener('change', refreshSizes);
if ($('resolution')) $('resolution').addEventListener('change', refreshSizes);
if ($('engine')) {
  $('engine').addEventListener('change', () => { store.set('mie_engine', currentEngine()); applyEngine(); });
}
$('size').addEventListener('change', () => { savedSize = $('size').value; store.set('mie_size', savedSize); });

// follow-ratio toggle. Restored BEFORE the start-up refreshSizes() below, so the
// very first render already reflects the saved state.
if ($('followRatio')) {
  $('followRatio').checked = store.get('mie_follow_ratio') === '1';
  $('followRatio').addEventListener('change', () => {
    store.set('mie_follow_ratio', $('followRatio').checked ? '1' : '0');
    applyFollowRatio();
  });
}

const base = () => $('baseUrl').value.trim().replace(/\/+$/, '');
const key  = () => $('apiKey').value.trim();

// ---- persisted settings: HTML defaults win when nothing is saved yet ----
const DEFAULTS = {
  baseUrl: 'https://image-direct.geiliapi.com/v1',
  apiKey: '',
  model: 'gpt-image-2',
  prompt: '替换背景为无任何可辨认物体的平滑灰绿渐变：低饱和灰绿与灰橄榄色，左亮右暗的柔和明暗过渡，四角略暗，过渡处没有可见分界线。背景干净无纹理：不要纸张颗粒、不要水渍斑点、不要云絮雾状、不要涂抹笔触、不要建筑、墙面、地面、树木或任何地标。',
};
for (const [id, dv] of Object.entries(DEFAULTS)) {
  const saved = store.get('mie_' + id);
  $(id).value = saved !== null && saved !== '' ? saved : dv;
  $(id).addEventListener('input', () => store.set('mie_' + id, $(id).value));
  $(id).addEventListener('change', () => store.set('mie_' + id, $(id).value));
}
for (const id of ['quality','fitMode','maskMode','scope','engine','resolution']) {
  const v = store.get('mie_' + id);
  if (v !== null && $(id)) $(id).value = v;
  if ($(id)) $(id).addEventListener('change', () => store.set('mie_' + id, $(id).value));
}
// NOTE: updateScopeHint() is deliberately NOT called here. It depends on
// wholeMode(), a `const` declared further down, and calling it this early threw
// "Cannot access 'wholeMode' before initialization" (TDZ) which aborted the
// whole script. applyEngine() (which calls it) runs next to its definition.
refreshSizes();
$('eye').onclick = () => {
  const el = $('apiKey');
  el.type = el.type === 'password' ? 'text' : 'password';
};
function resetConn() {
  for (const [id, dv] of Object.entries(DEFAULTS)) {
    store.del('mie_' + id); $(id).value = dv;
  }
}
window.resetConn = resetConn;
refreshSizes();

// ---- mask layer: white = paint = allow changes ----
const mc = document.createElement('canvas');
const mctx = mc.getContext('2d');

function setMsg(text, kind) {
  const m = $('msg');
  m.className = 'msg' + (kind ? ' ' + kind : '');
  m.textContent = text || '';
  if (!text) m.className = 'msg';
}

function fitCanvas(im) {
  const w = Math.min(im.naturalWidth, 900);
  scale = w / im.naturalWidth;
  cv.width = Math.round(im.naturalWidth * scale);
  cv.height = Math.round(im.naturalHeight * scale);
}

// load a list of files (1..n). The first becomes active; the rest queue up.
function loadFiles(list) {
  const arr = Array.from(list || []).filter(f => /^image\//.test(f.type));
  const bad = Array.from(list || []).filter(f => !/^image\//.test(f.type));
  if (bad.length) setMsg('跳过非图片文件：' + bad.map(f => f.name).join(', '), 'bad');
  if (!arr.length) { if (!bad.length) setMsg('没有选中文件', 'bad'); return; }

  let pending = arr.length;
  arr.forEach((f, idx) => {
    const im = new Image();
    im.onerror = () => { setMsg('图片解码失败：' + f.name, 'bad'); if (--pending === 0) finishLoad(); };
    im.onload = () => {
      const mc = document.createElement('canvas');
      fitCanvas(im);
      mc.width = cv.width; mc.height = cv.height;
      films.push({ name: f.name, im, mc, masked: false, resultB64: null });
      if (--pending === 0) finishLoad(idx === 0);
    };
    im.src = URL.createObjectURL(f);
  });
}

function finishLoad() {
  const n = films.length;
  $('fileName').textContent = n > 1
    ? `已载入 ${n} 张图片`
    : '已选择：' + films[0].name;
  $('editor').style.display = '';
  $('batchRow').style.display = '';
  $('runAll').disabled = n < 2;
  // keep the user's current image selected when appending; only auto-pick when
  // nothing was active yet
  if (active < 0 || active >= n) selectFilm(n - 1, true);
  else { saveActiveMask(); renderThumbs(); }
  renderThumbs();
}

function renderThumbs() {
  const box = $('thumbs');
  box.innerHTML = '';
  films.forEach((f, i) => {
    const d = document.createElement('div');
    d.className = 'th' + (i === active ? ' on' : '') + (f.masked ? ' masked' : '');
    // batch order, always starting at #1 — NOT the same thing as the request
    // slot number (图1 is whatever batch item is being processed right now).
    d.title = `批量第 ${i + 1} 张：${f.name}`
      + (i === active ? '（当前选中，生成时作为「图1 = 内容图」）' : '')
      + (f.masked ? '　已涂遮罩' : '');
    const t = document.createElement('img');
    t.src = f.im.src;
    d.appendChild(t);

    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = '#' + (i + 1);
    d.appendChild(num);

    const dot = document.createElement('span'); dot.className = 'dot';
    d.appendChild(dot);
    d.onclick = () => selectFilm(i);
    box.appendChild(d);
  });
  const nMasked = films.filter(f => f.masked).length;
  $('batchInfo').textContent = `${films.length} 张，其中 ${nMasked} 张已涂遮罩`
    + (films.length > 1 ? '（批量：每张单独发一次请求）' : '');
  $('imgInfo').textContent = img ? `${img.naturalWidth}×${img.naturalHeight}` : '未加载图片';
  applyFollowRatio();   // the followed size depends on which image is selected
  renderSlotPreview();
}

// save the on-screen canvas back into the film that is currently active
function saveActiveMask() {
  if (active < 0 || !films[active]) return;
  const f = films[active];
  f.mc.width = mc.width; f.mc.height = mc.height;
  const c = f.mc.getContext('2d');
  c.clearRect(0, 0, f.mc.width, f.mc.height);
  c.drawImage(mc, 0, 0);
  f.masked = maskHasContent(mc);
}

// count painted pixels in a mask canvas
function maskPixels(canvas) {
  if (!canvas || !canvas.width) return 0;
  const d = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 127) n++;
  return n;
}
const MASK_MIN_PX = 8;   // a deliberate click should already count as a mask
function maskHasContent(canvas) { return maskPixels(canvas) >= MASK_MIN_PX; }

function selectFilm(i, keepZoom) {
  if (active === i) return;
  saveActiveMask();
  active = i;
  const f = films[i];
  img = f.im;
  if (!keepZoom) {
    const w = Math.min(f.im.naturalWidth, 900);
    scale = w / f.im.naturalWidth;
    cv.width = Math.round(f.im.naturalWidth * scale);
    cv.height = Math.round(f.im.naturalHeight * scale);
  }
  mc.width = cv.width; mc.height = cv.height;
  mctx.clearRect(0, 0, mc.width, mc.height);
  if (f.mc.width) mctx.drawImage(f.mc, 0, 0, mc.width, mc.height);
  hasMask = maskHasContent(mc);
  redraw(); updateMaskStat(); renderThumbs();
  $('run').disabled = false;
  // restore a previous result for this image, if any
  if (f.resultB64) showResult(f.resultB64, f.name);
  else { $('outBox').className = 'hint'; $('outBox').textContent = '尚未生成'; $('download').disabled = true; resultURL = null; }
}

function showResult(b64, name) {
  resultURL = 'data:image/png;base64,' + b64;
  $('outBox').className = '';
  $('outBox').innerHTML = '';
  const cap = document.createElement('div');
  cap.className = 'hint'; cap.textContent = name || '';
  const el = document.createElement('img'); el.className = 'out'; el.src = resultURL;
  $('outBox').appendChild(cap); $('outBox').appendChild(el);
  $('download').disabled = false;
}

function loadFile(f) {
  if (!f) return;
  if (!/^image\//.test(f.type)) {
    setMsg('这个文件不是图片：' + f.name + '（type=' + (f.type || '未知') + '）', 'bad');
    return;
  }
  const url = URL.createObjectURL(f);
  const im = new Image();
  im.onerror = () => setMsg('图片解码失败：' + f.name, 'bad');
  im.onload = () => {
    img = im;
    const w = Math.min(im.naturalWidth, 900);
    scale = w / im.naturalWidth;
    cv.width = Math.round(im.naturalWidth * scale);
    cv.height = Math.round(im.naturalHeight * scale);
    mc.width = cv.width; mc.height = cv.height;
    mctx.clearRect(0, 0, mc.width, mc.height);
    hasMask = false;
    redraw();
    $('editor').style.display = '';
    $('drop').style.display = 'none';
    $('imgInfo').textContent = `${im.naturalWidth}×${im.naturalHeight}`;
    $('run').disabled = false;
    updateMaskStat();
  };
  im.src = url;
}

function redraw() {
  if (!img) return;
  // two-pass composite: white mask -> red, then overlay at low alpha
  const tint = document.createElement('canvas');
  tint.width = mc.width; tint.height = mc.height;
  const tc = tint.getContext('2d');
  tc.drawImage(mc, 0, 0);
  tc.globalCompositeOperation = 'source-in';     // keep only painted pixels
  tc.fillStyle = 'rgba(255,40,40,1)';
  tc.fillRect(0, 0, tint.width, tint.height);

  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(img, 0, 0, cv.width, cv.height);
  ctx.globalAlpha = 0.45;
  ctx.drawImage(tint, 0, 0, cv.width, cv.height);
  ctx.globalAlpha = 1;
}

function pos(e) {
  const r = cv.getBoundingClientRect();
  const t = e.touches ? e.touches[0] : e;
  return { x: (t.clientX - r.left) * cv.width / r.width,
           y: (t.clientY - r.top) * cv.height / r.height };
}

function stroke(a, b) {
  // Brush size is given in DISPLAY pixels (what the user sees on the slider).
  // Canvas coords are in internal pixels, so the conversion factor is
  // (canvas.width / displayed width) - it must be MULTIPLIED. The previous
  // version divided, which shrank the brush to a few pixels and made valid
  // strokes register as an empty mask.
  const rect = cv.getBoundingClientRect();
  const factor = rect.width > 0 ? cv.width / rect.width : 1;
  const r = Math.max(2, parseInt($('brush').value, 10) * factor);
  mctx.globalCompositeOperation = 'source-over';
  mctx.strokeStyle = erase ? 'rgba(0,0,0,0)' : '#ffffff';
  mctx.fillStyle = erase ? 'rgba(0,0,0,0)' : '#ffffff';
  mctx.lineWidth = r; mctx.lineCap = 'round'; mctx.lineJoin = 'round';
  if (erase) mctx.globalCompositeOperation = 'destination-out';
  mctx.beginPath(); mctx.moveTo(a.x, a.y); mctx.lineTo(b.x, b.y); mctx.stroke();
  mctx.beginPath(); mctx.arc(b.x, b.y, r / 2, 0, Math.PI * 2); mctx.fill();
  mctx.globalCompositeOperation = 'source-over';
  hasMask = true;
}

let last = null;
cv.addEventListener('pointerdown', e => {
  if (!img) return; drawing = true; last = pos(e);
  stroke(last, last); redraw(); cv.setPointerCapture(e.pointerId);
});
cv.addEventListener('pointermove', e => {
  if (!drawing) return;
  const p = pos(e); stroke(last, p); last = p; redraw();
});
cv.addEventListener('pointerup', e => {
  drawing = false; updateMaskStat(); renderThumbs();
  try { cv.releasePointerCapture(e.pointerId); } catch (_) {}
});

function updateMaskStat() {
  if (!mc.width) return;
  const d = mctx.getImageData(0, 0, mc.width, mc.height).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 127) n++;
  const pct = (n / (mc.width * mc.height) * 100);
  // authoritative state: derived from the pixels, never from a flag
  hasMask = n >= MASK_MIN_PX;
  $('maskStat').textContent = n > 0
    ? `遮罩覆盖率 ${pct.toFixed(2)}%（${n.toLocaleString()} 像素）`
    : '遮罩覆盖率 0%　未涂';
  $('maskStat').style.color = n > 0 ? 'var(--ok)' : 'var(--dim)';
  if (active >= 0 && films[active]) films[active].masked = hasMask;
}
function updateBrush() {
  const rect = cv.getBoundingClientRect();
  const factor = rect.width > 0 ? cv.width / rect.width : 1;
  const canvasR = Math.round(parseInt($('brush').value, 10) * factor);
  $('brushVal').textContent = $('brush').value + '（画布 ' + canvasR + 'px）';
}
$('brush').addEventListener('input', updateBrush); updateBrush();
window.addEventListener('resize', updateBrush);

// Belt and braces: the <label for="file"> opens the picker natively (no JS
// needed), the visible <input> works on its own, and this handler covers the
// case where the user clicks the dashed box itself.
$('drop').addEventListener('click', e => {
  e.preventDefault(); e.stopPropagation();
  $('file').click();
});
$('file').onchange = e => {
  const files = e.target.files;
  if (files && files.length) {
    // APPEND, never replace: picking one file at a time must build up a batch.
    // (The `multiple` attribute is present, but some sandboxed/embedded
    // browsers ignore it and only ever hand back one file.)
    $('fileName').textContent = `正在载入 ${files.length} 个文件…`;
    loadFiles(files);
  }
  e.target.value = '';                  // allow re-picking the same file
};
$('addMore').onclick = () => $('file').click();
$('clearList').onclick = () => {
  if (!films.length) return;
  if (!confirm('清空已载入的图片列表？未生成的结果会丢失。')) return;
  films = []; active = -1; img = null; hasMask = false;
  $('thumbs').innerHTML = '';
  $('editor').style.display = 'none';
  $('fileName').textContent = '已清空，请重新选择图片';
  $('run').disabled = true; $('runAll').disabled = true;
  $('outBox').className = 'hint'; $('outBox').textContent = '尚未生成';
  $('download').disabled = true; resultURL = null;
  $('imgInfo').textContent = '未加载图片';
  $('maskStat').textContent = '遮罩覆盖率 0%';
};
$('drop').addEventListener('dragover', e => { e.preventDefault(); $('drop').classList.add('over'); });
$('drop').addEventListener('dragleave', () => $('drop').classList.remove('over'));
$('drop').addEventListener('drop', e => {
  e.preventDefault(); $('drop').classList.remove('over');
  const files = e.dataTransfer.files;
  if (files && files.length) {
    $('fileName').textContent = `正在载入 ${files.length} 个文件…`;
    loadFiles(files);                      // append, same as the picker
  }
});
$('erase').onclick = () => { erase = !erase; $('erase').textContent = erase ? '画笔模式' : '橡皮模式'; };
$('clear').onclick = () => {
  mctx.clearRect(0, 0, mc.width, mc.height); hasMask = false;
  saveActiveMask(); redraw(); updateMaskStat(); renderThumbs();
};
$('invert').onclick = () => {
  if (!mc.width) return;
  const d = mctx.getImageData(0, 0, mc.width, mc.height);
  for (let i = 0; i < d.data.length; i += 4) {
    const on = d.data[i + 3] > 127;
    d.data[i] = 255; d.data[i + 1] = 255; d.data[i + 2] = 255;
    d.data[i + 3] = on ? 0 : 255;
  }
  mctx.putImageData(d, 0, 0); hasMask = true;
  saveActiveMask(); redraw(); updateMaskStat(); renderThumbs();
};
$('fit').onclick = () => { if (img) loadFileFromImg(); };
function loadFileFromImg() {
  const w = Math.min(img.naturalWidth, 900);
  const s = w / img.naturalWidth;
  const old = document.createElement('canvas');
  old.width = mc.width; old.height = mc.height;
  old.getContext('2d').drawImage(mc, 0, 0);
  cv.width = Math.round(img.naturalWidth * s); cv.height = Math.round(img.naturalHeight * s);
  mc.width = cv.width; mc.height = cv.height;
  mctx.drawImage(old, 0, 0, mc.width, mc.height);
  redraw(); updateMaskStat();
}

// ---- API helpers ----
async function testConn() {
  setMsg('测试中…');
  try {
    const r = await fetch(api('/api/ping'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ base_url: base(), api_key: key() })
    });
    const j = await r.json();
    setMsg(j.message, j.ok ? 'ok' : 'bad');
  } catch (e) { setMsg('本地服务异常: ' + e.message, 'bad'); }
}
$('test').onclick = testConn;

$('listModels').onclick = async () => {
  setMsg('获取模型列表…');
  try {
    const r = await fetch(api('/api/models'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ base_url: base(), api_key: key() })
    });
    const j = await r.json();
    if (!j.ok) { setMsg(j.message, 'bad'); return; }
    const dl = $('models'); dl.innerHTML = '';
    j.models.forEach(m => { const o = document.createElement('option'); o.value = m; dl.appendChild(o); });
    setMsg(`拿到 ${j.models.length} 个模型。含 image 关键字的：\n` +
           (j.image_models.length ? j.image_models.join('\n') : '（无）'), 'ok');
  } catch (e) { setMsg('本地服务异常: ' + e.message, 'bad'); }
};

function blobOf(canvas) {
  return new Promise(res => canvas.toBlob(res, 'image/png'));
}

// convert any loaded image into a Blob suitable for upload.
// Re-encodes at a bounded size: the API target is at most 1536 px on the long
// side, so sending a 20 MP original just burns upload time and can exceed the
// multipart limits. PNG for line art, JPEG when it is large, to stay small.
const MAX_EDGE = 1600;
const MAX_BYTES = 4 * 1024 * 1024;
function imageBlob(film) {
  return new Promise(res => {
    const im = film.im;
    const s = Math.min(1, MAX_EDGE / Math.max(im.naturalWidth, im.naturalHeight));
    const w = Math.max(1, Math.round(im.naturalWidth * s));
    const h = Math.max(1, Math.round(im.naturalHeight * s));
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const g = c.getContext('2d');
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);   // flatten any alpha
    g.drawImage(im, 0, 0, w, h);

    c.toBlob(png => {
      if (png && png.size <= MAX_BYTES) {
        res({ blob: png, ext: 'png', size: png.size, w, h });
        return;
      }
      c.toBlob(jpg => {
        const b = jpg || png;
        res({ blob: b, ext: 'jpg', size: b ? b.size : 0, w, h });
      }, 'image/jpeg', 0.92);
    }, 'image/png');
  });
}

async function callEdit(film) {
  const eng = (typeof $('engine') !== 'undefined' && $('engine')) ? $('engine').value : 'gpt';
  const whole = $('scope').value === 'whole';
  // no image selected -> text-to-image; otherwise image-to-image
  const operation = film ? 'i2i' : 't2i';

  const fd = new FormData();
  fd.append('engine', eng);
  fd.append('operation', operation);
  fd.append('base_url', base());
  fd.append('api_key', key());
  fd.append('model', $('model').value.trim());
  fd.append('prompt', $('prompt').value);
  fd.append('size', $('size').value);
  fd.append('quality', $('quality') ? $('quality').value : 'low');
  fd.append('resolution', $('resolution') ? $('resolution').value : '1k');
  fd.append('pad_mode', $('fitMode').value);
  fd.append('mask_mode', $('maskMode').value);
  fd.append('scope', $('scope').value);

  // reference images — attached to THIS request (not queued as jobs).
  // Names must be distinct parts: parse_multipart keys files by field name, so
  // repeated "ref_file" would silently collapse to the last one.
  fd.append('ref_count', String(refs.length));
  fd.append('ref_roles', JSON.stringify(refs.map(r => r.role)));
  for (let i = 0; i < refs.length; i++) {
    const rinfo = await imageBlob({ im: refs[i].im });
    fd.append('ref_file_' + i, rinfo.blob,
              'ref' + (i + 1) + '_' + refs[i].role + '.' + rinfo.ext);
  }
  diag.refs = refs.length
    ? refs.map((r, i) => `图${i + (maskSlotUsed() ? 3 : 2)}=${r.role}(${r.name})`).join(' ')
    : '（无）';

  let maskInfo = '（文生图，无输入图片）';
  if (film) {
    const info = await imageBlob(film);
    fd.append('image_file', info.blob, 'image.' + info.ext);
    diag.image = `${info.w}x${info.h} ${info.ext} ${(info.size / 1024).toFixed(0)} KB`;
    maskInfo = '（整张图模式，无遮罩）';
    if (!whole) {
      const mb = await blobOf(film.mc);
      fd.append('mask_file', mb, 'mask.png');
      maskInfo = `mask.png ${(mb.size / 1024).toFixed(0)} KB`;
    }
  } else {
    diag.image = '（无）';
  }
  diag.mask = maskInfo;
  diag.engine = eng;

  const t0 = Date.now();
  const ctl = new AbortController();
  const TO = 15 * 60 * 1000;               // 15 min hard ceiling
  const tid = setTimeout(() => ctl.abort(), TO);
  let resp, text;
  try {
    resp = await fetch(api('/api/edit'), { method: 'POST', body: fd, signal: ctl.signal });
    text = await resp.text();
  } catch (e) {
    clearTimeout(tid);
    return { ok: false, message: e.name === 'AbortError'
      ? `请求超时（超过 ${TO / 60000} 分钟）\n本地服务地址：${api('/api/edit')}`
      : `网络请求失败：${e.name}: ${e.message}\n本地服务地址：${api('/api/edit')}` };
  }
  clearTimeout(tid);
  diag.ms = Date.now() - t0;

  if (!resp.ok) {
    return { ok: false, message: `本地服务返回 HTTP ${resp.status}\n${String(text).slice(0, 600)}` };
  }
  try {
    return JSON.parse(text);
  } catch (e) {
    return { ok: false, message: `返回内容不是 JSON（前 600 字符）：\n${String(text).slice(0, 600)}` };
  }
}

const wholeMode = () => $('scope').value === 'whole';

function updateScopeHint() {
  const w = wholeMode();
  // whole mode is the default: hide the paint tools entirely so nothing implies
  // that masking is required
  $('maskStat').style.display = w ? 'none' : '';
  $('maskTools').style.display = w ? 'none' : '';
  $('scopeHint').textContent = w
    ? '整张图直接交给模型重绘，不需要涂任何东西。'
    : '切换到左边画布——画笔工具会出现在画布下方，按住左键拖动涂出要修改的地方。';
}
$('scope').addEventListener('change', () => {
  store.set('mie_scope', $('scope').value);
  updateScopeHint();
  // the red mask guide takes image[1] in mask mode, so every reference's 图N
  // badge shifts by one — re-render the rows, not only the legend.
  renderRefs();
});
updateScopeHint();
// Draw the numbering legend once at start-up, so the two numbering systems are
// visible before anything is uploaded. This call site is deliberately below
// every declaration it depends on — check_tdz.py guards against regressions.
renderSlotPreview();

function preflight() {
  if (!films.length) { setMsg('请先选择图片', 'bad'); return false; }
  if (!base() || !key()) { setMsg('请先填 Base URL 和 API Key', 'bad'); return false; }
  if (!$('prompt').value.trim()) { setMsg('请填写提示词', 'bad'); return false; }
  // reference-image guards, checked here so the user sees the reason before any
  // request is spent. The server enforces the same rules again.
  if (refs.length) {
    const f = engFlags();
    if (!f.multi) {
      setMsg('当前引擎「' + (ENGINE_DEFAULTS[currentEngine()] || {}).model + '」不接受多于 1 张输入图片'
        + '（上游 HTTP 400）。\n你已添加 ' + refs.length + ' 张参考图。\n\n'
        + '请改用「GPT Image 2」引擎，或清空参考图列表。', 'bad');
      return false;
    }
    const max = maxRefsNow();
    if (refs.length > max) {
      const maskSlot = engFlags().mask && !wholeModeNow();
      setMsg('参考图数量超出上限：当前模式下最多 ' + max + ' 张。\n\n'
        + '原因：接口只接受 4 张图（image / image[1] / image[2] / image[3]），'
        + '本请求固定占用 1 张内容图'
        + (maskSlot ? ' + 1 张红色标记图' : '') + '，剩下的位置才是参考图。\n\n'
        + (maskSlot ? '二选一：把「修改范围」改成「整张图」腾出位置，或减少参考图。'
                    : '请减少参考图。'), 'bad');
      return false;
    }
  }
  return true;
}

// shared cores used by both the single and the batch buttons
async function generateOne(i, tag) {
  const f = films[i];
  if (!wholeMode() && !maskHasContent(f.mc)) {
    return { i, ok: false, message: f.name + '：未涂遮罩' };
  }
  // resolve the size for THIS image first: with 跟随原图比例 on, a batch of
  // mixed aspect ratios must each generate at their own best-fit size.
  applyFollowRatio(f);
  const j = await callEdit(f);
  if (j.ok) { f.resultB64 = j.image_b64; f.masked = true; }
  else if (!j.message && !j.raw) { j.message = '服务端未返回错误说明，原始响应为空'; }
  return { i, ok: j.ok, message: j.message, name: f.name,
           slotMap: j.slot_map, partsSent: j.parts_sent,
           size: ($('size') ? $('size').value : '') };
}

/** One-line echo of what the server confirms it actually sent. */
function sentLine(r) {
  if (!r || !r.ok) return '';
  const sz = r.size ? `\n尺寸：${r.size}` + (followRatioOn() ? '（跟随原图自动选定）' : '') : '';
  if (Array.isArray(r.slotMap) && r.slotMap.length) {
    const plan = r.slotMap.map(s => '图' + s.index + '=' + s.label).join(' | ');
    const parts = Array.isArray(r.partsSent) ? r.partsSent.join(' + ') : '';
    return sz + `\n已发送的图片：${plan}` + (parts ? `\n实际 parts：${parts}` : '');
  }
  return sz + (Array.isArray(r.partsSent) ? `\n实际 parts：${r.partsSent.join(' + ')}` : '');
}

let busy = false;
function setBusy(v) {
  busy = v;
  $('run').disabled = v;
  $('runAll').disabled = v || films.length < 2;
}

$('run').onclick = async () => {
  if (busy) return;
  if (!base() || !key()) { setMsg('请先填 Base URL 和 API Key', 'bad'); return; }
  if (!$('prompt').value.trim()) { setMsg('请填写提示词', 'bad'); return; }

  const film = (active >= 0 && films[active]) ? films[active] : null;
  if (!film) {
    // no image loaded -> text-to-image
    setBusy(true);
    const t0 = Date.now();
    try {
      setMsg('文生图中…（已等待 0s）');
      const timer = setInterval(() => {
        if (busy) setMsg(`文生图中…（已等待 ${Math.round((Date.now() - t0) / 1000)}s）`);
      }, 1000);
      const j = await callEdit(null);
      clearInterval(timer);
      if (j.ok) {
        showResult(j.image_b64, '文生图结果');
        setMsg(`文生图完成，用时 ${Math.round((Date.now() - t0) / 1000)}s`, 'ok');
      } else {
        setMsg('文生图失败：\n' + (j.message || '未知错误') + '\n\n—— 诊断信息 ——\n' + diagText(), 'bad');
      }
    } catch (e) {
      setMsg('异常：' + e.message, 'bad');
    } finally { setBusy(false); }
    return;
  }

  saveActiveMask();
  const i = active;
  if (!wholeMode()) {
    const px = maskPixels(film.mc);
    if (px < MASK_MIN_PX) {
      setMsg(`画布上没有检测到涂改区域（当前 ${px} 像素）。\n\n`
        + `两种解决办法：\n`
        + `1) 在画布上【按住鼠标左键拖动】涂出要修改的地方（不是单击）；`
        + `涂的过程中左上角会显示「遮罩覆盖率」，数字变大就说明涂上了。\n`
        + `2) 不想指定范围，就把「修改范围」改成「整张图（无需涂遮罩）」，`
        + `然后直接点生成。\n\n`
        + `如果已经涂了、覆盖率却一直是 0%，请把左上角显示的数字告诉我。`, 'bad');
      return;
    }
  }
  setBusy(true);
  const t0 = Date.now();
  try {
    setMsg('生成中…（已等待 0s）');
    const timer = setInterval(() => {
      if (busy) setMsg(`生成中…（已等待 ${Math.round((Date.now() - t0) / 1000)}s）`);
    }, 1000);
    const r = await generateOne(i);
    clearInterval(timer);
    if (r.ok) {
      showResult(films[i].resultB64, films[i].name);
      setMsg(`完成，用时 ${Math.round((Date.now() - t0) / 1000)}s` + sentLine(r), 'ok');
    } else {
      setMsg('生成失败：\n' + (r.message || '未知错误') + '\n\n—— 诊断信息 ——\n' + diagText(), 'bad');
    }
    renderThumbs();
  } catch (e) {
    setMsg('异常：' + e.message, 'bad');
  } finally { setBusy(false); }
};

$('runAll').onclick = async () => {
  if (busy || !preflight()) return;
  const todo = films.map((f, i) => i)
    .filter(i => wholeMode() || maskHasContent(films[i].mc));
  if (!todo.length) {
    setMsg('没有可生成的图片。整体模式下所有图片都可生成；遮罩模式下请先涂遮罩。', 'bad');
    return;
  }
  const what = wholeMode() ? '整张图重绘' : '按遮罩局部修改';
  if (!confirm(`将对 ${todo.length} 张图依次生成（${what}，共用同一提示词与参数）。继续？`)) return;

  setBusy(true);
  const t0 = Date.now();
  const results = [];
  try {
    for (let k = 0; k < todo.length; k++) {
      const i = todo[k];
      setMsg(`批量生成中… ${k + 1}/${todo.length}　批量 #${i + 1}：${films[i].name}\n`
             + `尺寸 ${$('size').value}${followRatioOn() ? '（跟随原图自动选定）' : ''}`
             + `　已用时 ${Math.round((Date.now() - t0) / 1000)}s`);
      const r = await generateOne(i);
      results.push(r);
      if (r.ok) { films[i].resultB64 = films[i].resultB64; }
      selectFilmSilent(i);
      renderThumbs();
    }
    const okN = results.filter(r => r.ok).length;
    if (!okN) {
      setMsg('批量生成全部失败。\n' + results.map(r => (r.name || '') + '：' + (r.message || '')).join('\n')
             + '\n\n—— 诊断信息 ——\n' + diagText(), 'bad');
    }
    // show every result in the output panel
    $('outBox').className = '';
    $('outBox').innerHTML = '';
    results.forEach(r => {
      if (r.ok) {
        const cap = document.createElement('div'); cap.className = 'hint'; cap.textContent = r.name;
        const el = document.createElement('img'); el.className = 'out';
        el.src = 'data:image/png;base64,' + films[r.i].resultB64;
        $('outBox').appendChild(cap); $('outBox').appendChild(el);
      }
    });
    if (okN) { resultURL = 'data:image/png;base64,' + films[results.find(r => r.ok).i].resultB64; $('download').disabled = false; }
    setMsg(`批量完成：成功 ${okN} / ${results.length}，用时 ${Math.round((Date.now() - t0) / 1000)}s\n`
           + results.map(r => (r.ok ? 'OK   ' : 'FAIL ') + (r.name || '') + (r.ok ? '' : '  ' + (r.message || ''))).join('\n')
           + (results.find(r => r.ok) ? sentLine(results.find(r => r.ok)) : ''),
           okN === results.length ? 'ok' : 'bad');
  } catch (e) {
    setMsg('异常：' + e.message, 'bad');
  } finally { setBusy(false); }
};

$('copyMask').onclick = () => {
  if (active < 0) return;
  saveActiveMask();
  const src = films[active].mc;
  films.forEach((f, i) => {
    if (i === active) return;
    f.mc.width = src.width; f.mc.height = src.height;
    const c = f.mc.getContext('2d');
    c.clearRect(0, 0, f.mc.width, f.mc.height);
    c.drawImage(src, 0, 0);
    f.masked = true;
  });
  renderThumbs();
  setMsg(`已把第 ${active + 1} 张的遮罩复制到其余 ${films.length - 1} 张。`, 'ok');
};

// switch film without re-rendering thumbs (used inside the batch loop)
function selectFilmSilent(i) {
  saveActiveMask();
  active = i;
  const f = films[i];
  img = f.im;
  const w = Math.min(f.im.naturalWidth, 900);
  scale = w / f.im.naturalWidth;
  cv.width = Math.round(f.im.naturalWidth * scale);
  cv.height = Math.round(f.im.naturalHeight * scale);
  mc.width = cv.width; mc.height = cv.height;
  mctx.clearRect(0, 0, mc.width, mc.height);
  if (f.mc.width) mctx.drawImage(f.mc, 0, 0, mc.width, mc.height);
  hasMask = maskHasContent(mc);
  redraw(); updateMaskStat();
}

$('download').onclick = () => {
  if (!resultURL) return;
  const a = document.createElement('a');
  const nm = (active >= 0 && films[active] ? films[active].name.replace(/\.[^.]+$/, '') : 'result');
  a.href = resultURL; a.download = nm + '_edited.png'; a.click();
};
</script></body></html>
"""



# --------------------------------------------------------------------------
# 画廊（快捷打开相关图片）
# --------------------------------------------------------------------------
# 按修改时间倒序列出工作区里的图片，并分三类：
#   成果 = 交付件（round 根层的 out_*、compose 的 out/final/deliver 目录）
#   局部 = 局部改图与对照件（round_*/lab 的改图实验件、checks 的检查图、
#          以及名字里带 redmark / _zoom_ / _sheet / contact / palette / _crop 的对照物）
#   过程 = 其余（work/、input/、target*、ref_*、round_lib、compose 杂项等）
# 规则写在 gallery_category() 里，页面上每张卡片都带分类标签，便于核对与调整。
# 只服务白名单根目录内的图片后缀，避免变成任意文件读取。
_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GALLERY_ROOTS = [
    os.path.join(_WORKSPACE, "style-distill"),
    os.path.join(_WORKSPACE, "compose"),
]
GALLERY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
GALLERY_REL_BASE = _WORKSPACE
GALLERY_SCAN_LIMIT = 3000          # 扫描上限（纯安全阀）
GALLERY_PROCESS_CAP = 120          # 只有"过程"封顶：成果与局部一张不落

CATEGORIES = (("all", "全部"), ("deliver", "成果"), ("candidate", "候选"), ("input", "输入"),
              ("reference", "参考"), ("detail", "局部"), ("compare", "对照"), ("process", "过程"))
CAT_LABEL = {"deliver": "成果", "candidate": "候选", "input": "输入", "reference": "参考",
             "detail": "局部", "compare": "对照", "process": "过程"}
# 规则经 2026-09-22 两次审计修订（看图 + 逐条清单 + 断言），详见 fix_gallery_categories*.py
DETAIL_DIRS = {"lab"}                      # 局部改图实验件（round_*/lab）
DETAIL_HINTS = ("redmark", "_zoom")         # 涂红预览、局部放大对照
DETAIL_SUFFIX = ("zoom",)                  # …_zoom.png（名字末尾的 zoom）
DETAIL_MASKISH = ("mask_preview", "mask_guide", "line_mask")
# 对照：输出 vs 参考的对照条、诊断拼版
COMPARE_DIRS = {"checks", "diag"}
COMPARE_HINTS = ("compare", "side_by_side", "_vs_", "_cmp", "check", "grid",
                 "_sheet", "contact", "_ab_")
SCRATCH_HINTS = ("_smoke", "_dry", "_budget", "_ok_test", "_ledger")   # 我自己的测试残留 → 过程
# 参考图：参考目录 / 名字明示 / 本项目 input 的 A-C 内容、B-D 参考约定
REFERENCE_DIRS = {"ref_xiami", "references", "refs", "pose_ref", "pose_ref2"}
REFERENCE_HINTS = ("style_ref", "pose_ref", "pose_style", "identity_ref", "face_ref",
                   "char_ref", "ref_face", "ref_style")
REFERENCE_INPUT_PREFIX = ("b_", "d_")    # 角色分配约定：A/C＝内容图，B/D＝参考图
CONTENT_INPUT_PREFIX = ("a_", "c_")      # 仅用于说明，不参与判定
DELIVER_DIRS = {"out", "out2", "final", "final2", "deliver"}
# 旧管线 compose/ 没有交付清单，只能按"成品名"判：以下名字是图层/原图/对照/检查，不算成果
DELIVER_DENY = ("original", "background_only", "bg_only", "bg", "mask", "preview",
                "side_by_side", "compare", "check", "tech")
ROUND_ROOT_DENY = {"round_lib"}            # round_lib 以 "round_" 开头，但不是轮次目录
PROCESS_DIRS = {"_probe"}                 # 我的草稿区（审阅拼版等）→ 一律过程

ATTACHMENTS_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "attachments")
# 索引路径可用环境变量覆盖：ZCode 侧的 zcode_inputs.py 会生成同 schema 的索引
# （keys: paths / excluded / unreachable），指过来即可切换数据源，
# **不需要动 gallery_category 的判定逻辑**（那套 64 条回归断言必须保持通过）。
USER_INPUT_INDEX = os.environ.get("GALLERY_USER_INPUTS") or os.path.join(
    GALLERY_REL_BASE, "style-distill", "round_lib", "user_inputs.json")
_USER_INPUTS: dict = {}


def user_inputs() -> dict:
    """读"用户上传图"索引：{paths: set(工作区内相对路径, 小写), attachments: [ {id,name,…} ]}。

    索引由 style-distill/build_user_inputs_index.py 生成：它从会话日志取 role=user 的图片附件，
    再与工作区图片做 sha256 配对——所以这是**证据**，不是命名约定。
    """
    import json

    if _USER_INPUTS:
        return _USER_INPUTS
    paths, excluded, unreachable = set(), 0, 0
    try:
        with open(USER_INPUT_INDEX, encoding="utf-8") as f:
            d = json.load(f)
        paths = {str(x).lower() for x in (d.get("paths") or [])}
        excluded = len(d.get("excluded") or [])
        unreachable = len(d.get("unreachable") or [])
    except (OSError, ValueError):
        pass
    _USER_INPUTS["paths"] = paths
    _USER_INPUTS["excluded"] = excluded
    _USER_INPUTS["unreachable"] = unreachable
    return _USER_INPUTS


_ACCEPTED_CACHE: dict[str, set] = {}


def _accepted_names(round_abs: str) -> set:
    """读该轮 README 里带 ✓ 的交付件名（README 是"已认可"的权威来源）。"""
    import os
    import re

    if round_abs in _ACCEPTED_CACHE:
        return _ACCEPTED_CACHE[round_abs]
    found: set = set()
    p = os.path.join(round_abs, "README.md")
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if "✓" not in line:
                    continue
                found.update(re.findall(r"[A-Za-z0-9_\-]+\.(?:png|jpg|jpeg|webp|gif)", line))
    except OSError:
        found = set()
    # ⚠ 统一转小写再比：调用方传进来的 name 是小写的，而 README 里写的是 out_v12B.png（大写 B）
    _ACCEPTED_CACHE[round_abs] = {n.lower() for n in found}
    return _ACCEPTED_CACHE[round_abs]


def gallery_category(rel: str) -> str:
    """按路径特征分到 成果 / 候选 / 局部 / 对照 / 过程。

    优先级：局部（特征最具体） → 对照 → 成果/候选（看 README 有无 ✓） → 过程。
    测试残留（_smoke/_dry/…）先落"过程"，免得混进"对照"或"局部"。
    """
    import os

    name = os.path.basename(rel).lower()
    segs = [s.lower() for s in rel.replace("\\", "/").split("/")]
    dirs = segs[:-1]
    stem = name.rsplit(".", 1)[0]

    # ---- 我自己的测试残留/草稿区：一律过程（要排在"对照/局部"之前）
    if name.startswith(SCRATCH_HINTS) or (set(dirs) & PROCESS_DIRS):
        return "process"

    # ---- 输入：用户自己上传的图（按 sha256 与会话记录配对）
    if rel.replace("\\", "/").lower() in user_inputs()["paths"]:
        return "input"

    # ---- 参考图：参考目录、名字明示、input 里的 B/D 约定
    in_input = "input" in dirs
    if (set(dirs) & REFERENCE_DIRS
            or any(h in name for h in REFERENCE_HINTS)
            or (in_input and name.startswith(REFERENCE_INPUT_PREFIX))):
        return "reference"

    # ---- 局部：局部改图实验件、涂红预览、局部放大对照、蒙版素材
    if set(dirs) & DETAIL_DIRS:
        return "detail"
    if (any(h in name for h in DETAIL_HINTS) or stem.endswith(DETAIL_SUFFIX)
            or name.startswith(("mask",)) or any(h in name for h in DETAIL_MASKISH)):
        return "detail"

    # ---- 对照：对照条与诊断拼版
    if set(dirs) & COMPARE_DIRS or any(h in name for h in COMPARE_HINTS):
        return "compare"

    # ---- 成果 / 候选：轮次根层的 out_*，按该轮 README 有无 ✓ 区分
    if name.startswith("out_"):
        round_dirs = [d for d in dirs if d.startswith("round_") and d not in ROUND_ROOT_DENY]
        if round_dirs and not (set(dirs) & {"work", "lab", "input", "checks"}):
            # 注意：要拼**完整相对目录**（含 style-distill 那一层），
            # 早先只拼了轮次目录名，README 永远读不到 → 成果全被误判成候选。
            round_abs = os.path.join(GALLERY_REL_BASE, *dirs)
            return "deliver" if name in _accepted_names(round_abs) else "candidate"
    if set(dirs) & DELIVER_DIRS and not any(k in name for k in DELIVER_DENY):
        return "deliver"

    return "process"


def scan_roots():
    """只扫**本轮**目录（style-distill/<最新的 round_*>）。

    口径来自用户："只从最近这轮开始，从这轮起才计入，之前的全部隐藏"。
    自动识别本轮，所以开新一轮时画廊会自动跟过去，不需要改配置。
    """
    latest = latest_round()
    if latest:
        d = os.path.join(GALLERY_REL_BASE, "style-distill", latest)
        if os.path.isdir(d):
            return [d]
    return list(GALLERY_ROOTS)          # 兜底：识别不到本轮时不至于空白


def _gallery_scan():
    """按修改时间倒序列出**本轮**目录里的图片（更早的轮次不计入、不显示）。"""
    import os

    found = []
    for root in scan_roots():
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("node_modules", "__pycache__", ".git")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in GALLERY_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                try:
                    rel = os.path.relpath(full, GALLERY_REL_BASE).replace("\\", "/")
                except ValueError:
                    rel = full.replace("\\", "/")
                found.append({"mtime": st.st_mtime, "size": st.st_size,
                              "full": full, "rel": rel,
                              "cat": gallery_category(rel)})
                if len(found) >= GALLERY_SCAN_LIMIT:
                    break
    found.sort(key=lambda r: r["mtime"], reverse=True)
    return found


def latest_round() -> str:
    """最近的这一轮：style-distill 下按 mtime 最新的 round_* 目录（排除 round_lib）。"""
    import glob

    best, best_m = "", -1.0
    for d in glob.glob(os.path.join(GALLERY_REL_BASE, "style-distill", "round_*")):
        base = os.path.basename(d)
        if base in ROUND_ROOT_DENY or not os.path.isdir(d):
            continue
        try:
            m = os.stat(d).st_mtime
        except OSError:
            continue
        if m > best_m:
            best, best_m = base, m
    return best


def gallery_select():
    """成果与局部全取；过程只取最新 GALLERY_PROCESS_CAP 张。

    每行带 scope：属于**最近这一轮**的为 latest，其余 older（页面默认只显示 latest）。
    """
    rows = _gallery_scan()
    latest = latest_round()
    kept, process_seen = [], 0
    for row in rows:
        if row["cat"] == "process":
            process_seen += 1
            if process_seen > GALLERY_PROCESS_CAP:
                continue
        kept.append(row)
    counts = {key: 0 for key, _ in CATEGORIES}
    counts["all"] = len(kept)
    for row in kept:
        counts[row["cat"]] = counts.get(row["cat"], 0) + 1
    counts["latest"] = len(kept)                 # 现在"计入的"就是"本轮的"
    counts["older"] = 0                          # 更早的轮次不计入，也不显示
    counts["_latest_round"] = latest
    return kept, counts


def _input_note() -> str:
    """页头补一句"还有多少张上传没进仓库"——那些无法在画廊里定位，如实说明而不是假装有卡片。

    原因（实测）：附件元数据里的 attachmentId 与附件库里的对象文件名**不是同一个哈希**
    （对象是规范化后的副本，ID 是原文件的哈希），所以按 ID 拼不出对象路径。
    """
    ui = user_inputs()
    n_ex, n_un = int(ui.get("excluded") or 0), int(ui.get("unreachable") or 0)
    if not n_ex:
        return ""
    return (f" · 「输入」只计入最近一次上传（{len(ui.get('paths') or [])} 张）；"
            f"更早的 {n_ex} 张按你的要求不计入（其中 {n_un} 张只在附件库、无法在此定位）")


def gallery_html() -> str:
    import html
    import time

    rows, counts = gallery_select()
    cards = []
    for row in rows:
        full, rel = row["full"], row["rel"]
        cat = row["cat"]
        q = urllib.parse.quote(full)
        cards.append(
            f'<figure class="card" data-cat="{cat}">'
            f'<img class="thumb" loading="lazy" src="/gallery/img?p={q}"'
            f' data-full="/gallery/img?p={q}"'
            f' data-file="{html.escape(full, quote=True)}"'
            f' data-rel="{html.escape(rel, quote=True)}"'
            f' data-name="{html.escape(rel.rsplit("/", 1)[-1], quote=True)}"'
            f' alt="{html.escape(rel)}">'
            f'<figcaption><span class="chip c-{cat}">{CAT_LABEL.get(cat, cat)}</span>'
            f'<b>{html.escape(rel.rsplit("/", 1)[-1])}</b>'
            f'<span class="meta">{time.strftime("%m-%d %H:%M", time.localtime(row["mtime"]))}'
            f' · {max(1, row["size"] // 1024)} KB</span>'
            f'<span class="path">{html.escape(rel)}</span>'
            f'<button type="button" data-copy="{html.escape(full, quote=True)}">复制路径</button>'
            '</figcaption></figure>')

    tabs = "".join(
        f'<button type="button" class="tab{" on" if key == "all" else ""}" data-cat="{key}">'
        f'{label}<span class="n">{counts.get(key, 0)}</span></button>'
        for key, label in CATEGORIES)

    head = (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>图片 · mantu</title><style>'
        'body{margin:0;background:#14161a;color:#e8eaed;font:14px/1.5 system-ui,"Segoe UI",sans-serif}'
        'body.locked{overflow:hidden}'
        'header{position:sticky;top:0;z-index:3;background:#1b1e24;border-bottom:1px solid #2b3038;'
        'padding:10px 14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}'
        'h1{font-size:15px;margin:0;font-weight:600}'
        '.tabs{display:flex;gap:6px}'
        '.tab{background:#22262d;border:1px solid #333a44;color:#cfd6df;border-radius:6px;'
        'padding:4px 10px;font:inherit;font-size:13px;cursor:pointer;display:inline-flex;gap:6px}'
        '.tab:hover{background:#2b3038}'
        '.tab.on{background:#2f6fd0;border-color:#3f7fe0;color:#fff}'
        '.tab .n{opacity:.75;font-variant-numeric:tabular-nums}'
        'input{background:#0f1115;border:1px solid #333a44;color:inherit;border-radius:6px;'
        'padding:6px 10px;font:inherit;min-width:220px}'
        '.hint{color:#98a2b3;font-size:12px}'
        'main{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;padding:14px}'
        '.card{margin:0;background:#1b1e24;border:1px solid #2b3038;border-radius:8px;overflow:hidden}'
        '.thumb{display:block;width:100%;height:200px;object-fit:contain;background:#0f1115;cursor:zoom-in}'
        '.thumb:hover{outline:2px solid #3f7fe0;outline-offset:-2px}'
        'figcaption{padding:8px;display:grid;gap:2px}'
        'figcaption b{font-size:12.5px;word-break:break-all}'
        '.chip{justify-self:start;font-size:11px;padding:1px 6px;border-radius:10px;border:1px solid}'
        '.c-deliver{color:#8fe388;border-color:#2f5d33}'
        '.c-detail{color:#ffd27f;border-color:#5d4a2f}'
        '.c-process{color:#9fb4cc;border-color:#33445d}.c-candidate{color:#ffb4e6;border-color:#5d3350}.c-compare{color:#9adbd3;border-color:#2f4f4c}.c-reference{color:#c9b6ff;border-color:#443a63}.c-input{color:#ffd6a5;border-color:#5d4626}'
        '.meta{color:#98a2b3;font-size:11.5px}'
        '.path{color:#7d8794;font-size:11px;word-break:break-all}'
        'button{margin-top:6px;background:#22262d;border:1px solid #333a44;color:#cfd6df;border-radius:5px;'
        'padding:4px 8px;font:inherit;font-size:12px;cursor:pointer}'
        'button:hover{background:#2b3038}'
        '#lb{position:fixed;inset:0;z-index:9;background:rgba(8,10,13,.94);display:flex;'
        'flex-direction:column}'
        '#lb[hidden]{display:none}'
        '.lb-bar{display:flex;gap:10px;align-items:center;padding:8px 12px;background:#1b1e24;'
        'border-bottom:1px solid #2b3038;flex-wrap:wrap}'
        '.lb-bar .name{font-weight:600}'
        '.lb-bar .rel{color:#7d8794;font-size:11.5px;word-break:break-all}'
        '.lb-bar .sp{flex:1}'
        '.lb-bar a{color:#8fb8ff;text-decoration:none;border:1px solid #333a44;border-radius:5px;'
        'padding:4px 8px;font-size:12px}'
        '/* 居中改用子元素 margin:auto：flex 的 align/justify:center 在内容溢出时会吃掉滚动范围，'
        '   导致放大后拖不到最上/最下（经典 flexbox 陷阱）。 */'
        '.lb-stage{flex:1;position:relative;overflow:auto;display:flex}'
        '#lb-img{max-width:100%;max-height:100%;object-fit:contain;cursor:zoom-in;margin:auto}'
        '#lb-img.zoom{max-width:none;max-height:none;cursor:grab;touch-action:none}'
        '#lb-img.dragging{cursor:grabbing}'
        '.lb-nav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:64px;'
        'background:rgba(34,38,45,.85);border:1px solid #333a44;color:#e8eaed;font-size:20px;'
        'border-radius:8px;cursor:pointer;margin:0}'
        '#lb-prev{left:10px}'
        '#lb-next{right:10px}'
        '.lb-pos{color:#98a2b3;font-size:12px;font-variant-numeric:tabular-nums}.lb-zoom{color:#cfd6df;font-size:12px;font-variant-numeric:tabular-nums;min-width:56px;text-align:right}'
        '</style></head><body>'
        '<header><h1>图片</h1>'
        f'<div class="tabs">{tabs}</div>'
        '<input id="q" placeholder="按文件名 / 路径筛选（如 v2、round_arcade）">'
        f'<span class="hint" id="hint">只计入本轮「{counts.get("_latest_round") or "未识别"}」'
        f'（{counts.get("latest", 0)} 张）；更早的轮次不计入、也不显示 · '
        f'点图页内预览，←/→ 翻图'
        f'{_input_note()}</span>'
        '</header><main id="grid">'
    )

    lightbox = (
        '</main>'
        '<div id="lb" hidden>'
        '<div class="lb-bar">'
        '<span class="name" id="lb-name"></span>'
        '<span class="rel" id="lb-rel"></span>'
        '<span class="sp"></span>'
        '<span class="lb-pos" id="lb-pos"></span>'
        '<span class="lb-zoom" id="lb-zoom">适应窗口</span>'
        '<a id="lb-raw" href="#" target="_blank" rel="noopener">在新标签打开原图</a>'
        '<button type="button" id="lb-copy">复制路径</button>'
        '<button type="button" id="lb-close">关闭 (Esc)</button>'
        '</div>'
        '<div class="lb-stage" id="lb-stage">'
        '<button type="button" class="lb-nav" id="lb-prev" aria-label="上一张">‹</button>'
        '<img id="lb-img" alt="">'
        '<button type="button" class="lb-nav" id="lb-next" aria-label="下一张">›</button>'
        '</div></div>'
        '<script>'
        'const q=document.getElementById("q"),grid=document.getElementById("grid"),'
        'hint=document.getElementById("hint"),'
        'cards=[...grid.querySelectorAll(".card")],tabs=[...document.querySelectorAll(".tab")],'
        'lb=document.getElementById("lb"),lbImg=document.getElementById("lb-img"),'
        'lbName=document.getElementById("lb-name"),lbRel=document.getElementById("lb-rel"),'
        'lbRaw=document.getElementById("lb-raw"),lbCopy=document.getElementById("lb-copy"),'
        'lbPos=document.getElementById("lb-pos"),'
        'lbZoom=document.getElementById("lb-zoom"),'
        'stage=document.getElementById("lb-stage");'
        'let cat="all",visible=[],at=0,zf=1,zfTarget=1,rafId=0,anchor=null,fitW=0;'
        'function maxZ(){const nw=lbImg.naturalWidth||0;'
        'return Math.max(8,fitW>0&&nw>0?nw/fitW+1:8);}'
        'function zoomLabel(){'
        'const nw=lbImg.naturalWidth||0;'
        'if(zf<=1.001){lbZoom.textContent="适应窗口";return;}'
        'const pct=(nw>0&&fitW>0)?Math.round(zf*fitW/nw*100):Math.round(zf*100);'
        'lbZoom.textContent=pct+"%";}'
        'function paint(){'
        'const rect=stage.getBoundingClientRect();'
        'const ax=anchor?anchor.cx:rect.width/2,ay=anchor?anchor.cy:rect.height/2;'
        'const oldW=lbImg.clientWidth||1,oldH=lbImg.clientHeight||1;'
        'const rx=(stage.scrollLeft+ax)/oldW,ry=(stage.scrollTop+ay)/oldH;'
        'if(zf<=1.001){lbImg.style.width="";lbImg.classList.remove("zoom");}'
        'else{lbImg.style.maxWidth="none";lbImg.classList.add("zoom");'
        'const base=fitW||lbImg.naturalWidth||oldW;'
        'lbImg.style.width=(base*zf).toFixed(1)+"px";}'
        'zoomLabel();'
        'if(zf>1.001){stage.scrollLeft=rx*lbImg.clientWidth-ax;'
        'stage.scrollTop=ry*lbImg.clientHeight-ay;}}'
        'function captureFit(){'
        'if(lbImg.style.width)return;'
        'fitW=lbImg.clientWidth;'
        'if(zf>1.001){paint();}}'
        'function tick(){'
        'if(Math.abs(zfTarget-zf)<0.002){zf=zfTarget;paint();rafId=0;return;}'
        'zf+=(zfTarget-zf)*0.22;'
        'paint();'
        'rafId=requestAnimationFrame(tick);}'
        'function zoomTo(f,ev){'
        'zfTarget=Math.max(1,Math.min(maxZ(),f));'
        'if(ev){const r=stage.getBoundingClientRect();'
        'anchor={cx:ev.clientX-r.left,cy:ev.clientY-r.top};}'
        'else if(!anchor){anchor=null;}'
        'if(!rafId)rafId=requestAnimationFrame(tick);}'
        'function oneToOne(){const nw=lbImg.naturalWidth||0;'
        'return (nw>0&&fitW>0)?nw/fitW:2;}'
        'function resetZoom(){'
        'if(rafId){cancelAnimationFrame(rafId);rafId=0;}'
        'zf=1;zfTarget=1;anchor=null;fitW=0;'
        'lbImg.style.width="";lbImg.style.maxWidth="";lbImg.classList.remove("zoom");'
        'zoomLabel();}'
        'function refresh(){visible=cards.filter(c=>c.style.display!=="none");}'
        'function apply(){const s=q.value.trim().toLowerCase();let total=0,shown=0;'
        'for(const c of cards){const okCat=(cat==="all"||c.dataset.cat===cat);'
        'if(okCat)total++;const hit=okCat&&(!s||c.textContent.toLowerCase().includes(s));'
        'c.style.display=hit?"":"none";if(hit)shown++;}'
        'refresh();'
        'hint.textContent=`显示 ${shown} / ${total} 张`+(s?`（筛选「${q.value.trim()}」）`:"")'
        '+` · 点图页内预览，←/→ 翻图`;'
        'for(const t of tabs)t.classList.toggle("on",t.dataset.cat===cat);'
        'if(!lb.hidden)show(at);}'
        'function show(i){if(!visible.length){close_();return;}'
        'at=(i%visible.length+visible.length)%visible.length;'
        'const c=visible[at],img=c.querySelector("img");'
        'resetZoom();'
        'lbImg.onload=()=>{captureFit();};'
        'lbImg.src=img.dataset.full;'
        'lbName.textContent=img.dataset.name;'
        'lbRel.textContent=img.dataset.rel;'
        'lbRaw.href=img.dataset.full;'
        'lbCopy.dataset.copy=img.dataset.file;'
        'lbPos.textContent=(at+1)+" / "+visible.length;'
        'lb.hidden=false;document.body.classList.add("locked");}'
        'function close_(){resetZoom();lb.hidden=true;lbImg.removeAttribute("src");'
        'document.body.classList.remove("locked");}'
        'for(const t of tabs)t.addEventListener("click",()=>{cat=t.dataset.cat;apply();});'
        'q.addEventListener("input",apply);'
        'grid.addEventListener("click",e=>{'
        'if(e.target.closest("button[data-copy]")){'
        'const b=e.target.closest("button[data-copy]");'
        'navigator.clipboard.writeText(b.dataset.copy).then(()=>{'
        'b.textContent="已复制";setTimeout(()=>b.textContent="复制路径",1200);})'
        '.catch(()=>window.prompt("复制这条路径：",b.dataset.copy));return;}'
        'const c=e.target.closest(".card");if(!c)return;refresh();show(visible.indexOf(c));});'
        'document.getElementById("lb-close").addEventListener("click",close_);'
        'document.getElementById("lb-prev").addEventListener("click",()=>show(at-1));'
        'document.getElementById("lb-next").addEventListener("click",()=>show(at+1));'
        'let dragging=false,dragMoved=false,dragX=0,dragY=0,dragSL=0,dragST=0;'
        'lbImg.addEventListener("dragstart",e=>e.preventDefault());'
        'lbImg.addEventListener("pointerdown",e=>{'
        'if(lb.hidden||zf<=1.001)return;'
        'dragging=true;dragMoved=false;'
        'dragX=e.clientX;dragY=e.clientY;'
        'dragSL=stage.scrollLeft;dragST=stage.scrollTop;'
        'try{lbImg.setPointerCapture(e.pointerId);}catch(_){}'
        'lbImg.classList.add("dragging");e.preventDefault();});'
        'lbImg.addEventListener("pointermove",e=>{'
        'if(!dragging)return;'
        'const dx=e.clientX-dragX,dy=e.clientY-dragY;'
        'if(Math.abs(dx)>3||Math.abs(dy)>3)dragMoved=true;'
        'stage.scrollLeft=dragSL-dx;stage.scrollTop=dragST-dy;});'
        'function endDrag(e){if(!dragging)return;dragging=false;'
        'lbImg.classList.remove("dragging");'
        'try{lbImg.releasePointerCapture(e.pointerId);}catch(_){}'
        'setTimeout(()=>{dragMoved=false;},0);}'
        'lbImg.addEventListener("pointerup",endDrag);'
        'lbImg.addEventListener("pointercancel",endDrag);'
        'lbImg.addEventListener("click",()=>{if(dragMoved)return;'
        'anchor=null;zoomTo(zfTarget>1.001?1:oneToOne(),null);});'
        'stage.addEventListener("wheel",e=>{if(lb.hidden)return;'
        'e.preventDefault();'
        'let dy=e.deltaY;'
        'if(e.deltaMode===1)dy*=16;else if(e.deltaMode===2)dy*=100;'
        'zoomTo(zfTarget*Math.exp(-dy*0.0006),e);},{passive:false});'
        'lbCopy.addEventListener("click",()=>{'
        'navigator.clipboard.writeText(lbCopy.dataset.copy||"").then(()=>{'
        'lbCopy.textContent="已复制";setTimeout(()=>lbCopy.textContent="复制路径",1200);})'
        '.catch(()=>window.prompt("复制这条路径：",lbCopy.dataset.copy||""));});'
        # 点"空白处"关闭：图周围那片空白命中的是 .lb-stage（撑满剩余空间的容器），
        # 不只是 #lb 本体——早先只判 e.target===lb，所以在图旁点空白关不掉。
        # 点图片本身仍是缩放、点两侧箭头仍是翻图（它们的 target 是 img/button，不会走到这里）。
        'lb.addEventListener("click",e=>{const t=e.target;'
        'if(t===lb||(t&&t.id==="lb-stage"))close_();});'
        'document.addEventListener("keydown",e=>{if(lb.hidden)return;'
        'if(e.key==="Escape")close_();'
        'else if(e.key==="ArrowRight"){e.preventDefault();show(at+1);}'
        'else if(e.key==="ArrowLeft"){e.preventDefault();show(at-1);}});'
        'apply();'
        '</script></body></html>'
    )

    return head + "".join(cards) + lightbox


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MaskEdit/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- helpers ----
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The UI may run inside a sandboxed iframe (Origin: null) or from a
        # file:// page, so allow any origin. This server only ever listens on
        # 127.0.0.1 and holds no server-side secrets, so this is safe here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


    def _gallery_image(self, wanted: str):
        """服务白名单根目录内的图片文件（防目录穿越：解析后必须落在根内）。"""
        import os

        if (wanted or "").startswith("att:"):
            oid = wanted[4:]
            # 注意：user_inputs() 只提供 paths/excluded/unreachable，从不提供 atts。
            # 原先直接写 user_inputs()["atts"] 会 **KeyError**（此分支一旦被走到就崩），
            # 改为 .get 后优雅地返回 404。
            meta = next((a for a in (user_inputs().get("atts") or []) if str(a.get("id")) == oid), None)
            if meta is None:
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            p = os.path.join(ATTACHMENTS_DIR, "v1", "objects", oid[:2], oid[2:])
            if not os.path.isfile(p):
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            ctype = meta.get("mediaType") or "image/png"
            try:
                with open(p, "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError as e:
                return self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")

        p = os.path.abspath(wanted or "")
        allowed = False
        for root in GALLERY_ROOTS:
            root_abs = os.path.abspath(root)
            try:
                if os.path.commonpath([p, root_abs]) == root_abs:
                    allowed = True
                    break
            except ValueError:
                continue          # 不同盘符等
        ext = os.path.splitext(p)[1].lower()
        if not allowed or ext not in GALLERY_EXTS or not os.path.isfile(p):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
        ctype = ("image/png" if ext == ".png"
                 else "image/jpeg" if ext in (".jpg", ".jpeg")
                 else "image/webp" if ext == ".webp" else "image/gif")
        return self._send(200, data, ctype)

    def do_OPTIONS(self):
        self._send(204, b"")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _read_json(self):
        try:
            return json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---- routes ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
            origin = f"http://{host}"
            page = PAGE.replace("__APP_ORIGIN__", origin)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"")
        elif path == "/gallery":
            self._send(200, gallery_html().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/gallery/img":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._gallery_image((qs.get("p") or [""])[0])
        elif path == "/board":
            return self._board_page()
        elif path == "/board/img":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._board_image((qs.get("p") or [""])[0])
        elif path == "/api/board":
            import board_app
            return self._json(board_app.load_board())
        elif path == "/api/update":
            import board_app
            return self._json(board_app.update_status())
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/ping":
            return self.api_ping()
        if path == "/api/models":
            return self.api_models()
        if path == "/api/edit":
            return self.api_edit()
        if path == "/api/board/save":
            return self.api_board_save()
        if path == "/api/board/upload":
            return self.api_board_upload()
        if path == "/api/board/generate":
            return self.api_board_generate()
        if path == "/api/board/extract":
            return self.api_board_extract()
        if path == "/api/update":
            return self.api_update()
        self._json({"ok": False, "message": "unknown endpoint"}, 404)

    def _board_page(self):
        p = os.path.join(os.path.dirname(__file__), "board.html")
        try:
            with open(p, encoding="utf-8") as f:
                return self._send(200, f.read().encode("utf-8"), "text/html; charset=utf-8")
        except OSError as e:
            return self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")

    def _board_image(self, wanted: str):
        import board_app
        p = board_app.allowed_path(wanted)
        if p is None:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ext = p.suffix.lower()
        ctype = ("image/png" if ext == ".png"
                 else "image/jpeg" if ext in (".jpg", ".jpeg")
                 else "image/webp" if ext == ".webp" else "image/gif")
        try:
            return self._send(200, p.read_bytes(), ctype)
        except OSError as e:
            return self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")

    def api_board_save(self):
        import board_app
        j = self._read_json()
        return self._json(board_app.save_board(j))

    def api_board_upload(self):
        import board_app
        j = self._read_json()
        try:
            info = board_app.save_upload_b64(j.get("name") or "drop.png", j.get("data_b64") or "")
        except Exception as e:
            return self._json({"ok": False, "message": str(e)}, 400)
        info["ok"] = True
        return self._json(info)

    def api_board_generate(self):
        import board_app
        j = self._read_json()
        board = board_app.load_board()
        dry = bool(j.get("dry_run"))
        no_cache = bool(j.get("no_cache")) or not dry
        return self._json(board_app.run_edit(board, dry_run=dry, no_cache=no_cache))

    def api_board_extract(self):
        import board_app
        j = self._read_json()
        board = board_app.load_board()
        node_id = str(j.get("node_id") or "")
        return self._json(board_app.run_extract(board, node_id))

    def api_update(self):
        import board_app
        return self._json(board_app.apply_update())

    # ---- api: ping ----
    def api_ping(self):
        j = self._read_json()
        base = (j.get("base_url") or "").rstrip("/")
        key = j.get("api_key") or ""
        # 页面字段可能为空（凭据改由服务端环境变量提供）→ 回退到引擎默认值，
        # 与 api_edit 同一套口径；两边都空才算真的没配。
        eng = engine_of(j.get("engine") or "gpt")
        if not base:
            base = eng["base"]
        if not key:
            key = eng["key"]
        if not base or not key:
            return self._json({"ok": False, "message": (
                "缺少 Base URL 或 API Key。\n"
                "服务端来源是环境变量：RELAY_API_KEY（必填）、RELAY_BASE_URL（选填）。\n"
                "设好后需**重启本地服务**才会读到；也可以在页面上手动填。")})

        req = urllib.request.Request(base + "/models")
        req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=ssl.create_default_context()) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            n = len(data.get("data", []))
            return self._json({"ok": True,
                               "message": f"连接成功 [OK]\n{base}\n可用模型 {n} 个"})
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:300]
            return self._json({"ok": False,
                               "message": f"HTTP {e.code}\n{txt}\n\n检查 Base URL 结尾是否有 /v1"})
        except Exception as e:
            return self._json({"ok": False,
                               "message": f"{type(e).__name__}: {e}\n\n"
                                          f"若是超时或 SSL 错误，说明本地访问不了该域名"})

    # ---- api: models ----
    def api_models(self):
        j = self._read_json()
        base = (j.get("base_url") or "").rstrip("/")
        key = j.get("api_key") or ""
        # 同 api_ping：空值回退到引擎默认（来自环境变量），页面不必手填
        eng = engine_of(j.get("engine") or "gpt")
        if not base:
            base = eng["base"]
        if not key:
            key = eng["key"]
        if not base or not key:
            return self._json({"ok": False, "message": (
                "缺少 Base URL 或 API Key。\n"
                "服务端来源是环境变量：RELAY_API_KEY（必填）、RELAY_BASE_URL（选填）。\n"
                "设好后需**重启本地服务**才会读到；也可以在页面上手动填。")})
        req = urllib.request.Request(base + "/models")
        req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=45,
                                        context=ssl.create_default_context()) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            models = sorted(m.get("id", "") for m in data.get("data", []))
            img = [m for m in models if any(t in m.lower() for t in
                                            ("image", "dall", "gpt-image", "flux", "sd", "seedream"))]
            return self._json({"ok": True, "models": models, "image_models": img})
        except urllib.error.HTTPError as e:
            return self._json({"ok": False,
                               "message": f"HTTP {e.code}\n" + e.read().decode("utf-8", "replace")[:300]})
        except Exception as e:
            return self._json({"ok": False, "message": f"{type(e).__name__}: {e}"})

    # ---- api: edit ----
    def api_edit(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return self._json({"ok": False, "message": "expected multipart/form-data"})
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        body = self._read_body()
        sys.stderr.write(f"  [/api/edit] received {len(body):,} bytes\n")
        fields, files = parse_multipart(body, boundary)
        sys.stderr.write(f"  [/api/edit] parts: {sorted(files.keys())} "
                         f"sizes: {[ (k, len(v[1])) for k, v in files.items() ]}\n")

        base = (fields.get("base_url") or "").rstrip("/")
        key = fields.get("api_key") or ""
        model = fields.get("model") or "gpt-image-1"
        prompt = fields.get("prompt") or ""
        size = fields.get("size") or "1024x1536"
        quality = fields.get("quality") or "high"
        pad_mode = fields.get("pad_mode") or "pad"
        mask_mode = fields.get("mask_mode") or "std"
        scope = (fields.get("scope") or "mask").strip()
        engine_name = (fields.get("engine") or "gpt").strip().lower()
        resolution = (fields.get("resolution") or "1k").strip().lower()
        ope = (fields.get("operation") or "").strip().lower()   # "", "t2i", "i2i"
        eng = engine_of(engine_name)

        # ---- reference images (role-tagged) ---------------------------------
        refs = take_reference_images(fields, files)
        if refs:
            sys.stderr.write(
                "  [/api/edit] refs: " + ", ".join(
                    f"{r[0]}:{r[1]}({len(r[2]) // 1024}KB)" for r in refs) + "\n")
            if not eng.get("multi_image"):
                names = "、".join(f"{i+1}){r[1]}" for i, r in enumerate(refs))
                return self._json({
                    "ok": False,
                    "message": (f"当前引擎「{eng['label']}」不接受多张输入图片"
                                f"（2026-10-06 实测：image + image[1] 返回 HTTP 400）。\n"
                                f"你已添加 {len(refs)} 张参考图：{names}\n\n"
                                f"请二选一：\n"
                                f"  · 改用「GPT Image 2」引擎（支持多张参考图）；或\n"
                                f"  · 清空参考图列表。")})


        # engine decides credentials unless the caller overrode them explicitly
        if not base:
            base = eng["base"]
        if not key:
            key = eng["key"]
        if not model:
            model = eng["t2i_model"]

        if not base or not key:
            return self._json({"ok": False, "message": "缺少 Base URL 或 API Key"})

        # ---- work out what the user asked for --------------------------------
        has_image = "image_file" in files
        t2i = (ope == "t2i") or (not has_image and ope != "i2i")
        if ope == "i2i" and not has_image:
            return self._json({"ok": False, "message": "图生图需要先选择图片"})

        if t2i:
            src = None
            whole = True
            tw, th = (int(v) for v in size.lower().split("x"))
            model = model or eng["t2i_model"]
            vis_png = pad_paint = alpha_png = image_png = None
            prompt_used = prompt
        else:
            try:
                tw, th = (int(v) for v in size.lower().split("x"))
            except Exception:
                return self._json({"ok": False, "message": f"尺寸无法解析: {size}"})
            whole = (scope == "whole")
            if not whole and not eng["mask"]:
                return self._json({
                    "ok": False,
                    "message": ("Grok 不支持遮罩局部修改（它的 /images/edits 没有 mask 机制，"
                                "且不接受第二张参考图）。请把「修改范围」改成"
                                "「整张图（无需涂遮罩）」，或改用 GPT Image 2 引擎。")})
            if not whole and "mask_file" not in files:
                return self._json({"ok": False, "message": "缺少遮罩"})

            import io
            try:
                src = Image.open(io.BytesIO(files["image_file"][1]))
                src.load()
            except Exception as e:
                return self._json({"ok": False, "message": f"图片无法读取: {e}"})

            canvas_mask = None
            if not whole:
                canvas_mask = Image.open(io.BytesIO(files["mask_file"][1])).convert("RGBA")

            # paint the edit region flat red and send it as reference image #2
            if whole:
                vis_png, pad_paint, schema_prefix = None, None, ""
            else:
                vis_png, pad_paint = make_visual_mask(
                    src, canvas_mask, tw, th, pad_mode, invert=(mask_mode == "inv"))
                if not pad_paint.any():
                    return self._json({"ok": False,
                                       "message": "遮罩为空：请涂出要修改的区域（或切换遮罩语义）"})
                schema_prefix = SCHEMA_PROMPT

            # ---- authoritative slot map, built from what is REALLY sent ------
            # Send order is fixed below: image, image[1]=mask ref (when mask mode),
            # then image[2..] = the user's role-tagged references, in added order.
            mask_slot_sent = bool(eng["mask"] and not whole)
            slots = [(1, SLOT_CONTENT_CN[0], SLOT_CONTENT_CN[1],
                      SLOT_CONTENT_EN[0], SLOT_CONTENT_EN[1])]
            if mask_slot_sent:
                slots.append((2, SLOT_MASK_CN[0], SLOT_MASK_CN[1],
                              SLOT_MASK_EN[0], SLOT_MASK_EN[1]))
            ref_first = 2 + (1 if mask_slot_sent else 0)

            used = 1 + (1 if mask_slot_sent else 0)
            if used + len(refs) > MAX_IMAGE_SLOTS:
                over = used + len(refs) - MAX_IMAGE_SLOTS
                compose_parts = ["内容图 1 张"]
                if mask_slot_sent:
                    compose_parts.append("红色标记图 1 张")
                compose_parts.append(f"参考图 {len(refs)} 张")
                fix_hint = "请减掉 %d 张参考图" % over
                if mask_slot_sent:
                    fix_hint += "，或把「修改范围」改成「整张图」以腾出红色标记图占的位置。"
                else:
                    fix_hint += "。"
                over_msg = (
                    f"图片数量超出上限：本请求要发 {used + len(refs)} 张，"
                    f"但接口只接受 {MAX_IMAGE_SLOTS} 张"
                    f"（image / image[1] / image[2] / image[3]）。\n"
                    f"当前构成：{'＋'.join(compose_parts)}。\n\n" + fix_hint
                )
                return self._json({"ok": False, "message": over_msg})

            for i, (role, rname, _rbytes) in enumerate(refs):
                lcn, icn = REF_ROLES[role]["cn"]
                len_, ien = REF_ROLES[role]["en"]
                slots.append((ref_first + i, lcn, icn, len_, ien))

            # Order matters: SCHEMA_PROMPT must stay the FIRST thing the model
            # reads — its "red = the only editable area" contract was verified
            # 100% on the protected region (see make_visual_mask). The slot map is
            # new information, so it goes after it and before the user's own text.
            header = build_slot_header(slots, _has_cjk(prompt))
            prompt_used = schema_prefix + header + prompt
            sys.stderr.write("  [/api/edit] slots: " + "; ".join(
                ("image=" if s[0] == 1 else f"image[{s[0] - 1}]=") + s[1] for s in slots) + "\n")
            slot_map = [{"index": s[0], "label": s[1]} for s in slots]
            alpha_png = build_mask_alpha(pad_paint) if pad_paint is not None else None

            img_fit = normalise_to_size(src, tw, th, pad_mode)
            ibuf = io.BytesIO()
            img_fit.save(ibuf, "PNG", optimize=True)
            image_png = ibuf.getvalue()

        # ---- 1. TEXT-TO-IMAGE -------------------------------------------------
        if t2i:
            body_obj = {"model": model or eng["t2i_model"], "prompt": prompt, "n": 1}
            if eng["uses_quality"]:
                body_obj["quality"] = quality
            if eng["uses_resolution"]:
                body_obj["resolution"] = resolution
                body_obj["size"] = size
                # Grok's default reply is an unreachable imgen.x.ai url, so ask
                # for inline data. This is the ONLY way Grok text-to-image works
                # from this machine (verified).
                body_obj["response_format"] = "b64_json"
            else:
                body_obj["size"] = size
            url = base + "/images/generations"
            payload = json.dumps(body_obj).encode("utf-8")
            status, raw = relay_post(url, payload, "application/json", key, timeout=420)
            if status == -1:
                return self._json({"ok": False, "message":
                                   f"请求未发出：{raw.decode('utf-8','replace')}\nURL: {url}"})
            text = raw.decode("utf-8", "replace")
            if status != 200:
                return self._json({"ok": False, "message":
                                   f"HTTP {status}\n{text[:800]}\n\nURL: {url}"})
            try:
                data = json.loads(text)
            except Exception:
                return self._json({"ok": False, "message": f"返回不是 JSON:\n{text[:400]}"})
            items = data.get("data") or []
            if not items:
                return self._json({"ok": False, "message": f"返回里没有图片:\n{text[:400]}"})
            b64, err = engine_fetch_image(items[0])
            if err:
                return self._json({"ok": False, "message": err})
            return self._json({"ok": True, "image_b64": force_size_b64(b64, size)})

        # ---- 2. IMAGE-TO-IMAGE ------------------------------------------------
        bnd = ("----MaskEdit" + uuid.uuid4().hex).encode()
        out_files = {"image": ("image.png", image_png, "image/png")}
        if eng["mask"] and not whole:
            out_files["image[1]"] = ("mask_ref.png", vis_png, "image/png")
            # undocumented; harmless on relays that ignore it, useful on those
            # that implement the standard OpenAI convention
            out_files["mask"] = ("mask.png", alpha_png, "image/png")

        # user references land after the mask slot so numbering stays contiguous
        # and matches the header injected into the prompt above.
        for i, (role, rname, rbytes) in enumerate(refs):
            try:
                rimg = Image.open(io.BytesIO(rbytes))
                rimg.load()
            except Exception as e:
                return self._json({"ok": False,
                                   "message": f"参考图无法读取（{rname}）: {e}"})
            rfit = normalise_to_size(rimg, tw, th, pad_mode)
            rbuf = io.BytesIO()
            rfit.save(rbuf, "PNG", optimize=True)
            out_files[f"image[{ref_first + i - 1}]"] = (
                f"ref{i + 1}_{role}.png", rbuf.getvalue(), "image/png")

        mp_fields = {"model": model or eng["i2i_model"], "prompt": prompt_used, "n": "1"}
        if eng["uses_quality"]:
            mp_fields["quality"] = quality
        if eng["uses_resolution"]:
            mp_fields["resolution"] = resolution
        mp_fields["size"] = size
        if not eng["edits_b64"]:
            # harmless probe: the relay ignores it here, but asking costs nothing
            mp_fields["response_format"] = "b64_json"
        mp_body = build_multipart(mp_fields, out_files, bnd)
        url = base + "/images/edits"
        status, raw = relay_post(url, mp_body,
                                 f"multipart/form-data; boundary={bnd.decode()}",
                                 key, timeout=420)

        if status == -1:
            return self._json({"ok": False,
                               "message": f"请求未发出：{raw.decode('utf-8','replace')}\n\n"
                                          f"URL: {url}"})
        text = raw.decode("utf-8", "replace")
        if status != 200:
            return self._json({"ok": False, "message": f"HTTP {status}\n{text[:800]}\n\nURL: {url}"})

        try:
            data = json.loads(text)
        except Exception:
            return self._json({"ok": False, "message": f"返回不是 JSON:\n{text[:400]}"})

        items = data.get("data") or []
        if not items:
            return self._json({"ok": False, "message": f"返回里没有图片:\n{text[:400]}"})
        b64, err = engine_fetch_image(items[0])
        if err:
            return self._json({"ok": False, "message": err})
        return self._json({"ok": True, "image_b64": force_size_b64(b64, size),
                           "slot_map": slot_map,
                           "ref_count": len(refs),
                           "parts_sent": sorted(out_files.keys())})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--log", default="", help="also append request log to this file")
    a = ap.parse_args()

    if a.log:
        # mirror stderr into a file so a failed run can be inspected afterwards
        try:
            fh = open(a.log, "a", encoding="utf-8", buffering=1)
            real = sys.stderr

            class Tee:
                def write(self, s):
                    real.write(s)
                    try:
                        fh.write(s)
                    except Exception:
                        pass
                    return len(s)

                def flush(self):
                    real.flush()

            sys.stderr = Tee()
            print(f"[{__import__('time').strftime('%H:%M:%S')}] --- server started on port {a.port} ---")
        except Exception as e:
            print(f"could not open log file {a.log}: {e}")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("=" * 62)
    print("  本地遮罩改图工具已启动")
    print(f"  打开浏览器访问：  http://{a.host}:{a.port}")
    print("  停止：在本窗口按 Ctrl+C")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
