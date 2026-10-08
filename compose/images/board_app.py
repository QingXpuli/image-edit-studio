"""Infinite board helpers for /board. Generation delegates to zimage.py / run_round."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BOARD_DIR = ROOT / ".zimage" / "board"
BOARD_JSON = BOARD_DIR / "board.json"
UPLOAD_DIR = BOARD_DIR / "uploads"
OUT_DIR = ROOT / "产图"
ZIMAGE = ROOT / "zcode-image-edit" / "bin" / "zimage.py"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

ALLOWED_ROOTS = [
    ROOT / "产图",
    ROOT / "图生图用",
    ROOT / "t1",
    ROOT / "style-distill",
    ROOT / "compose",
    ROOT / ".zimage",
]


def _ensure() -> None:
    BOARD_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val:
        return val
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v, _ = winreg.QueryValueEx(k, name)
                if isinstance(v, str) and v:
                    return v
        except Exception:
            pass
    return default


def grok_transport() -> tuple[str, str, str]:
    base = _env("RELAY_GROK_BASE_URL").rstrip("/")
    key = _env("RELAY_GROK_API_KEY")
    model = _env("RELAY_GROK_I2I_MODEL", "grok-imagine-edit") or "grok-imagine-edit"
    return base, key, model


def empty_board() -> dict:
    return {
        "cam": {"x": 0, "y": 0, "z": 1},
        "objects": [],
        "edges": [],
        "prompt": "",
        "size": "1024x1536",
        "pad": "pad",
        "input_fidelity": "",
        "identityId": "",
        "refId": "",
        "genId": "",
    }


def load_board() -> dict:
    _ensure()
    if not BOARD_JSON.is_file():
        return empty_board()
    try:
        data = json.loads(BOARD_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_board()
    if not isinstance(data, dict):
        return empty_board()
    base = empty_board()
    base.update({k: data.get(k, base[k]) for k in base})
    if not isinstance(base["objects"], list):
        base["objects"] = []
    if not isinstance(base.get("edges"), list):
        base["edges"] = []
    return base


def save_board(data: dict) -> dict:
    _ensure()
    cur = load_board()
    for k in (
        "cam", "objects", "edges", "prompt", "size", "pad",
        "input_fidelity", "identityId", "refId", "genId",
    ):
        if k in data:
            cur[k] = data[k]
    tmp = BOARD_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(BOARD_JSON)
    return cur


def allowed_path(path: str) -> Path | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    if p.suffix.lower() not in IMG_EXT or not p.is_file():
        return None
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    return None


def save_upload(name: str, raw: bytes) -> dict:
    _ensure()
    ext = Path(name or "drop.png").suffix.lower()
    if ext not in IMG_EXT:
        ext = ".png"
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    dest.write_bytes(raw)
    with Image.open(dest) as im:
        w, h = im.size
    return {"path": str(dest), "w": w, "h": h, "name": dest.name}


def save_upload_b64(name: str, data_b64: str) -> dict:
    import base64

    raw = data_b64
    if "," in raw:
        raw = raw.split(",", 1)[1]
    return save_upload(name, base64.b64decode(raw))


def obj_by_id(board: dict, oid: str) -> dict | None:
    for o in board.get("objects") or []:
        if str(o.get("id")) == str(oid):
            return o
    return None


def incoming(board: dict, oid: str) -> list[dict]:
    out = []
    seen: set[str] = set()
    for e in board.get("edges") or []:
        if str(e.get("to")) != str(oid):
            continue
        src = obj_by_id(board, str(e.get("from") or ""))
        if not src:
            continue
        sid = str(src.get("id") or "")
        if sid in seen:
            continue
        seen.add(sid)
        out.append(src)
    return out


_CHROME_HEADS = (
    "连线开", "连线关", "已保存", "复位", "节点 ", "遮罩", "画廊",
    "未命名画布", "生成在节点上点", "补充提示词",
)


def strip_chrome(text: str) -> str:
    """Drop dock/UI chrome that was accidentally saved into the prompt."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    while lines:
        s = lines[0].strip()
        if not s:
            lines.pop(0)
            continue
        if s in ("−", "-", "+", "补边", "裁切") or re.fullmatch(r"\d+%", s) or re.fullmatch(r"\d+x\d+", s):
            lines.pop(0)
            continue
        if any(s.startswith(h) for h in _CHROME_HEADS):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def active_gen(board: dict) -> dict | None:
    gen = obj_by_id(board, board.get("genId") or "")
    if gen is not None and gen.get("type") == "gen":
        return gen
    gens = [o for o in board.get("objects") or [] if o.get("type") == "gen"]
    if len(gens) == 1:
        return gens[0]
    return None


def resolve_roles(board: dict) -> tuple[dict | None, dict | None, str, dict | None]:
    """Identity / ref / prompt from graph edges, then fall back to side fields."""
    gen = active_gen(board)
    images: list[dict] = []
    texts: list[str] = []
    if gen is not None:
        for src in incoming(board, str(gen.get("id"))):
            if src.get("type") == "image":
                images.append(src)
            elif src.get("type") in ("text", "extract"):
                t = strip_chrome(src.get("text") or "")
                if t:
                    texts.append(t)
    wired_ids = {str(o.get("id")) for o in images}
    pick_ident = str(board.get("identityId") or "")
    pick_ref = str(board.get("refId") or "")
    ident = next((o for o in images if str(o.get("id")) == pick_ident), None)
    if ident is None:
        ident = images[0] if images else obj_by_id(board, pick_ident)
    if ident is not None and ident.get("type") != "image":
        ident = None
    ident_id = str(ident.get("id")) if ident else ""
    ref = next((o for o in images if str(o.get("id")) == pick_ref and str(o.get("id")) != ident_id), None)
    if ref is None and images:
        ref = next((o for o in images if str(o.get("id")) != ident_id), None)
    elif not images:
        ref = obj_by_id(board, pick_ref)
    if ref is not None and (ref.get("type") != "image" or (ident and ref.get("id") == ident.get("id"))):
        ref = None
    dock = strip_chrome(board.get("prompt") or "")
    node_prompt = strip_chrome((gen.get("prompt") if gen else None) or "")
    if node_prompt:
        parts = [node_prompt, *texts]
    else:
        parts = [p for p in (dock, *texts) if p]
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)
    prompt = "\n".join(unique)
    return ident, ref, prompt, gen


def rect_to_pixels(identity: dict, board: dict) -> str | None:
    rects = [
        o
        for o in board.get("objects") or []
        if o.get("type") == "rect" and str(o.get("parent")) == str(identity.get("id"))
    ]
    if not rects:
        return None
    r = rects[0]
    path = allowed_path(identity.get("path") or "")
    if path is None:
        return None
    with Image.open(path) as im:
        iw, ih = im.size
    x0 = int(max(0, float(r.get("nx", 0)) * iw))
    y0 = int(max(0, float(r.get("ny", 0)) * ih))
    x1 = int(min(iw, (float(r.get("nx", 0)) + float(r.get("nw", 0))) * iw))
    y1 = int(min(ih, (float(r.get("ny", 0)) + float(r.get("nh", 0))) * ih))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return f"{x0},{y0},{x1},{y1}"


def build_edit_cmd(board: dict, *, dry_run: bool, job_id: str, out: Path, no_cache: bool = False) -> tuple[list[str] | None, str]:
    ident, ref, prompt, gen = resolve_roles(board)
    if not ident or ident.get("type") != "image":
        return None, "把一张图片连到生成节点（或选中后设为图一）。"
    src = allowed_path(ident.get("path") or "")
    if src is None:
        return None, "图一路径不在工作区内，或不存在。"
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "把文本节点连到生成节点，或在底部填写提示词。"
    size = (gen.get("size") if gen and gen.get("size") else None) or board.get("size") or "1024x1536"
    if not re.fullmatch(r"\d+x\d+", str(size)):
        return None, "尺寸必须是 1024x1536 这种形式。"
    pad_src = (gen.get("pad") if gen and gen.get("pad") else None) or board.get("pad")
    pad = pad_src if pad_src in ("pad", "crop") else "pad"
    if gen is not None and "input_fidelity" in gen:
        fidelity = gen.get("input_fidelity") or ""
    else:
        fidelity = board.get("input_fidelity") or ""
    channel = ((gen.get("channel") if gen else None) or board.get("channel") or "gpt").strip().lower()
    if channel not in ("gpt", "grok"):
        channel = "gpt"
    if channel == "grok" and ref and ref.get("type") == "image":
        return None, "Grok 通道只接受一张图。请只保留图一，或改回 GPT。"
    grok_model = "grok-imagine-edit"
    if channel == "grok":
        grok_base, grok_key, grok_model = grok_transport()
        if not grok_base or not grok_key:
            return None, "Grok 未配置 RELAY_GROK_BASE_URL / RELAY_GROK_API_KEY。请先设用户环境变量后重启画布服务。"
        size = "1024x1024"
    prompt_path = BOARD_DIR / "last-prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    cmd = [
        sys.executable,
        str(ZIMAGE),
        "edit",
        "--image",
        str(src),
        "--prompt-file",
        str(prompt_path),
        "--out",
        str(out),
        "--size",
        str(size),
        "--pad",
        pad,
        "--encode",
        "jpg",
        "--job-id",
        job_id,
        "--force",
    ]
    if channel == "grok":
        cmd += ["--model", grok_model]
    if ref and ref.get("type") == "image":
        rp = allowed_path(ref.get("path") or "")
        if rp is None:
            return None, "图二路径不在工作区内。"
        cmd += ["--ref", str(rp)]
    rect = rect_to_pixels(ident, board)
    if rect:
        cmd += ["--rect", rect]
    else:
        cmd.append("--whole")
    if channel != "grok" and fidelity in ("low", "high"):
        cmd += ["--input-fidelity", fidelity]
    if dry_run:
        cmd.append("--dry-run")
    elif no_cache:
        cmd.append("--no-cache")
    return cmd, ""


def run_edit(board: dict, *, dry_run: bool, no_cache: bool = False) -> dict:
    _ensure()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    job_id = f"board-{stamp}"
    out = OUT_DIR / f"board-{stamp}.png"
    cmd, err = build_edit_cmd(board, dry_run=dry_run, job_id=job_id, out=out, no_cache=no_cache)
    if err:
        return {"ok": False, "message": err}
    env = os.environ.copy()
    _, _, _, gen = resolve_roles(board)
    channel = ((gen.get("channel") if gen else None) or board.get("channel") or "gpt").strip().lower()
    if channel == "grok":
        grok_base, grok_key, grok_model = grok_transport()
        env["RELAY_BASE_URL"] = grok_base
        env["RELAY_API_KEY"] = grok_key
        env["RELAY_API_KEY_HD"] = grok_key
        env["RELAY_MODEL"] = grok_model
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
        timeout=420,
    )
    log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "message": log[-4000:] or f"exit {proc.returncode}",
            "job_id": job_id,
            "dry_run": dry_run,
        }
    ident, ref, _, _ = resolve_roles(board)
    ident_name = Path(ident.get("path") or ident.get("name") or "").name if ident else ""
    ref_name = Path(ref.get("path") or ref.get("name") or "").name if ref else ""
    roles_line = "图一 " + (ident_name or "(无)") + " · 图二 " + (ref_name or "(无)")
    result: dict = {
        "ok": True,
        "message": roles_line + "\n" + log[-4000:],
        "job_id": job_id,
        "dry_run": dry_run,
        "fields": "image + image[1]" if "--ref" in cmd else "image",
        "size": board.get("size") or "1024x1536",
        "pad": board.get("pad") or "pad",
        "image1": ident_name,
        "image2": ref_name,
    }
    if dry_run:
        return result
    if not out.is_file():
        return {"ok": False, "message": "命令成功但没有结果文件。\n" + log[-2000:]}
    with Image.open(out) as im:
        w, h = im.size
    result["path"] = str(out)
    result["w"] = w
    result["h"] = h
    return result


EXTRACT_SCRIPT = ROOT / "style-distill" / "_skill" / "style-distill" / "scripts" / "extract_source.py"


_POSE_KEYS = (
    "sitting", "standing", "looking", "holding", "from_side", "from_behind",
    "from_above", "from_below", "arms", "hand", "pose", "kneeling", "lying",
    "walk", "crossed", "leaning", "profile", "three-quarter",
)

_FINGER_NEG = (
    "fused fingers, extra fingers, missing fingers, stiff rigid fingers, "
    "clawed fingers, clenched fists"
)


def _join(items, empty="未识别"):
    return ", ".join(items) if items else empty


def _pose_tags(row: dict) -> str:
    tags = [t for t in (row.get("wd14_tags") or []) if any(k in t.lower() for k in _POSE_KEYS)]
    return _join(tags, "未识别（标签里没有明确动作，需目测）")


def _leak_terms(row: dict) -> str:
    ident = list(row.get("wd14_classes", {}).get("身份向") or [])
    extra = [t for t in (row.get("wd14_tags") or []) if t.lower() in {
        "earrings", "jewelry", "necklace", "choker", "hair_ornament",
        "pointy_ears", "animal_ears", "cat_ears", "fox_ears",
    }]
    seen, out = set(), []
    for t in ident + extra:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return _join(out, "（无身份向标签，仍须目检）")


def compile_extract_text(kind: str, row: dict, name: str) -> str:
    """Compile style-distill Card III-shaped text from extract_source numbers.

    Local only. Does not claim original-prompt recovery. STYLE hex is observation,
    not a colour-transfer target (R1).
    """
    pal = _join(
        [f"{c.get('hex')} ({c.get('占比')})" for c in (row.get("palette") or [])[:6]],
        "未识别",
    )
    ident = _join(row.get("wd14_classes", {}).get("身份向") or [])
    tech = _join(row.get("wd14_classes", {}).get("技法向") or [], "未识别（WD 经常打不出技法）")
    other = _join((row.get("wd14_classes", {}).get("未分类") or [])[:16])
    pose = _pose_tags(row)
    leak = _leak_terms(row)
    size = row.get("size") or [0, 0]
    weak = row.get("edge_weak_pct")
    strong = row.get("edge_strong_pct")
    paper = row.get("paper_white_pct")
    bright = row.get("bright") or "未识别"
    line_hint = (
        "弱边缘高→线或色阶更密；弱边缘低且纸白高→白底会压低 edge，勿当无线。"
        if (weak is not None and paper is not None)
        else "edge 未量。"
    )
    head_pose = (
        "HEAD\n"
        f"定点姿态迁移。参考图 {name}（{size[0]}×{size[1]}）只提供身体动作与构图。\n"
        "图一是身份、服装、配色与画法的唯一来源。不要输出设定稿或四视图。"
    )
    head_style = (
        "HEAD\n"
        f"画法迁移。参考图 {name} 只提供手法观察，不是身份源，也不是配色源。\n"
        "图一每个部位保持自己的色相（R1）。不得声称还原原提示词（R3）。"
    )
    pose_block = (
        "POSE\n"
        f"- 动作/机位标签【实测】：{pose}\n"
        "- 逐项对齐：身体朝向、头部角度、视线、肩线高低、躯干扭转、重心脚、"
        "双臂弯曲、双手相对身体的位置、手的开合。\n"
        "- 手指数量跟图一走。方位以角色自己的身体为参照。"
    )
    style_block = (
        "STYLE（由本地量测编译，槽位未目测处标未识别）\n"
        f"- LINE/EDGE【实测】弱边缘 {weak}% / 强边缘 {strong}%。{line_hint}\n"
        f"- FILL/VALUE【实测】纸白 {paper}%  亮区色 {bright}（白纸图上亮区常是纸色，不是肤色）。\n"
        f"- 观察调色板（禁止当迁移目标 hex）：{pal}\n"
        f"- 技法向标签：{tech}\n"
        "- FACE/HAIR/FABRIC/FINISH：未识别（WD 不能代替卡 II 目测）。\n"
        "WD 标签是内容普查，不是画风保真。"
    )
    preserve = (
        "PRESERVE\n"
        "成图人物必须是图一的同一个人：脸型、五官、发型、发色、瞳色、体型、"
        "服装件数与款式、配饰。保留图一每个部位自己的颜色与画法。"
    )
    colour = (
        "COLOUR LOCK\n"
        "每个部位保持图一自己的色相。暗部与高光从该部位自身色相派生，"
        "不得引入参考图的色板。"
    )
    dont = (
        "DO NOT INCLUDE\n"
        f"不要参考图的人。身份向泄漏：{leak}\n"
        f"{_FINGER_NEG}\n"
        "extra character, split-screen reference, text labels, watermark\n"
        "hue shift, palette swap, garment redesigned to suit the pose"
    )
    if kind == "pose":
        return "\n\n".join([head_pose, preserve, pose_block, colour, dont])
    if kind == "style":
        return "\n\n".join([
            head_style, preserve, colour, style_block,
            "DO NOT INCLUDE\n"
            f"不要参考图的人、道具或场景。身份向：{ident}\n"
            f"内容标签（不迁）：{other}\n"
            "hue shift, recolouring, palette swap\n"
            "extra character, text labels, watermark",
        ])
    return "\n\n".join([head_pose, preserve, pose_block, colour, style_block, dont])


def run_extract(board: dict, node_id: str) -> dict:
    node = obj_by_id(board, node_id)
    if not node or node.get("type") != "extract":
        return {"ok": False, "message": "请先选中提取节点。"}
    images = [s for s in incoming(board, str(node.get("id"))) if s.get("type") == "image"]
    if not images:
        return {"ok": False, "message": "把一张图片连到提取节点。"}
    src_obj = images[0]
    src = allowed_path(src_obj.get("path") or "")
    if src is None:
        return {"ok": False, "message": "提取图路径不在工作区内。"}
    if not EXTRACT_SCRIPT.is_file():
        return {"ok": False, "message": f"找不到提取脚本：{EXTRACT_SCRIPT}"}
    kind = (node.get("kind") or "all").strip()
    if kind not in ("pose", "style", "all"):
        kind = "all"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = BOARD_DIR / "extract" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(EXTRACT_SCRIPT), str(src),
        "--out-dir", str(out_dir), "--skip-lineart",
    ]
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    measure = out_dir / "measure.json"
    if proc.returncode != 0 or not measure.is_file():
        return {"ok": False, "message": log[-3000:] or "提取失败。", "log": log[-3000:]}
    try:
        data = json.loads(measure.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"读 measure.json 失败：{e}"}
    files = data.get("files") or []
    if not files:
        return {"ok": False, "message": "提取没有写出文件记录。"}
    text = compile_extract_text(kind, files[0], src_obj.get("name") or src.name)
    node["text"] = text
    node["last_out"] = str(out_dir)
    save_board({"objects": board.get("objects") or []})
    return {
        "ok": True,
        "message": "已按 style-distill 卡 III 编译（本地量测，未请求上游）。接到生成节点即作为提示词。",
        "text": text,
        "kind": kind,
        "out_dir": str(out_dir),
        "node_id": node_id,
        "log": log[-1500:],
    }


UPDATE_REMOTE = "origin"
UPDATE_BRANCH = "master"


def _git(*args: str, timeout: int = 40) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode, out


def update_status() -> dict:
    code, inside = _git("rev-parse", "--is-inside-work-tree")
    if code != 0 or inside.splitlines()[-1:] != ["true"]:
        return {"ok": False, "message": "当前目录不是 git 仓库，无法在网页更新。"}
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch.splitlines() or ["?"])[-1]
    _, local = _git("rev-parse", "HEAD")
    local = (local.splitlines() or [""])[-1]
    _, dirty = _git("status", "--porcelain")
    dirty_n = len([ln for ln in dirty.splitlines() if ln.strip()])
    fetch_code, fetch_out = _git("fetch", UPDATE_REMOTE, UPDATE_BRANCH)
    remote_ref = f"{UPDATE_REMOTE}/{UPDATE_BRANCH}"
    _, remote = _git("rev-parse", remote_ref)
    remote = (remote.splitlines() or [""])[-1] if fetch_code == 0 else ""
    ahead = behind = 0
    if local and remote:
        _, counts = _git("rev-list", "--left-right", "--count", f"{local}...{remote}")
        parts = (counts.splitlines() or ["0\t0"])[-1].split()
        if len(parts) >= 2:
            ahead, behind = int(parts[0]), int(parts[1])
    _, subject = _git("log", "-1", "--format=%h %s", remote_ref if remote else "HEAD")
    latest = (subject.splitlines() or [""])[-1]
    if fetch_code != 0:
        return {
            "ok": False,
            "message": "拉取远程失败（网络或未配置 origin）。\n" + fetch_out[-800:],
            "branch": branch,
            "local": local[:12],
            "dirty": dirty_n,
        }
    if behind > 0:
        msg = f"有新版本：落后 origin/{UPDATE_BRANCH} {behind} 个提交。最新：{latest}"
        if dirty_n:
            msg += f"\n本地有 {dirty_n} 处未提交改动，网页更新会拒绝覆盖。"
    elif dirty_n:
        msg = f"已是最新提交，但本地有 {dirty_n} 处未提交改动。"
    else:
        msg = f"已是最新。当前 {local[:12]}（{branch}）"
    return {
        "ok": True,
        "message": msg,
        "branch": branch,
        "local": local[:12],
        "remote": remote[:12],
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty_n,
        "latest": latest,
        "can_update": behind > 0 and dirty_n == 0 and ahead == 0,
    }


def apply_update() -> dict:
    st = update_status()
    if not st.get("ok"):
        return st
    if st.get("dirty"):
        return {"ok": False, "message": "本地有未提交改动，拒绝覆盖。请先自行提交或另开干净目录。", **st}
    if st.get("ahead"):
        return {"ok": False, "message": "本地比远程超前，拒绝强制覆盖。", **st}
    if not st.get("behind"):
        return {"ok": True, "message": "没有可更新的提交。", **st}
    code, out = _git("merge", "--ff-only", f"{UPDATE_REMOTE}/{UPDATE_BRANCH}")
    if code != 0:
        return {"ok": False, "message": "快进合并失败。\n" + out[-1200:], **st}
    _, local = _git("rev-parse", "HEAD")
    return {
        "ok": True,
        "message": "已更新到 origin/master（快进）。刷新页面加载新画布。服务进程若仍缓存旧模块，请重启 zimage.py board。\n" + out[-800:],
        "local": (local.splitlines() or [""])[-1][:12],
        "behind": 0,
        "dirty": 0,
        "can_update": False,
    }
