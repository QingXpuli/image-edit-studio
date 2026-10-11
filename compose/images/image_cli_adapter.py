"""Web image requests delegate to zimage; credentials remain in child environment."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "zcode-image-edit" / "bin" / "zimage.py"


def _image_bytes(raw: bytes, *, mask: bool = False) -> bytes:
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        buffer = io.BytesIO()
        image.convert("RGBA" if mask else "RGB").save(buffer, "PNG")
        return buffer.getvalue()


def execute_image_request(*, prompt: str, source: bytes | None, refs=(), mask: bytes | None = None,
                          protect: bool = False, size: str = "1024x1024", quality: str = "low",
                          model: str = "", base_url: str = "", api_key: str = "",
                          pad: str = "pad", operation: str = "edit", dry_run: bool = False,
                          input_fidelity: str = "", output_format: str = "png",
                          sample_id: str = "", runner=None) -> dict:
    if not prompt.strip():
        return {"ok": False, "error": "缺少提示词", "retry": False}
    if operation not in ("edit", "generate"):
        return {"ok": False, "error": "未知图片操作", "retry": False}
    if operation == "edit" and not source:
        return {"ok": False, "error": "改图需要内容图", "retry": False}
    if operation == "generate" and (source or refs or mask):
        return {"ok": False, "error": "文生图不接受图片或遮罩", "retry": False}
    if not base_url or not api_key:
        return {"ok": False, "error": "未配置匹配的端点与凭据", "retry": False}
    if "grok" in model.lower() and (refs or mask):
        return {"ok": False, "error": "Grok 不接受参考图或遮罩", "retry": False}

    root = Path(os.environ.get("ZIMAGE_WEB_ROOT") or ROOT / ".zimage" / "web")
    request_id = "web-" + uuid.uuid4().hex
    directory = root / request_id
    directory.mkdir(parents=True)
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    output = directory / "result.png"
    command = [sys.executable, "-B", str(CLI), "edit", "--operation", operation,
               "--prompt-file", str(prompt_path), "--out", str(output), "--size", size,
               "--quality", quality, "--pad", pad, "--encode", "jpg", "--job-id", request_id]
    roles = []
    if source:
        target = directory / "source.png"
        target.write_bytes(_image_bytes(source))
        command.extend(["--image", str(target)])
        roles.append({"id": "target", "path": str(target), "role": "identity"})
    for index, ref in enumerate(refs):
        role, raw = ref if isinstance(ref, tuple) else ("reference", ref)
        path = directory / f"reference-{index}.png"
        path.write_bytes(_image_bytes(raw))
        command.extend(["--ref", str(path)])
        roles.append({"id": f"reference-{index}", "path": str(path), "role": str(role)})
    if mask:
        path = directory / "mask.png"
        path.write_bytes(_image_bytes(mask, mask=True))
        command.extend(["--mask-file", str(path)])
        if protect:
            command.append("--protect")
    elif operation == "edit":
        command.append("--whole")
    if model:
        command.extend(["--model", model])
    if input_fidelity:
        command.extend(["--input-fidelity", input_fidelity])
    if sample_id:
        command.extend(["--sample-id", sample_id])
    if dry_run:
        command.append("--dry-run")
    task = {
        "schema": "zimage-task-v1", "intent": prompt,
        "input_roles": roles, "change_scope": "generate" if operation == "generate" else ("mask" if mask else "whole"),
        "preserve": [], "changes": [prompt], "deliverable": {"size": size, "path": str(output)},
        "side_effect_policy": {"generation_authorized": not dry_run, "live_validation": False},
        "ui_policy": "explicit_web_submit", "required_checks": ["requested_change", "preservation", "reference_leakage"],
    }
    task_path = directory / "task.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    command.extend(["--task-file", str(task_path)])
    env = dict(os.environ)
    env.update(RELAY_BASE_URL=base_url.rstrip("/"), RELAY_API_KEY=api_key,
               RELAY_API_KEY_HD=api_key, RELAY_MODEL=model or "gpt-image-2",
               ZIMAGE_REQUIRE_PUBLIC_DOWNLOAD="1")
    run = runner or subprocess.run
    try:
        process = run(command, env=env, cwd=str(ROOT), capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=1200)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "执行等待超时；受理状态未知，请检查任务记录，不要重复提交。",
                "job_id": request_id, "acceptance": "unknown", "retry": False}
    jobs = Path(env.get("ZIMAGE_JOBS_DIR") or ROOT / ".zimage" / "jobs")
    manifest = jobs / request_id / "manifest.json"
    record = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    result = {
        "ok": process.returncode == 0, "job_id": request_id, "dry_run": dry_run,
        "status": record.get("status"), "task_result": record.get("task_result", record.get("status")),
        "technical_validation": record.get("technical_validation"),
        "semantic_validation": record.get("semantic_validation", (record.get("validation") or {}).get("semantic")),
        "validation": record.get("validation"), "from_cache": bool(record.get("from_cache")),
        "parts_sent": record.get("fields", []), "feed_bytes": record.get("request_bytes"),
        "acceptance": record.get("acceptance", "not_sent" if dry_run else "unknown"), "retry": False,
    }
    if process.returncode:
        detail = ((process.stdout or "") + "\n" + (process.stderr or "")).strip()
        result["error"] = "主执行器拒绝或未完成请求：" + str(record.get("failure_type") or record.get("status") or "preflight_failed")
        if detail:
            result["detail"] = detail[-4000:]
        return result
    if dry_run:
        result["message"] = "预检完成，未发送；预算与槽位见任务记录。"
        return result
    if not output.is_file():
        return {**result, "ok": False, "error": "没有可交付文件，请检查任务记录。"}
    with Image.open(output) as image:
        image.load()
        actual = f"{image.width}x{image.height}"
        buffer = io.BytesIO()
        format_name = {"jpeg": "JPEG", "webp": "WEBP", "png": "PNG"}.get(output_format, "PNG")
        image.convert("RGB").save(buffer, format_name)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    result.update(image_b64=encoded, b64=encoded, actual_size=actual, requested_size=size,
                  mime={"JPEG": "image/jpeg", "WEBP": "image/webp", "PNG": "image/png"}[format_name],
                  result_sha256=hashlib.sha256(output.read_bytes()).hexdigest(), path=str(output),
                  message="文件已保存；语义验收待复核。")
    result["images"] = [{"b64": encoded, "size": actual}]
    return result
