"""Infinite board helpers for /board. Generation delegates to zimage.py / run_round."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# 服务以无控制台后台进程运行;其子进程(git/提取/蒸馏等命令行程序)若不加此标志,
# Windows 会为每次调用弹出命令行窗口(页面每次刷新触发 git fetch,弹窗尤其明显)。
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BOARD_DIR = ROOT / ".zimage" / "board"
BOARD_JSON = BOARD_DIR / "board.json"
BOARDS_DIR = BOARD_DIR / "boards"
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
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


def _board_path(bid: str) -> Path:
    if not BOARD_ID_RE.match(bid or ""):
        raise ValueError("非法画布 id")
    return BOARDS_DIR / f"{bid}.json"


def _board_revision(path: Path) -> str:
    try:
        st = path.stat()
        return f"{st.st_mtime_ns:x}-{st.st_size:x}"
    except OSError:
        return "missing"


@contextmanager
def _board_lock(bid: str):
    """跨进程单画布写锁; revision check + atomic replace 必须处于同一临界区。"""
    lock_path = _board_path(bid).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _migrate_legacy() -> None:
    """旧单文件 board.json → boards/default.json,仅在 boards 目录为空时执行一次。"""
    BOARDS_DIR.mkdir(parents=True, exist_ok=True)
    if BOARD_JSON.is_file() and not any(BOARDS_DIR.glob("*.json")):
        try:
            data = json.loads(BOARD_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            data.setdefault("name", "default")
            (BOARDS_DIR / "default.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )


def empty_board() -> dict:
    return {
        "name": "",
        "cam": {"x": 0, "y": 0, "z": 1},
        "objects": [],
        "edges": [],
        "prompt": "",
        "size": "1024x1536",
        "pad": "pad",
        "input_fidelity": "",
        "identity_scope": "preserve",
        "pose_scope": "preserve",
        "style_scope": "preserve",
        "color_scope": "preserve",
        "identityId": "",
        "refId": "",
        "genId": "",
    }


def load_board(bid: str = "default") -> dict:
    _ensure()
    _migrate_legacy()
    path = _board_path(bid)
    if not path.is_file():
        return empty_board()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    base["_revision"] = _board_revision(path)
    return base


def save_board(
    data: dict,
    bid: str = "default",
    expected_mtime: float | None = None,
    expected_revision: str | None = None,
) -> dict:
    """全量保存。revision/mtime 检查与替换在同一跨进程锁内完成。"""
    _ensure()
    _migrate_legacy()
    p = _board_path(bid)
    with _board_lock(bid):
        current_revision = _board_revision(p)
        if expected_revision is not None and current_revision != expected_revision:
            return {"ok": False, "conflict": True, "revision": current_revision,
                    "message": "画布已被其他端修改，请刷新后再保存。"}
        if expected_mtime is not None and p.is_file():
            try:
                if abs(p.stat().st_mtime - expected_mtime) > 1e-6:
                    return {"ok": False, "conflict": True, "revision": current_revision,
                            "message": "画布刚被其他端修改,请重读后重试。"}
            except OSError:
                pass
        cur = load_board(bid)
        for k in (
            "name", "cam", "objects", "edges", "prompt", "size", "pad",
            "input_fidelity", "identity_scope", "pose_scope", "style_scope", "color_scope",
            "identityId", "refId", "genId",
        ):
            if k in data:
                cur[k] = data[k]
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        cur["_revision"] = _board_revision(p)
        cur["_mtime"] = p.stat().st_mtime
        cur["ok"] = True
        return cur


def list_boards() -> list[dict]:
    _ensure()
    _migrate_legacy()
    out = []
    for p in BOARDS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": p.stem,
            "name": (d.get("name") if isinstance(d, dict) else "") or p.stem,
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
            "objects": len((d.get("objects") or []) if isinstance(d, dict) else []),
        })
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def create_board(name: str) -> dict:
    _ensure()
    _migrate_legacy()
    bid = uuid.uuid4().hex[:8]
    b = empty_board()
    b["name"] = (name or "").strip() or f"画布 {bid}"
    _board_path(bid).write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "id": bid, "name": b["name"]}


def rename_board(bid: str, name: str) -> dict:
    _ensure()
    _migrate_legacy()
    p = _board_path(bid)
    if not p.is_file():
        return {"ok": False, "message": "画布不存在。"}
    b = json.loads(p.read_text(encoding="utf-8"))
    b["name"] = (name or "").strip() or bid
    p.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "id": bid, "name": b["name"]}


def delete_board(bid: str) -> dict:
    _ensure()
    _migrate_legacy()
    if len(list(BOARDS_DIR.glob("*.json"))) <= 1:
        return {"ok": False, "message": "最后一块画布不能删除。"}
    p = _board_path(bid)
    if not p.is_file():
        return {"ok": False, "message": "画布不存在。"}
    p.unlink()
    return {"ok": True, "id": bid}


def list_assets(limit: int = 300) -> dict:
    """产图/ 只读列表,供资产面板浏览。路径一律过 allowed_path 白名单。"""
    _ensure()
    root = ROOT / "产图"
    items: list[dict] = []
    if root.is_dir():
        try:
            candidates = sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
        except OSError:
            candidates = []
        for p in candidates:
            if len(items) >= limit:
                break
            if p.suffix.lower() not in IMG_EXT or not p.is_file():
                continue
            if allowed_path(str(p)) is None:
                continue
            try:
                items.append({"path": str(p), "name": p.name, "mtime": int(p.stat().st_mtime)})
            except OSError:
                continue
    return {"ok": True, "items": items}


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
    ident_id = str(ident.get("id")) if ident else ""
    ref = next((o for o in images if str(o.get("id")) == pick_ref and str(o.get("id")) != ident_id), None)
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


def reference_inputs(board: dict, gen: dict | None, identity_id: str) -> list[tuple[dict, str]]:
    """Collect direct refs and images feeding extract nodes with explicit semantic roles."""
    if not gen:
        return []
    out: list[tuple[dict, str]] = []
    seen: set[str] = set()
    for src in incoming(board, str(gen.get("id"))):
        if src.get("type") == "image":
            sid = str(src.get("id"))
            if sid != identity_id and sid not in seen:
                seen.add(sid)
                out.append((src, str(src.get("role") or "reference")))
        elif src.get("type") == "extract":
            kind = str(src.get("kind") or "all")
            # all = 姿态+风格双段卡,同时为画风与姿态门禁提供证据
            role = ("pose_reference" if kind == "pose"
                    else "style_reference" if kind == "style"
                    else "style_and_pose_reference")
            for img in incoming(board, str(src.get("id"))):
                if img.get("type") != "image":
                    continue
                sid = str(img.get("id"))
                if sid != identity_id and sid not in seen:
                    seen.add(sid)
                    out.append((img, str(img.get("role") or role)))
    return out


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
        return None, "没有有效图一：请把身份图片直接连到生成节点，然后点击「修复角色引用」。"
    src = allowed_path(ident.get("path") or "")
    if src is None:
        return None, "图一路径不在工作区内，或不存在。"
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "把文本节点连到生成节点，或在底部填写提示词。"
    gen_id = str((gen or {}).get("id") or "")
    connected_image_ids = {str(e.get("from")) for e in board.get("edges") or [] if str(e.get("to")) == gen_id}
    unapproved = [o for o in board.get("objects") or [] if str(o.get("id")) in connected_image_ids and o.get("type") == "image" and o.get("role") in ("lineart_reference", "exemplar_reference") and not o.get("distill_approved")]
    if unapproved:
        return None, "蒸馏样例尚未审核：请选中蒸馏产物并点击「审核蒸馏包」，审核后才能预算/生成。"
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
    prompt_path = BOARD_DIR / "requests" / job_id / "prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
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
    # 图一之外的显式参考：直接连线或经提取节点进入生成节点。
    ident_id = str(ident.get("id"))
    ref_inputs = reference_inputs(board, gen, ident_id)
    # GPT 允许多参考；Grok 明确只允许一张参考图。
    if channel == "grok" and len(ref_inputs) > 1:
        return None, "Grok 通道只接受一张参考图；请移除额外风格/姿态样例或改用 GPT。"
    for o, _role in ref_inputs:
        op = allowed_path(o.get("path") or "")
        if op is not None:
            cmd += ["--ref", str(op)]
    rect = rect_to_pixels(ident, board)
    if rect:
        cmd += ["--rect", rect]
    else:
        cmd.append("--whole")
    if channel != "grok" and fidelity in ("low", "high"):
        cmd += ["--input-fidelity", fidelity]
    def scope_value(key: str, *, node_only: bool) -> str:
        # 画风/姿态迁移只认生成节点自己的声明;identity/color 允许全局回退(对色工具旧路径)。
        raw = (gen.get(key) if gen else None) or (None if node_only else board.get(key))
        return raw if raw in ("preserve", "transfer") else "preserve"

    identity_scope = scope_value("identity_scope", node_only=False)
    pose_scope = scope_value("pose_scope", node_only=True)
    style_scope = scope_value("style_scope", node_only=True)
    color_scope = scope_value("color_scope", node_only=False)
    style_roles = ("style_reference", "lineart_reference", "exemplar_reference", "style_and_pose_reference")
    if style_scope == "transfer" and not any(role in style_roles for _o, role in ref_inputs):
        return None, "本生成节点声明了画风迁移,但没有画风证据(style_reference 或蒸馏线稿/样例);请在节点上把「画风」改回保留,或连接画风源/蒸馏包。"
    if pose_scope == "transfer" and not any(role in ("pose_reference", "style_and_pose_reference") for _o, role in ref_inputs):
        return None, "本生成节点声明了姿态迁移,但没有 pose_reference;请在节点上把「姿态」改回保留,或连接姿态源/姿态包。"
    if (style_scope == "transfer" or pose_scope == "transfer") and fidelity != "low":
        return None, "画风/姿态迁移要求 input_fidelity=low；请在生成节点勾选后再预算。"
    roles = [{"id": str(ident.get("id")), "path": str(src), "role": "identity"}]
    for o, role in ref_inputs:
        op = allowed_path(o.get("path") or "")
        if op is not None:
            roles.append({"id": str(o.get("id")), "path": str(op), "role": role})
    preserve = ["identity"] if identity_scope == "preserve" else []
    if color_scope == "preserve":
        preserve.append("target_colors")
    task = {
        "schema": "zimage-task-v1", "intent": prompt, "input_roles": roles,
        "change_scope": "mask" if rect else "whole", "preserve": preserve, "changes": [prompt],
        "scopes": {"identity_scope": identity_scope, "pose_scope": pose_scope, "style_scope": style_scope, "color_scope": color_scope},
        "deliverable": {"path": str(out), "size": size},
        "side_effect_policy": {"generation_authorized": not dry_run},
        "ui_policy": "explicit_board_submit",
        "required_checks": ["requested_change", "preservation", "reference_leakage", "scope_consistency"],
    }
    task_path = prompt_path.with_name("task.json")
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    cmd += ["--task-file", str(task_path)]
    if dry_run:
        cmd.append("--dry-run")
    elif no_cache:
        cmd += ["--sample-id", uuid.uuid4().hex]
    return cmd, ""


def run_edit(board: dict, *, dry_run: bool, no_cache: bool = False) -> dict:
    _ensure()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:10]
    job_id = f"board-{stamp}-{suffix}"
    out = OUT_DIR / f"{job_id}.png"
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
        timeout=1200, creationflags=_CREATE_NO_WINDOW
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
        "fields": "see job payload",
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
    jobs_root = Path(os.environ.get("ZIMAGE_JOBS_DIR") or ROOT / ".zimage" / "jobs")
    manifest_path = jobs_root / job_id / "manifest.json"
    if manifest_path.is_file():
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
        result["validation"] = record.get("validation")
        result["task_result"] = record.get("task_result", record.get("status"))
        result["semantic_validation"] = record.get("semantic_validation", (record.get("validation") or {}).get("semantic"))
    result["path"] = str(out)
    result["w"] = w
    result["h"] = h
    return result


def review_job(job_id: str, verdict: str, checks: dict[str, str]) -> dict:
    if verdict not in ("pass", "fail", "uncertain"):
        return {"ok": False, "message": "verdict 必须是 pass/fail/uncertain。"}
    jobs_root = Path(os.environ.get("ZIMAGE_JOBS_DIR") or ROOT / ".zimage" / "jobs")
    target = jobs_root / job_id
    if not (target / "manifest.json").is_file():
        return {"ok": False, "message": "找不到 Job manifest。"}
    args = [sys.executable, str(ZIMAGE), "review", "--job", job_id, "--verdict", verdict]
    for name, value in checks.items(): args += ["--check", f"{name}={value}"]
    required = ["requested_change", "identity_preservation", "style_transfer", "pose_transfer", "reference_leakage", "color_scope"]
    for name in required: args += ["--required-check", name]
    proc = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=_CREATE_NO_WINDOW)
    log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return {"ok": False, "message": log[-3000:] or "验收登记失败。", "verdict": verdict}
    try: payload = json.loads(proc.stdout)
    except ValueError: payload = {"job": job_id, "verdict": verdict}
    return {"ok": True, **payload}


DISTILL_SCRIPT = HERE / "distill_style.py"
POSE_SCRIPT = HERE / "pose_extract.py"


def run_pose_extract(board: dict, node_id: str) -> dict:
    node = obj_by_id(board, node_id)
    if not node or node.get("type") != "image":
        return {"ok": False, "message": "请先选中姿态源图片。"}
    src = allowed_path(node.get("path") or "")
    if src is None:
        return {"ok": False, "message": "姿态源路径不在工作区内。"}
    out_dir = BOARD_DIR / "pose" / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, str(POSE_SCRIPT), str(src), "--out-dir", str(out_dir)], cwd=str(HERE), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, creationflags=_CREATE_NO_WINDOW)
    log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        try:
            info = json.loads((proc.stdout or "").strip().splitlines()[-1])
            return {"ok": False, "pose_estimated": False, "pose_controlled": False, "message": info.get("message") or "姿态模型不可用。", "detail": info.get("detail", "")}
        except Exception:
            return {"ok": False, "pose_estimated": False, "pose_controlled": False, "message": log[-1000:] or f"姿态提取失败 exit {proc.returncode}"}
    try:
        package = json.loads((out_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"读取姿态包失败:{e}"}
    package["role"] = "pose_reference"
    package["message"] = "姿态关键点已提取；当前为 pose_estimated，不是 pose_controlled。"
    return package


def run_distill(board: dict, node_ids: list[str]) -> dict:
    """对选中的一或多张图片节点做画风蒸馏,产出证据包(多图时跨图聚合)。"""
    nodes = [obj_by_id(board, nid) for nid in node_ids]
    nodes = [n for n in nodes if n]
    if not nodes or any(n.get("type") != "image" for n in nodes):
        return {"ok": False, "message": "请选中一张或多张图片节点再蒸馏。"}
    sources = []
    for n in nodes:
        src = allowed_path(n.get("path") or "")
        if src is None:
            return {"ok": False, "message": f"图片路径不在工作区内:{n.get('path')}"}
        sources.append(str(src))
    if not DISTILL_SCRIPT.is_file():
        return {"ok": False, "message": f"找不到蒸馏脚本:{DISTILL_SCRIPT}"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = BOARD_DIR / "distill" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(DISTILL_SCRIPT), *sources, "--out-dir", str(out_dir)],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800, creationflags=_CREATE_NO_WINDOW
     )
    log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return {"ok": False, "message": log[-3000:] or f"exit {proc.returncode}"}
    try:
        package = json.loads((out_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"读 package.json 失败:{e}"}
    package["ref_paths"] = sources
    package["node_ids"] = [str(n.get("id")) for n in nodes]
    try:
        model_card = out_dir / "model_prompt.md"
        package["card_text"] = model_card.read_text(encoding="utf-8") if model_card.is_file() else (out_dir / "style-card.md").read_text(encoding="utf-8")
    except OSError:
        package["card_text"] = "(画风卡读取失败,产物见 out_dir)"
    n = package.get("source_count", len(sources))
    package["message"] = f"蒸馏完成({n} 张样本聚合):线稿 {len(package.get('linearts') or [])},{len(package.get('exemplars') or [])} 张笔触样例,色调指纹,画风卡。"
    # 画风库:给包一个默认名(可在画风库中改名)
    manifest_p = out_dir / "manifest.json"
    try:
        m = json.loads(manifest_p.read_text(encoding="utf-8"))
        m.setdefault("name", f"画风包 {stamp}({n}样本)")
        manifest_p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        package["name"] = m["name"]
    except OSError:
        pass
    return package


def _style_base() -> Path:
    return (BOARD_DIR / "distill").resolve()


def list_styles() -> dict:
    """画风库:列出全部蒸馏包及其审核状态。"""
    _ensure()
    _migrate_legacy()
    base = _style_base()
    items = []
    if base.is_dir():
        dirs = sorted([x for x in base.iterdir() if x.is_dir()], key=lambda x: x.stat().st_mtime, reverse=True)
        for d in dirs:
            m_p, p_p = d / "manifest.json", d / "package.json"
            if not m_p.is_file():
                continue
            try:
                m = json.loads(m_p.read_text(encoding="utf-8"))
                pkg = json.loads(p_p.read_text(encoding="utf-8")) if p_p.is_file() else {}
            except (OSError, ValueError):
                continue
            preview = d / "source-preview.png"
            card = d / "model_prompt.md"
            if not card.is_file():
                card = d / "style-card.md"
            sources = m.get("sources") or ([m["source"]] if m.get("source") else [])
            items.append({
                "dir": str(d), "name": m.get("name") or d.name,
                "source_count": len(sources),
                "identity_risk": m.get("identity_risk"), "approved": m.get("approved", False),
                "approved_high_risk": m.get("approved_high_risk", False),
                "preview": str(preview) if preview.is_file() else "",
                "card_text": card.read_text(encoding="utf-8") if card.is_file() else "",
                "linearts": pkg.get("linearts") or ([pkg["lineart"]] if pkg.get("lineart") else []),
                "exemplars": pkg.get("exemplars") or [],
                "tone_json": pkg.get("tone_json") or (str(d / "tone.json") if (d / "tone.json").is_file() else ""),
                "manifest": str(m_p),
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
            })
    return {"ok": True, "items": items}


def _style_dir(package_dir: str) -> Path | None:
    """解析蒸馏包目录;相对路径同时尝试 cwd 与 ROOT(服务 cwd 可能不同)。"""
    p = Path(package_dir)
    candidates = [p] if p.is_absolute() else [Path.cwd() / p, ROOT / p]
    for c in candidates:
        try:
            c = c.resolve()
            c.relative_to(_style_base())
        except (ValueError, OSError):
            continue
        if c.is_dir():
            return c
    return None


def rename_style(package_dir: str, name: str) -> dict:
    d = _style_dir(package_dir)
    if d is None:
        return {"ok": False, "message": "蒸馏包不在工作区 distill 目录内。"}
    m_p = d / "manifest.json"
    if not m_p.is_file():
        return {"ok": False, "message": "找不到 manifest.json。"}
    try:
        m = json.loads(m_p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"读取 manifest 失败:{e}"}
    m["name"] = (name or "").strip() or d.name
    m_p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "name": m["name"]}


def delete_style(package_dir: str) -> dict:
    import shutil
    d = _style_dir(package_dir)
    if d is None:
        return {"ok": False, "message": "蒸馏包不在工作区 distill 目录内。"}
    shutil.rmtree(d)
    return {"ok": True, "message": "蒸馏包已删除。"}


def approve_distill(package_dir: str, allow_high_risk: bool = False) -> dict:
    d = _style_dir(package_dir)
    if d is None:
        return {"ok": False, "message": "蒸馏包不在工作区 distill 目录内。"}
    manifest_path = d / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "message": "找不到蒸馏包 manifest.json。"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"读取 manifest 失败:{e}"}
    if manifest.get("identity_risk") == "high" and not allow_high_risk:
        return {"ok": False, "high_risk": True,
                "message": "该蒸馏包身份风险为 high。请逐张检查样例、删除含人脸/身份特征的裁片后，确认强制批准。"}
    manifest["exemplars_reviewed"] = True
    manifest["approved"] = True
    if manifest.get("identity_risk") == "high":
        manifest["approved_high_risk"] = True
        manifest["approved_by"] = "human-override"
    manifest["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "manifest": str(manifest_path), "approved": True,
            "approved_high_risk": manifest.get("approved_high_risk", False)}


def align_tone(board: dict, node_id: str, ref_path: str, authorize_color_transfer: bool = False,
               tone_json: str | None = None) -> dict:
    """把选中图片节点的色调确定性对齐;必须显式授权 color_scope=transfer。"""
    if not authorize_color_transfer and (board.get("color_scope") or "preserve") != "transfer":
        return {"ok": False, "message": "色调对齐会改变配色；请先显式授权 color_scope=transfer。"}
    sys.path.insert(0, str(HERE))
    import distill_style
    node = obj_by_id(board, node_id)
    if not node or node.get("type") != "image":
        return {"ok": False, "message": "请先选中要调色的图片节点。"}
    src = allowed_path(node.get("path") or "")
    if src is None:
        return {"ok": False, "message": "图片路径不在工作区内。"}
    if tone_json:
        tj = Path(tone_json)
        if not tj.is_file() or not tj.resolve().is_relative_to(ROOT.resolve()):
            return {"ok": False, "message": "tone.json 不在工作区内。"}
        tone_file = tj
        ref_name = Path(json.loads(tj.read_text(encoding="utf-8")).get("sources", ["?"])[0]).name
    else:
        ref = allowed_path(ref_path or "")
        if ref is None:
            return {"ok": False, "message": "色调参考路径不在工作区内。"}
        tone_file = _tone_tmp(distill_style.tone_stats(ref))
        ref_name = ref.name
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = OUT_DIR / f"board-{stamp}-toned.png"
    distill_style.apply_tone(src, tone_file, dest)
    if not dest.is_file():
        return {"ok": False, "message": "调色未产出文件。"}
    with Image.open(dest) as im:
        w, h = im.size
    return {"ok": True, "path": str(dest), "w": w, "h": h,
            "message": f"已按 {ref_name} 的 LAB 指纹确定性对齐色调(本地计算)。"}


def _tone_tmp(tone: dict):
    import tempfile
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"tone": tone, "source": ""}, f, ensure_ascii=False)
    f.close()
    return Path(f.name)


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


def run_extract(board: dict, node_id: str, bid: str = "default") -> dict:
    node = obj_by_id(board, node_id)
    if not node or node.get("type") != "extract":
        return {"ok": False, "message": "请先选中提取节点。"}
    images = [s for s in incoming(board, str(node.get("id"))) if s.get("type") == "image"]
    if not images:
        return {"ok": False, "message": "把一张或多张图片连到提取节点。"}
    sources = []
    for s in images:
        src = allowed_path(s.get("path") or "")
        if src is None:
            return {"ok": False, "message": f"提取图路径不在工作区内:{s.get('path')}"}
        sources.append((s, src))
    if not EXTRACT_SCRIPT.is_file():
        return {"ok": False, "message": f"找不到提取脚本：{EXTRACT_SCRIPT}"}
    kind = (node.get("kind") or "all").strip()
    if kind not in ("pose", "style", "all"):
        kind = "all"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = BOARD_DIR / "extract" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    rows = []
    for idx, (s_obj, src) in enumerate(sources):
        sub = out_dir / f"src-{idx}"
        sub.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, str(EXTRACT_SCRIPT), str(src),
            "--out-dir", str(sub), "--skip-lineart",
        ]
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180, creationflags=_CREATE_NO_WINDOW
         )
        measure = sub / "measure.json"
        log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode != 0 or not measure.is_file():
            return {"ok": False, "message": f"第 {idx+1} 张提取失败。{log[-2000:]}"}
        try:
            data = json.loads(measure.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"ok": False, "message": f"读 measure.json 失败:{e}"}
        files = data.get("files") or []
        if files:
            rows.append(files[0])
    if not rows:
        return {"ok": False, "message": "提取没有写出文件记录。"}
    sys.path.insert(0, str(HERE))
    import distill_style
    if len(rows) == 1:
        text = compile_extract_text(kind, rows[0], sources[0][0].get("name") or sources[0][1].name)
    else:
        agg = distill_style.aggregate_rows(rows)
        text = compile_extract_text(kind, agg, f"{len(rows)}张样本聚合")
        weak = [r.get("edge_weak_pct") for r in rows if isinstance(r.get("edge_weak_pct"), (int, float))]
        if kind in ("style", "all") and len(weak) > 1:
            text += f"\n\nMULTI(N={len(rows)})\n- 跨图波动【实测】:弱边缘 {min(weak)}%~{max(weak)}%;波动大说明样本画风不稳定,需人工复核。\n- 每图量测见 src-N/measure.json。"
    node["text"] = text
    node["last_out"] = str(out_dir)
    node["sources"] = [str(src) for _s, src in sources]
    save_board({"objects": board.get("objects") or []}, bid)
    return {
        "ok": True,
        "message": f"已按 style-distill 卡 III 编译({len(rows)} 张样本聚合,本地量测,未请求上游)。接到生成节点即作为提示词。",
        "text": text,
        "kind": kind,
        "samples": len(rows),
        "out_dir": str(out_dir),
        "node_id": node_id,
        "log": "",
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
        timeout=timeout, creationflags=_CREATE_NO_WINDOW
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
