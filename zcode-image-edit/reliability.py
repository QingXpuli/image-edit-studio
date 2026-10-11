# -*- coding: utf-8 -*-
"""zimage reliability primitives.

This module is deliberately local-only: hashing, manifests, atomic writes, and
post-generation image/mask measurements. It never sends a network request and
never stores credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

SCHEMA_VERSION = "zimage-reliability-v2"
TASK_FIELDS = ("intent", "input_roles", "change_scope", "deliverable",
               "side_effect_policy", "ui_policy", "checklist")
UNKNOWN_SUBMISSION_STATES = {"submitted", "unknown", "sending", "response_interrupted"}


def job_id() -> str:
    """Return a sortable, collision-resistant job id."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_prompt(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\r\n", "\n").strip())


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_replace(src: Path, dst: Path, *, force: bool = False) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not force:
        raise FileExistsError(f"输出已存在（加 --force 才覆盖）：{dst}")
    os.replace(src, dst)


def atomic_copy(src: Path, dst: Path, *, force: bool = True) -> None:
    """Copy a completed artifact atomically; never expose a half-written cache file."""
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not force:
        raise FileExistsError(f"目标已存在：{dst}")
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cache_entry(root: Path, fingerprint: str) -> tuple[Path, Path, Path]:
    """Return (entry_dir, result_path, manifest_path) for one request fingerprint."""
    entry = root / fingerprint[:2] / fingerprint
    return entry, entry / "result.png", entry / "manifest.json"


def load_valid_cache(root: Path, fingerprint: str) -> dict[str, Any] | None:
    """Return a validated cache record, or None for any incomplete/corrupt entry."""
    entry, result, manifest = cache_entry(root, fingerprint)
    if not result.is_file() or not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("request_fingerprint") != fingerprint:
            return None
        validation = dict(data.get("validation") or {})
        if validation.get("status") not in ("PASS", "PASS_WITH_WARNING", "NEEDS_REVIEW"):
            return None
        if data.get("result_sha256") != sha256_file(result):
            return None
        with Image.open(result) as im:
            im.load()
        # Cache entries without semantic evidence are review candidates only.
        if not validation.get("semantic"):
            validation["status"] = "NEEDS_REVIEW"
            validation["semantic"] = {"status": "PENDING", "reason": "missing semantic review"}
            data["validation"] = validation
        data["result_path"] = str(result)
        return data
    except (OSError, ValueError, TypeError):
        return None


def store_cache(root: Path, fingerprint: str, result: Path, **metadata: Any) -> dict[str, Any]:
    """Store a validated result and its metadata; manifest is written last."""
    entry, cached_result, manifest = cache_entry(root, fingerprint)
    entry.mkdir(parents=True, exist_ok=True)
    atomic_copy(result, cached_result, force=True)
    with Image.open(cached_result) as im:
        im.load()
        result_size = list(im.size)
    record = {
        "request_fingerprint": fingerprint,
        "result_sha256": sha256_file(cached_result),
        "result_size": result_size,
        "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **metadata,
    }
    write_manifest(manifest, record)
    return record


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def file_record(path: Path, *, field: str = "") -> dict[str, Any]:
    path = Path(path).resolve()
    size = path.stat().st_size
    digest = sha256_file(path)
    return {"field": field, "path": str(path), "filename": path.name,
            "bytes": size, "size": size, "sha256": digest, "hash": digest}


def validate_task(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("task must be an object")
    if data.get("schema") not in (None, "zimage-task-v1"):
        raise ValueError("unsupported task schema")
    required = ("intent", "input_roles", "change_scope", "deliverable",
                "side_effect_policy", "ui_policy")
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError("task missing fields: " + ", ".join(missing))
    if not isinstance(data.get("input_roles"), list):
        raise ValueError("task.input_roles must be a list")
    policy = data.get("side_effect_policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("task.side_effect_policy must be an object")
    if not isinstance(data.get("deliverable"), dict):
        raise ValueError("task.deliverable must be an object")
    if "required_checks" in data and not isinstance(data["required_checks"], list):
        raise ValueError("task.required_checks must be a list")
    return json.loads(canonical_json(data))


def load_task(path: Path) -> dict[str, Any]:
    try:
        return validate_task(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"task file invalid: {exc}") from exc


def validate_prepared_payload(path: Path, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load the payload agent's frozen manifest and reject changed files."""
    path = Path(path).resolve()
    try:
        import sys
        sys.path.insert(0, str(path.parent))
        from image_payload import load_prepared_payload
        loaded = load_prepared_payload(path, expected_fields=(expected or {}).get("fields"),
                                       expected_params=(expected or {}).get("effective_params"))
        data = dict(getattr(loaded, "__dict__", {}) or {})
        if not data:
            data = {name: getattr(loaded, name) for name in
                    ("fields", "files", "preview_bytes", "coverage", "normalization_warnings",
                     "effective_params", "schema_version", "payload_sha256", "total_bytes",
                     "slots", "operation", "endpoint") if hasattr(loaded, name)}
        data["manifest_path"] = str(path)
        return data
    except ImportError:
        pass
    except Exception as exc:
        raise ValueError(f"prepared payload invalid: {exc}") from exc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"prepared payload unreadable: {exc}") from exc
    for item in data.get("files") or []:
        p = Path(item.get("path", ""))
        if not p.is_absolute():
            p = (path.parent / p).resolve()
        digest = item.get("sha256") or item.get("hash")
        if not p.is_file() or not digest or sha256_file(p) != digest:
            raise ValueError(f"prepared payload source changed: {p}")
    data["manifest_path"] = str(path)
    return data


def task_fingerprint_view(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task or task.get("legacy_unknown"):
        return {}
    view = json.loads(canonical_json(task))
    deliverable = view.get("deliverable")
    if isinstance(deliverable, dict):
        deliverable.pop("path", None)
    for role in view.get("input_roles", []) or []:
        if isinstance(role, dict):
            path = role.pop("path", None)
            if path and Path(path).is_file():
                role["sha256"] = sha256_file(Path(path))
    return view


def payload_fingerprint(payload: dict[str, Any]) -> str:
    return sha256_text(canonical_json({
        "payload_sha256": payload.get("payload_sha256"),
        "effective_params": payload.get("effective_params") or payload.get("fields") or {},
        "files": [{k: item.get(k) for k in ("field", "filename", "sha256", "hash", "bytes", "size")}
                  for item in payload.get("files", [])],
        "operation": payload.get("operation", "edit"),
    }))


def update_review(cache_root: Path, fingerprint: str, *, verdict: str,
                  checks: dict[str, str], required: list[str], output_sha256: str,
                  technical_status: str) -> dict[str, Any]:
    if verdict not in {"pass", "fail", "uncertain"}:
        raise ValueError("review verdict must be pass, fail, or uncertain")
    entry, result, manifest = cache_entry(cache_root, fingerprint)
    if not manifest.is_file() or not result.is_file():
        raise ValueError("cache entry does not exist")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    actual = sha256_file(result)
    if output_sha256 != actual:
        raise ValueError("review output sha256 mismatch")
    if technical_status == "FAIL":
        raise ValueError("technical validation failed")
    missing = [name for name in required if checks.get(name) != "pass"]
    status = "PASS" if verdict == "pass" and not missing else ("FAIL" if verdict == "fail" else "NEEDS_REVIEW")
    validation = dict(data.get("validation") or {})
    validation.update({"status": status, "technical_status": technical_status,
                       "semantic": {"status": verdict.upper(), "checks": checks,
                                     "required": required}})
    data.update({"validation": validation, "result_sha256": actual,
                 "review": {"verdict": verdict, "checks": checks, "required": required,
                            "output_sha256": actual}})
    write_manifest(manifest, data)
    return data


class SubmissionLockError(RuntimeError):
    pass


class SubmissionLock:
    def __init__(self, path: Path, record: dict[str, Any]):
        self.path, self.record = Path(path), record
        self.token = record["token"]
        self.submitted = False

    def mark(self, state: str, **extra: Any) -> None:
        self.record.update(state=state, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), **extra)
        atomic_write_text(self.path, json.dumps(self.record, ensure_ascii=False, indent=2) + "\n")
        self.submitted = state in {"sending", "submitted", "unknown", "response_interrupted"}

    def release(self) -> None:
        if self.submitted:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return pid > 0
    except (OSError, ValueError):
        return False


def acquire_submission_lock(root: Path, fingerprint: str, *, job_id: str) -> SubmissionLock:
    entry, _, _ = cache_entry(root, fingerprint)
    entry.mkdir(parents=True, exist_ok=True)
    path = entry / "owner.lock"
    record = {"pid": os.getpid(), "job_id": job_id, "fingerprint": fingerprint,
              "token": uuid.uuid4().hex, "state": "prepared",
              "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
        return SubmissionLock(path, record)
    except FileExistsError:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubmissionLockError("fingerprint owner unreadable; refusing resend") from exc
        if current.get("state") in {"sending", "submitted", "unknown", "response_interrupted"}:
            raise SubmissionLockError("fingerprint has persistent submission; refusing resend")
        if _pid_alive(int(current.get("pid", -1))):
            raise SubmissionLockError("fingerprint owned by live job")
        try:
            path.unlink()
        except OSError as exc:
            raise SubmissionLockError("dead pre-submit owner could not be reclaimed") from exc
        return acquire_submission_lock(root, fingerprint, job_id=job_id)


def write_shared_marker(path: Path, **data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(data, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), pid=os.getpid())
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())


def _scrub(value: Any, *, field: str = "") -> Any:
    """递归清理 manifest 里的常见凭据字段和值。"""
    sensitive = any(x in field.lower() for x in ("key", "authorization", "token", "secret", "password"))
    if sensitive:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _scrub(v, field=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, field=field) for v in value]
    if isinstance(value, str):
        if "sk-" in value or "Bearer " in value:
            return "[REDACTED]"
    return value


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    clean = _scrub(data)
    atomic_write_text(path, json.dumps(clean, ensure_ascii=False, indent=2) + "\n")


def update_manifest(path: Path, **updates: Any) -> dict[str, Any]:
    """Merge updates atomically, preserving a redacted state history for diagnosis."""
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    previous = current.get("state")
    next_state = updates.get("state")
    if next_state and next_state != previous:
        history = list(current.get("state_history") or [])
        history.append({"state": next_state, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        updates["state_history"] = history
    current.update(updates)
    write_manifest(path, current)
    return current


def request_fingerprint(*, source: Path | None, mask: Path | None, prompt: str,
                        model: str, size: str, quality: str,
                        pad: str, mask_invert: bool,
                        refs: list[Path] | None = None,
                        encode: str = "png", jpg_quality: int = 90,
                        budget_mib: float = 1.55,
                        endpoint: str = "",
                        mask_primary: bool = False,
                        input_fidelity: str = "", schema_prompt_hash: str = "",
                        operation: str = "edit", sample_id: str = "",
                        task: dict[str, Any] | None = None,
                        payload_sha256: str = "") -> str:
    payload = {
        "source_sha256": sha256_file(source) if source else None,
        "mask_sha256": sha256_file(mask) if mask else None,
        "ref_sha256": [sha256_file(Path(ref)) for ref in (refs or [])],
        "normalized_prompt_sha256": sha256_text(normalize_prompt(prompt)),
        "model": model,
        "size": size,
        "quality": quality,
        "pad": pad,
        "encode": encode,
        "jpg_quality": int(jpg_quality),
        "budget_mib": float(budget_mib),
        "endpoint": endpoint.rstrip("/"),
        "mask_invert": bool(mask_invert),
        "mask_primary": bool(mask_primary),
        "input_fidelity": input_fidelity or "",
        "schema_prompt_hash": schema_prompt_hash or "",
        "operation": operation or "edit",
        "sample_id": sample_id or "",
        "task": task_fingerprint_view(task),
        "payload_sha256": payload_sha256 or "",
        "schema": SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def legacy_request_fingerprint(*, source: Path | None, mask: Path | None, prompt: str,
                                model: str, size: str, quality: str, pad: str,
                                mask_invert: bool, refs: list[Path] | None = None,
                                encode: str = "png", jpg_quality: int = 90,
                                budget_mib: float = 1.55, endpoint: str = "",
                                mask_primary: bool = False) -> str:
    payload = {
        "source_sha256": sha256_file(source) if source else None,
        "mask_sha256": sha256_file(mask) if mask else None,
        "ref_sha256": [sha256_file(Path(ref)) for ref in (refs or [])],
        "normalized_prompt_sha256": sha256_text(normalize_prompt(prompt)),
        "model": model, "size": size, "quality": quality, "pad": pad,
        "encode": encode, "jpg_quality": int(jpg_quality),
        "budget_mib": float(budget_mib), "endpoint": endpoint.rstrip("/"),
        "mask_invert": bool(mask_invert), "mask_primary": bool(mask_primary),
        "schema": SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def technical_image_metrics(path: Path, *, requested_size: str | None = None) -> dict[str, Any]:
    """Deterministic decode/shape/content sanity checks; not semantic acceptance."""
    try:
        with Image.open(path) as im:
            im.load()
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32)
            size = [int(im.width), int(im.height)]
        mean = float(rgb.mean())
        variance = float(rgb.var())
        warnings: list[str] = []
        status = "PASS"
        if variance < 1.0:
            status = "PASS_WITH_WARNING"
            warnings.append("结果像素方差很低，可能是近乎纯色图；需要语义复核")
        if requested_size:
            expected = [int(x) for x in requested_size.split("x")]
            if size != expected:
                status = "PASS_WITH_WARNING"
                warnings.append(f"结果尺寸 {size[0]}x{size[1]} != 请求 {expected[0]}x{expected[1]}")
        return {
            "status": status,
            "size": size,
            "bytes": path.stat().st_size,
            "mean_rgb": round(mean, 3),
            "variance_rgb": round(variance, 3),
            "warnings": warnings,
        }
    except Exception as exc:
        return {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}", "warnings": ["结果无法完成技术检查"]}


def _crop_for_target(im: Image.Image, target_ar: float) -> Image.Image:
    src_ar = im.width / im.height
    if src_ar > target_ar:
        nw = int(im.height * target_ar)
        x = (im.width - nw) // 2
        return im.crop((x, 0, x + nw, im.height))
    nh = int(im.width / target_ar)
    y = (im.height - nh) // 2
    return im.crop((0, y, im.width, y + nh))


def _fit_mask(mask: Image.Image, tw: int, th: int, pad: str, out_size: tuple[int, int]) -> np.ndarray:
    """将源图坐标 alpha 映射到结果坐标，严格复刻 normalise_to_size 的 crop/pad 语义。"""
    target_ar = tw / th
    if pad == "crop":
        fitted = _crop_for_target(mask, target_ar)
        return np.asarray(fitted.resize(out_size, Image.NEAREST)) > 127
    scale = min(tw / mask.width, th / mask.height)
    nw, nh = max(1, round(mask.width * scale)), max(1, round(mask.height * scale))
    fitted = Image.new("L", (tw, th), 0)
    resized = mask.resize((nw, nh), Image.NEAREST)
    fitted.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return np.asarray(fitted.resize(out_size, Image.NEAREST)) > 127


def _fit_source(source: Image.Image, tw: int, th: int, pad: str, out_size: tuple[int, int]) -> Image.Image:
    """和 mask 使用同一对齐规则，生成用于保护区比较的源图。"""
    target_ar = tw / th
    if pad == "crop":
        fitted = _crop_for_target(source.convert("RGB"), target_ar)
        return fitted.resize(out_size, Image.LANCZOS)
    scale = min(tw / source.width, th / source.height)
    nw, nh = max(1, round(source.width * scale)), max(1, round(source.height * scale))
    fitted = Image.new("RGB", (tw, th), (255, 255, 255))
    resized = source.convert("RGB").resize((nw, nh), Image.LANCZOS)
    fitted.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return fitted.resize(out_size, Image.LANCZOS)


def protection_metrics(source: Path, result: Path, mask: Path, *, requested_size: str,
                       pad: str = "crop", invert: bool) -> dict[str, Any]:
    """Compare result against a normalized source and report editable/protected diffs."""
    sw, sh = (int(x) for x in requested_size.split("x"))
    src = Image.open(source).convert("RGB")
    out = Image.open(result).convert("RGB")
    out.load()
    src_fit = _fit_source(src, sw, sh, pad, out.size)

    m = Image.open(mask)
    m.load()
    alpha = np.asarray(m.convert("RGBA").getchannel("A")) > 127
    alpha_img = Image.fromarray((alpha.astype(np.uint8) * 255), "L")
    # 与 run_round 的 normalise_to_size 使用相同 pad/crop 语义，不能直接拉伸整张 mask。
    edit = _fit_mask(alpha_img, sw, sh, pad, out.size)
    protected = ~edit if not invert else edit

    a = np.asarray(src_fit).astype(np.float32)
    b = np.asarray(out).astype(np.float32)
    diff = np.abs(a - b).mean(axis=2)
    protected_mean = float(diff[protected].mean()) if protected.any() else 0.0
    editable_mean = float(diff[~protected].mean()) if (~protected).any() else 0.0
    protected_unchanged = float((diff[protected] < 12).mean()) if protected.any() else 1.0
    editable_changed = float((diff[~protected] > 12).mean()) if (~protected).any() else 0.0
    ratio = editable_mean / max(protected_mean, 1e-6)
    status = "PASS"
    warnings: list[str] = []
    if protected_unchanged < 0.98:
        status = "FAIL"
        warnings.append(f"保护区保持率 {protected_unchanged * 100:.1f}% < 98%")
    if editable_mean <= protected_mean and (~protected).any():
        status = "FAIL"
        warnings.append("可改区平均差异不高于保护区，疑似未按遮罩编辑")
    if out.size != (sw, sh):
        if status == "PASS":
            status = "PASS_WITH_WARNING"
        warnings.append(f"结果尺寸 {out.width}x{out.height} != 请求 {sw}x{sh}")
    return {
        "status": status,
        "source_size": list(src.size),
        "result_size": list(out.size),
        "requested_size": [sw, sh],
        "protected_mean_abs_diff": round(protected_mean, 3),
        "editable_mean_abs_diff": round(editable_mean, 3),
        "protected_unchanged_ratio": round(protected_unchanged, 5),
        "editable_changed_ratio": round(editable_changed, 5),
        "editable_to_protected_ratio": round(ratio, 3),
        "warnings": warnings,
    }
