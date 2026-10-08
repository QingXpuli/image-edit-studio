#!/usr/bin/env python3
"""zimage —— 在 ZCode 里用的改图工具（不含任何 DSH 依赖）。

它本身**不重写**任何链路：发送/压缩/体积门禁/重试/探测式下载/解码校验全部委托给
`style-distill/round_lib/run_round.py`（那是本项目的实测主力），本脚本只做三件事：
  1. 把"区域规格"变成遮罩 PNG（调 mask_gen.py）——因为 ZCode 里没有涂抹画布；
  2. 凭据与前置检查（在花钱之前就失败，而不是跑到一半才发现没配 key）；
  3. 把本地服务（网页手涂 / 画廊 / 无限画布）拉起、打开、停掉——替掉 DSH 插件那个"按钮"。

子命令：
  edit      改图：区域→遮罩→调用接口→落盘（默认先给预览，可 --dry-run 不发送）
  local     确定性本地操作（线稿调淡/放大/裁切/拼版/调子剖面），不调接口
  serve     拉起网页手涂页并打开浏览器
  gallery   拉起画廊页并打开浏览器
  board     拉起无限画布（放图、加字、写提示词、生成贴回）
  stop      停掉上面拉起的服务
  doctor    自检：依赖、凭据、脚本就位、ZCode 存储、技能安装状态（不调接口）

凭据只从环境变量读：RELAY_API_KEY（必填）/ RELAY_BASE_URL / RELAY_MODEL。
"""
from __future__ import annotations
import sys as _sys

# Windows 上 stdout 默认 cp936：打印 GBK 编不出的字符会直接崩，故脚本自带 UTF-8 前置。
# 注意顺序：若本文件将来引入 from __future__，它必须是第一条语句。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PKG = _HERE.parent                      # zcode-image-edit/


def find_root() -> Path:
    """定位工作区根目录。

    不写死"往上两层"——那样换布局就断。优先 ZIMAGE_ROOT，否则从本文件往上找
    同时含 compose/images/mask_edit_app.py 与 style-distill/round_lib/run_round.py 的目录。
    """
    env = os.environ.get("ZIMAGE_ROOT")
    if env:
        p = Path(env)
        if not p.is_dir():
            raise SystemExit(f"ZIMAGE_ROOT 指向的目录不存在：{p}")
        return p.resolve()
    for cand in [_HERE, *_HERE.parents]:
        if (cand / "compose" / "images" / "mask_edit_app.py").is_file() and \
           (cand / "style-distill" / "round_lib" / "run_round.py").is_file():
            return cand
    raise SystemExit(
        "找不到工作区根目录（需含 compose/images/mask_edit_app.py 与 "
        "style-distill/round_lib/run_round.py）。请设 ZIMAGE_ROOT 指过去。")


ROOT = find_root()
APP = ROOT / "compose" / "images" / "mask_edit_app.py"
ROUND = ROOT / "style-distill" / "round_lib" / "run_round.py"
LOCAL = ROOT / "style-distill" / "round_lib" / "local_ops.py"
MASKGEN = PKG / "mask_gen.py"
RELIABILITY = PKG / "reliability.py"
PY = sys.executable

sys.path.insert(0, str(PKG))
import reliability as R  # noqa: E402


# ---------------------------------------------------------------- 工具

def _pidfile(port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"zimage-service-{port}.pid"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(); s.settimeout(1.0)
    try:
        s.connect((host, port)); return True
    except Exception:
        return False
    finally:
        s.close()


def _http_ok(url: str, timeout: float = 8.0) -> int:
    """用 urllib 取状态码；失败返回 0。

    ⚠ 不要用 `fetch` 那种"请求后立刻 abort"的探活方式去探测以 HTTP/1.0 应答的本服务——
    文档 B3 记过一次实测：那会在 undici 内部抛不可捕获断言、直接杀掉宿主进程。
    这里是独立的 Python 进程，用 urllib 正常读完整响应，安全。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read(64)
            return int(r.status)
    except Exception:
        return 0


def _open_browser(url: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(url)          # noqa: S606
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception as e:
        print(f"  （自动打开浏览器失败：{type(e).__name__}: {e}；请手动打开 {url}）")


def _flush() -> None:
    """子进程直接写终端，但父进程的 stdout 被管道缓冲——不先 flush 自己的，
    子进程的输出就会插到前面，日志顺序乱掉。每次 spawn 之前都要调。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.flush()
        except Exception:
            pass


def _run(argv: list[str], *, capture: bool = False, cwd: Path | None = None,
         env: dict | None = None) -> tuple[int, str] | int:
    _flush()
    if not capture:
        return subprocess.call(argv, cwd=str(cwd) if cwd else None, env=env)
    p = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    chunks: list[str] = []
    assert p.stdout is not None
    for line in p.stdout:
        print(line, end="", flush=True)
        chunks.append(line)
    return p.wait(), "".join(chunks)


def _resolved_key() -> str:
    """与 gen.py 同一套凭据解析（进程环境 → 用户级注册表回退）。

    不能直接读 os.environ：进程的环境块是启动时固定的，若 ZCode 早于环境变量设置而启动，
    这里会报"未配置"而 gen.py 却能拿到凭据——出现自相矛盾的假阴性。
    """
    try:
        sys.path.insert(0, str(APP.parent))
        import gen  # noqa: E402
        return gen.KEY
    except Exception:
        return os.environ.get("RELAY_API_KEY", "")


def _gen_sizes() -> dict:
    """从 gen.py 读尺寸白名单——单一事实来源，避免这里再抄一份导致漂移。"""
    sys.path.insert(0, str(APP.parent))
    import gen  # noqa: E402
    return gen.SIZES


def _resolved_endpoint() -> str:
    sys.path.insert(0, str(APP.parent))
    import gen  # noqa: E402
    return gen.BASE


def _resolved_model() -> str:
    sys.path.insert(0, str(APP.parent))
    import gen  # noqa: E402
    return gen.MODEL


# ---------------------------------------------------------------- reliability

RELIABILITY_STATES = {
    "created", "preflight_ok", "prepared", "submitted", "retrying",
    "received", "decoded", "validated", "cache_hit", "saved", "failed", "blocked",
}


def _cache_root() -> Path:
    value = os.environ.get("ZIMAGE_CACHE_DIR", "")
    return Path(value).expanduser().resolve() if value else ROOT / ".zimage" / "cache"


def _job_dir(job: str) -> Path:
    base = os.environ.get("ZIMAGE_JOBS_DIR", "")
    root = Path(base).expanduser().resolve() if base else ROOT / ".zimage" / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    d = root / job
    try:
        d.mkdir()
    except FileExistsError:
        raise SystemExit(f"Job ID 已存在，请使用新的 --job-id：{job}")
    return d


def _write_state(path: Path, state: str, **extra) -> None:
    if state not in RELIABILITY_STATES:
        raise ValueError(state)
    data = {"state": state, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **extra}
    R.write_manifest(path, data)


def _read_prompt(a) -> str:
    prompt = a.prompt
    if a.prompt_file:
        prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        raise SystemExit("必须给 --prompt（或 --prompt-file）")
    return prompt


def _validate_input(image: Path) -> tuple[int, int]:
    if not image.is_file():
        raise SystemExit(f"找不到输入图：{image}")
    from PIL import Image
    try:
        im = Image.open(image); im.load()
    except Exception as e:
        raise SystemExit(f"输入图无法完整解码：{image}: {e}")
    if im.width < 16 or im.height < 16:
        raise SystemExit(f"输入图尺寸过小：{im.size}")
    return im.size


def _prepare_mask(a, image: Path, dst: Path) -> Path | None:
    import shutil
    if a.mask_file:
        src = Path(a.mask_file)
        if not src.is_file():
            raise SystemExit(f"找不到遮罩文件：{src}")
        shutil.copy2(src, dst)
        return dst
    if a.whole:
        return None
    if not (a.rect or a.polygon or a.flood or a.grabcut is not None):
        raise SystemExit(
            "没有给出要改的区域。请用 --rect/--polygon/--flood/--grabcut，"
            "或 --whole，或 --mask-file。")
    argv = [PY, str(MASKGEN), "--image", str(image), "-o", str(dst)]
    for x in a.rect:
        argv += ["--rect", x]
    for x in a.polygon:
        argv += ["--polygon", x]
    for x in a.flood:
        argv += ["--flood", x]
    if a.grabcut is not None:
        argv += ["--grabcut", a.grabcut]
    if a.dilate:
        argv += ["--dilate", str(a.dilate)]
    rc = _run(argv)
    if rc:
        raise SystemExit(rc)
    return dst


def _mask_summary(source: Path, mask: Path, *, protect: bool) -> tuple[float, list[str]]:
    from PIL import Image
    import numpy as np
    src = Image.open(source); src.load()
    m = Image.open(mask); m.load()
    warnings: list[str] = []
    if m.size != src.size:
        raise SystemExit(f"遮罩尺寸 {m.size} 与源图 {src.size} 不同；请先对齐，不静默重采样")
    alpha = np.asarray(m.convert("RGBA").getchannel("A")) > 127
    ratio = float(alpha.mean())
    if not ratio:
        raise SystemExit("遮罩为空：alpha=255 的区域为 0")
    if ratio < 0.001:
        warnings.append(f"遮罩面积 {ratio * 100:.3f}% 过小，可能几乎没有可见修改")
    if ratio > 0.80:
        warnings.append(f"遮罩面积 {ratio * 100:.1f}% 过大，接近整图重绘")
    if protect:
        warnings.append("反向语义：圈中区域保护，圈外整张重建")
    return ratio, warnings


def _estimate_bytes(image: Path, mask: Path | None, refs: list[str], a,
                    *, mask_primary: bool = False) -> tuple[int | None, float | None]:
    """本地估算真实请求体，不发送。

    局部路径按实际 image/mask 载荷算；整图路径复用 run_round 的 JPEG/压缩梯子，
    这样 plan 输出的数字与后面 dry-run 的口径一致。
    """
    from PIL import Image
    sys.path.insert(0, str(ROUND.parent))
    import run_round as rr
    src = Image.open(image).convert("RGB")
    ref_imgs = [Image.open(x).convert("RGB") for x in refs]
    tw, th = (int(x) for x in a.size.split("x"))
    if mask:
        files, coverage = rr.build_with_mask(src, mask, tw, th, a.pad, ref_imgs, invert=a.protect)
        if mask_primary:
            total = len(files["image[1]"][1]) + len(files["mask"][1])
        else:
            total = sum(len(v[1]) for v in files.values())
        return total, coverage
    if getattr(a, "encode", "png") == "jpg":
        files = rr.build_jpg(src, ref_imgs, tw, th, a.pad, getattr(a, "jpg_quality", 90))
        return sum(len(v[1]) for v in files.values()), None
    c2, r2, _note = rr.pick_reduction(src, ref_imgs, tw, th, a.pad,
                                       int(getattr(a, "budget_mib", 1.55) * 1048576))
    total = rr.norm_bytes(c2, tw, th, a.pad) + sum(rr.norm_bytes(r, tw, th, a.pad) for r in r2)
    return total, None


def _validate_refs(refs: list[str]) -> list[str]:
    out = []
    for ref in refs:
        p = Path(ref)
        if not p.is_file():
            raise SystemExit(f"找不到参考图：{p}")
        _validate_input(p)
        out.append(str(p.resolve()))
    return out


def _transport_report(path: Path, *, marked: bool) -> dict:
    sys.path.insert(0, str(APP.parent))
    import image_transport as T
    fallback = T.CallResult(phase="child_process", classification="child_report_missing",
                            acceptance="unknown" if marked else "not_sent",
                            submit_attempts=None if marked else 0).report()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # An allowlist prevents a malformed child report leaking arbitrary strings.
        phases = {"preflight", "submit", "read_response", "decode_response", "response",
                  "submission_guard", "snapshot", "parse_response", "decode_image",
                  "download", "result_pending", "saved", "recovery"}
        classifications = {"not_sent", "missing_config", "local_error", "transport_error",
                           "timeout", "response_interrupted", "invalid_response_encoding",
                           "response_complete", "submission_already_marked", "snapshot_failed",
                           "auth_error", "rate_limited", "request_rejected", "server_error",
                           "http_error", "moderation_blocked", "invalid_json", "invalid_json_shape",
                           "no_data", "no_image", "invalid_b64", "invalid_image", "output_exists",
                           "download_unavailable", "download_failed", "local_result_error",
                           "response_error", "url_pending", "saved", "cannot_recover_unknown"}
        if data.get("phase") not in phases or data.get("classification") not in classifications:
            return fallback
        clean = {"phase": data["phase"], "classification": data["classification"],
                 "acceptance": "not_sent" if data.get("acceptance") == "not_sent" else "unknown",
                 "retryable_generation": False}
        for name in ("http_status", "content_length", "received_bytes", "submit_attempts", "elapsed_s"):
            value = data.get(name)
            clean[name] = value if value is None or isinstance(value, (int, float)) else None
        ctype = data.get("content_type")
        clean["content_type"] = ctype if ctype in (None, "application/json", "text/json", "text/plain",
                                                  "text/html", "application/octet-stream", "other") else "other"
        return clean
    except (OSError, ValueError, TypeError):
        return fallback


def _freeze_transport_env():
    sys.path.insert(0, str(APP.parent))
    import gen
    env = dict(os.environ)
    env.update(RELAY_API_KEY=gen.KEY, RELAY_API_KEY_HD=gen.KEY_HD,
               RELAY_BASE_URL=gen.BASE, RELAY_MODEL=gen.MODEL)
    return env


PLAN_ARTIFACT_TYPE = "zimage.plan"
PLAN_SCHEMA_VERSION = "zimage-plan-v1"
JOB_ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _valid_job_id(value: str) -> str:
    if not JOB_ID_RX.fullmatch(value) or value in (".", ".."):
        raise SystemExit("--job-id 只能以字母或数字开头，后接字母、数字、点、下划线或连字符")
    return value


def _plan_data(a, *, persist_mask: Path | None = None) -> tuple[dict, Path | None]:
    image = Path(a.image).resolve()
    source_size = _validate_input(image)
    prompt = _read_prompt(a)
    a.ref = _validate_refs(a.ref)
    sizes = _gen_sizes()
    if a.size not in sizes.get(a.quality, []):
        raise SystemExit(f"--size {a.size} 不在 {a.quality} 档白名单内：{sizes.get(a.quality)}")
    mask = None
    temp_dir = None
    if a.whole and (a.mask_file or a.rect or a.polygon or a.flood or a.grabcut is not None or a.protect):
        raise SystemExit("--whole 不能同时指定区域、遮罩或 --protect")
    if not a.whole:
        if persist_mask is not None:
            persist_mask.parent.mkdir(parents=True, exist_ok=True)
            mask = _prepare_mask(a, image, persist_mask)
        else:
            temp_dir = Path(tempfile.mkdtemp(prefix="zimage-plan-"))
            mask = _prepare_mask(a, image, temp_dir / "mask.png")
    if mask:
        ratio, warnings = _mask_summary(image, mask, protect=a.protect)
    else:
        ratio, warnings = None, []
    model = a.model or _resolved_model()
    mask_primary = bool(mask and not a.protect and not getattr(a, "no_primary", False))
    fp = R.request_fingerprint(source=image, mask=mask, prompt=prompt,
                               model=model, size=a.size,
                               quality=a.quality, pad=a.pad, mask_invert=a.protect,
                               refs=[Path(x) for x in a.ref], encode=getattr(a, "encode", "png"),
                               jpg_quality=getattr(a, "jpg_quality", 90),
                               budget_mib=getattr(a, "budget_mib", 1.55),
                               endpoint=_resolved_endpoint(),
                               mask_primary=mask_primary)
    estimated, coverage = (_estimate_bytes(image, mask, a.ref, a, mask_primary=mask_primary)
                           if mask else (None, None))
    if mask and a.protect:
        warnings.append("反向保护模式不使用 mask-primary：保护语义就是保留圈内原像素")
    mask_sha256 = R.sha256_file(mask) if mask else None
    if temp_dir is not None:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    data = {
        "artifact_type": PLAN_ARTIFACT_TYPE,
        "schema_version": PLAN_SCHEMA_VERSION,
        "job_id": getattr(a, "job_id", "") or R.job_id(),
        "source": str(image),
        "source_size": list(source_size),
        "source_sha256": R.sha256_file(image),
        "mask": str(mask) if mask else None,
        "mask_sha256": mask_sha256,
        "mask_mode": "protect" if a.protect else ("edit" if mask else "whole"),
        "mask_ratio": ratio,
        "coverage_target_ratio": coverage,
        "refs": [{"path": str(Path(x).resolve()), "sha256": R.sha256_file(Path(x))} for x in a.ref],
        "model": model,
        "size": a.size,
        "quality": a.quality,
        "pad": a.pad,
        "encode": getattr(a, "encode", "png"),
        "jpg_quality": getattr(a, "jpg_quality", 90),
        "budget_mib": getattr(a, "budget_mib", 1.55),
        "mask_primary": mask_primary,
        "mask_invert": bool(a.protect),
        "endpoint_sha256": R.sha256_text(_resolved_endpoint()),
        "prompt_sha256": R.sha256_text(R.normalize_prompt(prompt)),
        "fingerprint": fp,
        "estimated_request_bytes": estimated,
        "budget_estimate": "deferred_to_run_round_dry_run" if estimated is None else "local_exact",
        "warnings": warnings,
        "status": "preflight_ok",
    }
    return data, mask


def _write_plan_artifact(a, plan_out: Path) -> tuple[dict, Path]:
    plan_out = plan_out.resolve()
    if getattr(a, "job_id", ""):
        _valid_job_id(a.job_id)
    assets = plan_out.parent / f"{plan_out.stem}.assets"
    force = bool(getattr(a, "force", False))
    if (plan_out.exists() or assets.exists()) and not force:
        raise SystemExit(f"计划已存在，请选择新路径或加 --force：{plan_out}")
    prompt = _read_prompt(a)
    plan_out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan_out.stem}.", dir=plan_out.parent))
    try:
        data, mask = _plan_data(a, persist_mask=staging / "mask.png")
        R.atomic_write_text(staging / "prompt.txt", prompt)
    except BaseException:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise
    mask = assets / "mask.png" if mask is not None else None
    prompt_path = assets / "prompt.txt"
    artifact = {
        "artifact_type": data["artifact_type"],
        "schema_version": data["schema_version"],
        "job_id": data["job_id"],
        "status": data["status"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {
            "path": data["source"],
            "sha256": data["source_sha256"],
            "size": data["source_size"],
        },
        "refs": [{**ref, "role": "reference"} for ref in data["refs"]],
        "mask": ({
            "path": str(mask),
            "sha256": data["mask_sha256"],
            "mode": data["mask_mode"],
            "ratio": data["mask_ratio"],
        } if mask else None),
        "prompt_path": str(prompt_path),
        "prompt_sha256": data["prompt_sha256"],
        "request": {
            "model": data["model"],
            "size": data["size"],
            "quality": data["quality"],
            "pad": data["pad"],
            "encode": data["encode"],
            "jpg_quality": data["jpg_quality"],
            "budget_mib": data["budget_mib"],
            "mask_primary": data["mask_primary"],
            "mask_invert": data["mask_invert"],
            "endpoint_sha256": data["endpoint_sha256"],
        },
        "fingerprint": data["fingerprint"],
        "estimated_request_bytes": data["estimated_request_bytes"],
        "budget_estimate": data["budget_estimate"],
        "coverage_target_ratio": data["coverage_target_ratio"],
        "warnings": data["warnings"],
    }
    import shutil
    backup = Path(tempfile.mkdtemp(prefix=f".{plan_out.stem}.backup.", dir=plan_out.parent)) if force else None
    old_plan = backup / "plan.json" if backup else None
    old_assets = backup / "assets" if backup else None
    published = False
    try:
        if plan_out.exists():
            plan_out.replace(old_plan)
        if assets.exists():
            shutil.move(str(assets), str(old_assets))
        staging.rename(assets)
        R.write_manifest(plan_out, artifact)
        published = True
    except BaseException:
        try:
            if plan_out.exists():
                plan_out.unlink()
            if assets.exists():
                shutil.rmtree(assets, ignore_errors=True)
            if old_plan and old_plan.exists():
                old_plan.replace(plan_out)
            if old_assets and old_assets.exists():
                shutil.move(str(old_assets), str(assets))
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if backup:
                shutil.rmtree(backup, ignore_errors=True)
        raise
    if backup:
        shutil.rmtree(backup, ignore_errors=True)
    return artifact, plan_out


def _load_plan_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"计划文件无法读取：{path}: {exc}")
    if data.get("artifact_type") != PLAN_ARTIFACT_TYPE or data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SystemExit(f"不支持的计划文件：{path}")
    return data


def _apply_plan_to_args(a, plan: dict) -> None:
    source = (plan.get("source") or {}).get("path")
    request = plan.get("request") or {}
    if not isinstance(source, str):
        raise SystemExit("计划缺少 source.path")
    a.image = source
    a.prompt = ""
    a.prompt_file = plan.get("prompt_path", "")
    if not a.prompt_file:
        raise SystemExit("计划缺少 prompt_path")
    a.ref = [item["path"] for item in plan.get("refs", [])]
    a.size = request["size"]
    a.quality = request["quality"]
    a.pad = request["pad"]
    a.encode = request.get("encode", "png")
    a.jpg_quality = int(request.get("jpg_quality", 90))
    a.budget_mib = float(request.get("budget_mib", 1.55))
    a.model = request.get("model", "")
    mask = plan.get("mask")
    a.protect = (mask or {}).get("mode") == "protect"
    a.whole = mask is None
    a.mask_file = "" if a.whole else mask.get("path", "")
    a.rect, a.polygon, a.flood = [], [], []
    a.grabcut = None
    a.dilate = 0
    a.no_primary = not bool(request.get("mask_primary", False))
    a.plan_job_id = plan.get("job_id", "")
    a.plan_expected = plan


def _verify_plan_inputs(plan: dict, prompt: str, image: Path, refs: list[str], mask: Path | None) -> None:
    source = plan.get("source") or {}
    if source.get("sha256") != R.sha256_file(image):
        raise SystemExit("计划失效：源图内容已变化")
    expected_refs = plan.get("refs") or []
    if [str(Path(x).resolve()) for x in refs] != [str(Path(x["path"]).resolve()) for x in expected_refs]:
        raise SystemExit("计划失效：参考图列表已变化")
    if any(item.get("sha256") != R.sha256_file(Path(item["path"])) for item in expected_refs):
        raise SystemExit("计划失效：参考图内容已变化")
    if plan.get("prompt_sha256") != R.sha256_text(R.normalize_prompt(prompt)):
        raise SystemExit("计划失效：提示词已变化")
    expected_mask = (plan.get("mask") or {}).get("sha256")
    if (expected_mask or None) != (R.sha256_file(mask) if mask else None):
        raise SystemExit("计划失效：遮罩已变化")
    request = plan.get("request") or {}
    if request.get("endpoint_sha256") != R.sha256_text(_resolved_endpoint()):
        raise SystemExit("计划失效：endpoint 已变化")


def cmd_plan(a) -> int:
    """纯本地计划：图片/遮罩/尺寸/指纹/预算，绝不调用上游。"""
    if getattr(a, "plan_out", ""):
        data, plan_path = _write_plan_artifact(a, Path(a.plan_out))
        print("  plan artifact:", plan_path)
    else:
        data, mask = _plan_data(a)
    source = data["source"] if isinstance(data.get("source"), str) else data["source"]["path"]
    source_size = data.get("source_size") or data["source"]["size"]
    mask_info = data.get("mask")
    mask_mode = data.get("mask_mode") or (mask_info["mode"] if mask_info else "whole")
    mask_ratio = data.get("mask_ratio") if data.get("mask_ratio") is not None else (mask_info.get("ratio") if mask_info else None)
    request = data.get("request", data)
    print("== zimage plan（本地，不发送）==")
    print("  job_id     :", data["job_id"])
    print("  源图       :", source)
    print("  源图尺寸   :", "x".join(map(str, source_size)))
    print("  任务       :", mask_mode)
    print("  目标尺寸   :", request["size"], "质量:", request["quality"], "pad:", request["pad"])
    print("  模型       :", request["model"])
    print("  prompt sha  :", data["prompt_sha256"][:16])
    print("  fingerprint :", data["fingerprint"])
    print("  请求体估算 :", f"{data['estimated_request_bytes']:,} B" if data["estimated_request_bytes"] else "整图路径由 run_round dry-run 精算")
    print("  修改面积   :", f"{mask_ratio * 100:.1f}%" if mask_ratio is not None else "整图")
    print("  状态       : preflight_ok（计划可提交；仍需 dry-run 预算）")
    for w in data["warnings"]:
        print("  ⚠", w)
    return 0


def cmd_edit(a) -> int:
    recovering = bool(getattr(a, "recover_from", ""))
    recovery_dir = None
    if recovering:
        recovery_dir = Path(a.recover_from).resolve()
        if recovery_dir.is_file():
            recovery_dir = recovery_dir.parent
        try:
            recipe = json.loads((recovery_dir / "recovery.private.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("FAILED: cannot_recover_unknown; no POST performed")
            return 1
        out, force, no_cache = a.out, a.force, a.no_cache
        for name, value in recipe.items():
            if name not in {"fn", "cmd", "recover_from", "out", "force", "no_cache"}:
                setattr(a, name, value)
        a.out, a.force, a.no_cache = out, force, no_cache
    if getattr(a, "plan_file", ""):
        allowed = {"--plan-file", "--out", "--dry-run", "--force", "--no-cache", "--job-id"}
        conflicts = {token.split("=", 1)[0] for token in sys.argv[2:]
                     if token.startswith("--") and token.split("=", 1)[0] not in allowed}
        if conflicts:
            raise SystemExit("--plan-file 不能混用其他输入参数：" + ", ".join(sorted(conflicts)))
        plan_path = Path(a.plan_file).resolve()
        plan = _load_plan_file(plan_path)
        _apply_plan_to_args(a, plan)
    if not getattr(a, "image", ""):
        raise SystemExit("必须给 --image 或 --plan-file")
    image = Path(a.image).resolve()
    _validate_input(image)
    prompt_txt = _read_prompt(a)
    a.ref = _validate_refs(a.ref)
    out = Path(a.out).resolve()
    if out.exists() and not a.force:
        raise SystemExit(f"输出已存在（加 --force 才覆盖）：{out}")

    plan_mask = None
    if getattr(a, "plan_expected", None) is not None:
        expected_mask_path = (a.plan_expected.get("mask") or {}).get("path")
        plan_mask = Path(expected_mask_path).resolve() if expected_mask_path else None
        if plan_mask and not plan_mask.is_file():
            raise SystemExit(f"计划失效：遮罩文件不存在：{plan_mask}")
        _verify_plan_inputs(a.plan_expected, prompt_txt, image, a.ref, plan_mask)
        plan_request = a.plan_expected.get("request") or {}
        mask_primary = bool(plan_mask and not a.protect and not getattr(a, "no_primary", False))
        planned_fp = R.request_fingerprint(
            source=image, mask=plan_mask, prompt=prompt_txt,
            model=a.model or _resolved_model(), size=a.size,
            quality=a.quality, pad=a.pad, mask_invert=a.protect,
            refs=[Path(x) for x in a.ref], encode=a.encode,
            jpg_quality=getattr(a, "jpg_quality", 90), budget_mib=a.budget_mib,
            endpoint=_resolved_endpoint(), mask_primary=mask_primary)
        if a.plan_expected.get("fingerprint") != planned_fp:
            raise SystemExit("计划失效：请求 fingerprint 已变化")
        if plan_request.get("endpoint_sha256") != R.sha256_text(_resolved_endpoint()):
            raise SystemExit("计划失效：endpoint 已变化")

    job = recovery_dir.name if recovering else (_valid_job_id(a.job_id) if a.job_id else R.job_id())
    jd = recovery_dir if recovering else _job_dir(job)
    manifest = jd / "manifest.json"
    print(f"  job_id  : {job}")
    print(f"  manifest: {manifest}")
    if not recovering:
        _write_state(manifest, "created", job_id=job, source=str(image), status="created",
                     submit_attempts=0, acceptance="not_sent", phase="preflight")
    transport_env = _freeze_transport_env()
    try:
        plan = getattr(a, "plan_expected", None)
        if plan is not None:
            mask = plan_mask
            ratio = (plan.get("mask") or {}).get("ratio")
            warnings = list(plan.get("warnings") or [])
            fp = plan["fingerprint"]
            estimated = plan.get("estimated_request_bytes")
            coverage = plan.get("coverage_target_ratio")
        else:
            mask = Path(a.mask_file) if recovering and a.mask_file else _prepare_mask(a, image, jd / "mask.png")
            ratio, warnings = _mask_summary(image, mask, protect=a.protect) if mask else (None, [])
            mask_primary = bool(mask and not a.protect and not getattr(a, "no_primary", False))
            fp = R.request_fingerprint(source=image, mask=mask, prompt=prompt_txt,
                                       model=a.model or _resolved_model(), size=a.size,
                                       quality=a.quality, pad=a.pad, mask_invert=a.protect,
                                       refs=[Path(x) for x in a.ref], encode=a.encode,
                                       jpg_quality=getattr(a, "jpg_quality", 90),
                                       budget_mib=a.budget_mib,
                                       endpoint=_resolved_endpoint(),
                                       mask_primary=mask_primary)
            estimated, coverage = _estimate_bytes(
                image, mask, a.ref, a, mask_primary=mask_primary
            )
            if mask and a.protect:
                warnings.append("反向保护模式不使用 mask-primary：保护语义就是保留圈内原像素")
        prompt_file = jd / "prompt.txt"
        R.atomic_write_text(prompt_file, prompt_txt)
        R.update_manifest(manifest, job_id=job, state="preflight_ok", status="preflight_ok",
                          source_sha256=R.sha256_file(image),
                          mask_sha256=R.sha256_file(mask) if mask else None,
                          prompt_sha256=R.sha256_text(R.normalize_prompt(prompt_txt)),
                          model=a.model or _resolved_model(), size=a.size, quality=a.quality,
                          pad=a.pad, mask_mode=("protect" if a.protect else ("edit" if mask else "whole")),
                          mask_ratio=ratio, coverage_target_ratio=coverage,
                          request_fingerprint=fp, warnings=warnings)
        R.update_manifest(manifest, state="prepared", status="prepared")

        cache = None if a.no_cache or a.dry_run or recovering else R.load_valid_cache(_cache_root(), fp)
        if cache is not None:
            cached_result = Path(cache["result_path"])
            cache_validation = cache.get("validation") or {"status": "PASS"}
            R.atomic_copy(cached_result, out, force=a.force)
            R.update_manifest(manifest, state="cache_hit", status="cache_hit",
                              cache_dir=str(_cache_root()), cached_result_sha256=cache.get("result_sha256"),
                              output=str(out), result_size=cache.get("result_size"),
                              validation=cache_validation)
            final_status = cache_validation.get("status", "PASS")
            R.update_manifest(manifest, state="saved", status=final_status, output=str(out), from_cache=True)
            note = "需语义复核" if final_status == "NEEDS_REVIEW" else "已验收"
            print(f"✓ 缓存命中：{fp} → {out}  （未发送上游；{note}）")
            return 0
        if not recovering and not a.dry_run and not transport_env.get("RELAY_API_KEY"):
            R.update_manifest(manifest, state="blocked", status="auth_error", failure_type="missing_config",
                              phase="preflight", acceptance="not_sent", submit_attempts=0)
            print("✗ 未配置 RELAY_API_KEY —— 在花钱之前先停下。")
            return 2

        sys.path.insert(0, str(APP.parent))
        import image_transport as T
        if not recovering:
            recipe = {name: getattr(a, name) for name in (
                "image", "prompt_file", "ref", "size", "quality", "pad", "encode", "jpg_quality",
                "model", "budget_mib", "whole", "protect", "no_primary", "mask_file",
                "rect", "polygon", "flood", "grabcut", "dilate")}
            recipe.update(image=str(image), prompt="", prompt_file=str(prompt_file),
                          mask_file=str(mask) if mask else "", dry_run=False, plan_file="")
            T.private_snapshot(jd / "recovery.private.json", json.dumps(recipe))
        temp_out = jd / ("recovered.tmp.png" if recovering else "result.tmp.png")
        report_path = jd / ("recovery.transport.json" if recovering else "transport.json")
        snapshot_path = jd / "response.private.json"
        argv = [PY, str(ROUND), "--content", str(image), "--prompt", str(prompt_file),
                "--out", str(temp_out), "--size", a.size, "--quality", a.quality,
                "--pad", a.pad, "--encode", a.encode, "--jpg-quality", str(a.jpg_quality),
                "--budget-mib", str(a.budget_mib), "--transport-report", str(report_path),
                "--snapshot", str(snapshot_path)]
        if recovering:
            argv += ["--recover-from", str(snapshot_path)]
        for r in a.ref:
            argv += ["--ref", r]
        if mask:
            argv += ["--mask", str(mask)]
            if a.protect:
                argv += ["--mask-invert"]
            elif not a.no_primary:
                argv += ["--mask-primary"]
        if a.dry_run:
            argv += ["--dry-run"]
        if a.model:
            argv += ["--model", a.model]
        if a.input_fidelity:
            argv += ["--input-fidelity", a.input_fidelity]
        R.update_manifest(manifest, state="prepared", status="prepared", request_bytes=estimated)
        rc, log = _run(argv, capture=True, env=transport_env)
        R.atomic_write_text(jd / ("recovery.process.log" if recovering else "process.log"), log)
        report = _transport_report(report_path, marked=Path(str(temp_out) + ".submission").exists() or recovering)
        R.update_manifest(manifest, transport=report, phase=report["phase"],
                          acceptance=report["acceptance"], submit_attempts=report["submit_attempts"],
                          retryable_generation=False, attempts=report["submit_attempts"], recovery=recovering)
        if a.dry_run:
            R.update_manifest(manifest, state="prepared", status="plan_only", dry_run=True,
                              submit_attempts=0, acceptance="not_sent")
            return rc
        if rc != 0:
            failure = report["classification"]
            R.update_manifest(manifest, state="blocked" if failure == "moderation_blocked" else "failed",
                              status=failure, failure_type=failure)
            return rc
        R.update_manifest(manifest, state="received", status="received")
        if not temp_out.is_file():
            R.update_manifest(manifest, state="failed", status="invalid_image", failure_type="invalid_image")
            print("✗ 上游返回成功但没有结果文件")
            return 3
        from PIL import Image
        try:
            im = Image.open(temp_out); im.load()
        except Exception as e:
            R.update_manifest(manifest, state="failed", status="invalid_image", failure_type="invalid_image",
                              error=str(e))
            print("✗ 结果图片无法完整解码：", e)
            return 3
        R.update_manifest(manifest, state="decoded", status="decoded",
                          result_size=list(im.size), result_bytes=temp_out.stat().st_size)
        metrics = None
        validation = None
        if mask:
            metrics = R.protection_metrics(image, temp_out, mask,
                                           requested_size=a.size, pad=a.pad, invert=a.protect)
            R.update_manifest(manifest, state="validated", status=metrics["status"], validation=metrics)
            print("结果验收：", json.dumps(metrics, ensure_ascii=False))
            if metrics["status"] == "FAIL":
                R.update_manifest(manifest, state="failed", status="protected_area_changed",
                                  failure_type="protected_area_changed")
                print("✗ 遮罩验收失败；结果保留在 Job 目录，没有替换最终输出")
                return 4
        else:
            technical = R.technical_image_metrics(temp_out, requested_size=a.size)
            if technical["status"] == "FAIL":
                R.update_manifest(manifest, state="failed", status="invalid_image",
                                  failure_type="invalid_image", technical_validation=technical)
                print("✗ 结果技术验收失败：", json.dumps(technical, ensure_ascii=False))
                return 3
            validation = {
                "status": "NEEDS_REVIEW",
                "technical": technical,
                "semantic": {"status": "PENDING", "reason": "整图结果需按任务要求做视觉语义验收"},
            }
            R.update_manifest(manifest, state="validated", status="NEEDS_REVIEW", validation=validation)
            print("整图技术验收通过；语义验收待 AI 视觉复核：", json.dumps(technical, ensure_ascii=False))
        R.atomic_replace(temp_out, out, force=a.force)
        cache_warning = None
        try:
            cached = None if a.no_cache else R.store_cache(
                _cache_root(), fp, out,
                result_size=list(im.size), validation=validation or metrics or {"status": "PASS"})
            cache_warning = None
        except Exception as cache_error:
            cached = None
            cache_warning = f"缓存写入失败：{type(cache_error).__name__}: {cache_error}"
            print("⚠", cache_warning)
        R.update_manifest(manifest, state="saved", status=(validation or metrics or {}).get("status", "NEEDS_REVIEW"),
                          output=str(out), cache_stored=bool(cached), cache_warning=cache_warning)
        print(f"✓ 结果原子写入：{out}  {im.size[0]}x{im.size[1]}  {out.stat().st_size / 1024:.0f} KB")
        return 0
    except BaseException as e:
        marked = any(jd.glob("*.submission"))
        R.update_manifest(manifest, state="failed", status="local_error", failure_type="local_error",
                          phase="outer_process", acceptance="unknown" if marked or recovering else "not_sent")
        print("FAILED: local_error (safe diagnostic)")
        return 1

# ---------------------------------------------------------------- 本地服务

def _start_service(port: int, detach: bool) -> subprocess.Popen | None:
    """已在跑就直接用；否则拉起。返回新进程（已在跑时返回 None）。"""
    if _port_open(port):
        print(f"  服务已在 127.0.0.1:{port} 运行，直接复用")
        return None
    popen_kw = dict(cwd=str(APP.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if detach and os.name == "nt":
        popen_kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS|NEW_PROCESS_GROUP
    elif detach:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen([PY, str(APP), "--port", str(port)], **popen_kw)
    _pidfile(port).write_text(str(proc.pid), encoding="utf-8")
    for _ in range(30):
        if _port_open(port):
            print(f"  服务已起于 127.0.0.1:{port}（pid {proc.pid}）")
            return proc
        time.sleep(0.4)
    print(f"  ✗ 服务 12 秒内没起来（pid {proc.pid}）")
    print(f"    自检：{PY} \"{APP}\" --port {port}   ← 直接前台跑会打印原因")
    return proc


def _serve(a, path: str) -> int:
    url = f"http://127.0.0.1:{a.port}{path}"
    proc = _start_service(a.port, detach=not a.foreground)
    code = _http_ok(url)
    print(f"  GET {url} → HTTP {code}" + (" ✓" if code == 200 else " ✗"))
    if code == 200 and not a.no_open:
        _open_browser(url)
        print(f"  已尝试打开浏览器：{url}")
    if a.foreground and proc is not None:
        print("  前台运行中，Ctrl+C 结束")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0
    print(f"  后台运行中。停止：python zimage.py stop --port {a.port}")
    return 0 if code == 200 else 1


def cmd_serve(a) -> int:
    return _serve(a, "/")


def cmd_gallery(a) -> int:
    return _serve(a, "/gallery")


def cmd_board(a) -> int:
    return _serve(a, "/board")


def cmd_stop(a) -> int:
    pf = _pidfile(a.port)
    if not pf.is_file():
        print(f"  没有 pid 记录（{pf}）；端口{'在听' if _port_open(a.port) else '也没在听'}")
        return 0
    pid = pf.read_text(encoding="utf-8").strip()
    print(f"  停止 pid {pid}（端口 {a.port}）")
    if os.name == "nt":
        subprocess.call(["taskkill", "/PID", pid, "/F"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    else:
        try:
            os.kill(int(pid), 15)
        except Exception as e:
            print("  kill 失败:", e)
    time.sleep(1.0)
    print("  端口仍在听:", _port_open(a.port))
    if not _port_open(a.port):
        pf.unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------- doctor

def cmd_doctor(a) -> int:
    print("== zimage 自检 ==")
    print(f"  Python        : {sys.version.split()[0]}  ({PY})")
    print(f"  工作区根      : {ROOT}")

    print("\n  [依赖]")
    for m in ("PIL", "numpy", "cv2"):
        try:
            mod = __import__(m)
            print(f"    {m:8s} OK  {getattr(mod, '__version__', '?')}")
        except Exception as e:
            print(f"    {m:8s} **缺失** {type(e).__name__}")

    print("\n  [凭据]（只看有没有，不打印值）")
    k = _resolved_key()
    print(f"    RELAY_API_KEY  : {'已设置（%d 字符）' % len(k) if k else '**未设置** → edit 会直接停下'}")

    sys.path.insert(0, str(APP.parent))
    try:
        import gen
        print(f"    RELAY_BASE_URL : {gen.BASE}")
        print(f"    RELAY_MODEL    : {gen.MODEL}")
    except Exception as e:
        print("    gen.py 导入失败:", e)

    print("\n  [脚本就位]")
    for label, p in (("服务本体 mask_edit_app.py", APP), ("一键跑一轮 run_round.py", ROUND),
                     ("本地操作 local_ops.py", LOCAL), ("遮罩生成 mask_gen.py", MASKGEN)):
        print(f"    {label:28s} {'✓' if p.is_file() else '**缺失**'}  {p}")

    print("\n  [本地服务]")
    for port in (8000, 3080):
        print(f"    {port} {'在听' if _port_open(port) else '未听'}")

    print("\n  [ZCode 存储]（画廊「输入」页签的数据源）")
    zc = Path.home() / ".zcode" / "cli"
    print(f"    db.sqlite   {'✓' if (zc / 'db' / 'db.sqlite').is_file() else '**缺失**'}  {zc / 'db' / 'db.sqlite'}")
    print(f"    artifacts/  {'✓' if (zc / 'artifacts').is_dir() else '**缺失**'}  {zc / 'artifacts'}")

    print("\n  [技能与命令安装状态]")
    sk = Path.home() / ".agents" / "skills" / "image-edit" / "SKILL.md"
    mirror = PKG / "_skill" / "image-edit" / "SKILL.md"
    if sk.is_file() and mirror.is_file():
        import hashlib
        h1 = hashlib.sha256(sk.read_bytes()).hexdigest()[:12]
        h2 = hashlib.sha256(mirror.read_bytes()).hexdigest()[:12]
        print(f"    技能已装      {sk}")
        print(f"    与镜像一致    {'✓' if h1 == h2 else '**不同**（重跑 install.py）'}  {h1} / {h2}")
    elif mirror.is_file():
        print(f"    技能**未装**（镜像在 {mirror}）→ 跑 python zimage.py install 装上")
    else:
        print("    技能既未装、镜像也缺 —— 先确认 zcode-image-edit/_skill/image-edit/ 存在")
    cdir = Path.home() / ".agents" / "commands"
    for c in ("image-edit.md", "paint-mask.md", "image-gallery.md"):
        print(f"    命令 /{c[:-3]:14s} {'✓' if (cdir / c).is_file() else '**未装**'}")
    return 0


def cmd_local(a) -> int:
    argv = [PY, str(LOCAL)] + list(a.rest)
    return _run(argv)


def cmd_install(a) -> int:
    """转发给 install.py：装技能与命令到 ZCode 用户作用域并自检。"""
    argv = [PY, str(PKG / "install.py")] + list(a.rest)
    return _run(argv)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="ZCode 里的改图工具（委托 run_round，无 DSH 依赖）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("edit", help="改图：区域→遮罩→调接口→落盘")
    e.add_argument("--image", default="")
    e.add_argument("--plan-file", default="", help="复用 plan --plan-out 生成的本地计划")
    e.add_argument("--prompt", default="", help="要改成的样子（一句话）")
    e.add_argument("--prompt-file", default="")
    e.add_argument("--out", required=True)
    e.add_argument("--ref", action="append", default=[], help="参考图（可重复，映射 image[1..]）")
    e.add_argument("--rect", action="append", default=[], help="x0,y0,x1,y1（可重复取并集）")
    e.add_argument("--polygon", action="append", default=[], help="x,y x,y x,y …")
    e.add_argument("--flood", action="append", default=[], help="x,y[,tol] 种子点漫水")
    e.add_argument("--grabcut", nargs="?", const="", default=None, help="前景分割（可选初始框）")
    e.add_argument("--dilate", type=int, default=0, help="区域外扩 N 像素")
    e.add_argument("--mask-file", default="", help="用现成遮罩 PNG（alpha=255 即圈中区域）")
    e.add_argument("--protect", action="store_true", help="反向：圈中的区域被**保护**")
    e.add_argument("--no-primary", action="store_true",
                   help="正向时不加 --mask-primary（仅用于验证保护区逐像素未动）")
    e.add_argument("--whole", action="store_true", help="整图改图，不给区域")
    e.add_argument("--size", default="1024x1536")
    e.add_argument("--quality", default="low", choices=["low", "medium", "high"])
    e.add_argument("--pad", default="crop", choices=["pad", "crop"])
    e.add_argument("--encode", default="png", choices=["png", "jpg"])
    e.add_argument("--jpg-quality", type=int, default=90)
    e.add_argument("--model", default="")
    e.add_argument("--input-fidelity", default="", choices=["", "low", "high"],
                   help="画风迁移必带：low＝参考图只取画风不保留内容")
    e.add_argument("--budget-mib", type=float, default=1.55)
    e.add_argument("--dry-run", action="store_true", help="只出预算报表与遮罩预览，不发送")
    e.add_argument("--force", action="store_true", help="允许覆盖已有输出")
    e.add_argument("--no-cache", action="store_true", help="不读取或写入成功结果缓存")
    e.add_argument("--job-id", default="", help="显式指定可复现的 job id")
    e.add_argument("--recover-from", default="", help="Job directory; local decode/URL GET only, never POST")
    e.set_defaults(fn=cmd_edit)

    pl = sub.add_parser("plan", help="只做本地预检与请求计划，不调用上游")
    pl.add_argument("--image", required=True)
    pl.add_argument("--prompt", default="")
    pl.add_argument("--prompt-file", default="")
    pl.add_argument("--ref", action="append", default=[])
    pl.add_argument("--rect", action="append", default=[])
    pl.add_argument("--polygon", action="append", default=[])
    pl.add_argument("--flood", action="append", default=[])
    pl.add_argument("--grabcut", nargs="?", const="", default=None)
    pl.add_argument("--dilate", type=int, default=0)
    pl.add_argument("--mask-file", default="")
    pl.add_argument("--protect", action="store_true")
    pl.add_argument("--whole", action="store_true")
    pl.add_argument("--size", default="1024x1536")
    pl.add_argument("--quality", default="low", choices=["low", "medium", "high"])
    pl.add_argument("--pad", default="crop", choices=["pad", "crop"])
    pl.add_argument("--encode", default="png", choices=["png", "jpg"])
    pl.add_argument("--jpg-quality", type=int, default=90)
    pl.add_argument("--budget-mib", type=float, default=1.55)
    pl.add_argument("--model", default="")
    pl.add_argument("--input-fidelity", default="", choices=["", "low", "high"])
    pl.add_argument("--no-primary", action="store_true")
    pl.add_argument("--plan-out", default="", help="将本地计划原子写入 JSON artifact")
    pl.add_argument("--job-id", default="", help="计划使用的 job id")
    pl.add_argument("--force", action="store_true", help="允许覆盖已有 plan artifact")
    pl.set_defaults(fn=cmd_plan)

    s = sub.add_parser("serve", help="拉起网页手涂页")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--foreground", action="store_true", help="前台运行（Ctrl+C 结束）")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(fn=cmd_serve)

    g = sub.add_parser("gallery", help="拉起画廊页")
    g.add_argument("--port", type=int, default=8000)
    g.add_argument("--foreground", action="store_true")
    g.add_argument("--no-open", action="store_true")
    g.set_defaults(fn=cmd_gallery)

    bd = sub.add_parser("board", help="拉起无限画布（即梦式：放图、加字、写提示词、生成贴回）")
    bd.add_argument("--port", type=int, default=8000)
    bd.add_argument("--foreground", action="store_true")
    bd.add_argument("--no-open", action="store_true")
    bd.set_defaults(fn=cmd_board)

    t = sub.add_parser("stop", help="停掉上面拉起的服务")
    t.add_argument("--port", type=int, default=8000)
    t.set_defaults(fn=cmd_stop)

    d = sub.add_parser("doctor", help="自检（不调接口）")
    d.set_defaults(fn=cmd_doctor)

    lo = sub.add_parser("local", help="确定性本地操作（转发 local_ops.py）")
    lo.add_argument("rest", nargs=argparse.REMAINDER)
    lo.set_defaults(fn=cmd_local)

    ins = sub.add_parser("install", help="装技能与命令到 ZCode 用户作用域（转发 install.py）")
    ins.add_argument("rest", nargs=argparse.REMAINDER)
    ins.set_defaults(fn=cmd_install)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
