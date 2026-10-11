"""Pure, offline image payload preparation and persistence helpers.

This module deliberately has no credential, GUI, relay, or network imports.  It
is shared by the CLI and the compatibility generator so a prepared payload is
exactly the payload that will be submitted.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image

SCHEMA_VERSION = "image-payload-v1"
SCHEMA_PROMPT = (
    "[MASK INSTRUCTION] The second reference image is the same picture with the region to "
    "modify marked in flat red. Red areas are the ONLY places you may change. Every pixel "
    "outside the red areas must be reproduced exactly as in the first image - same shapes, "
    "same colours, same line art, same lighting, nothing redrawn, nothing restyled, nothing "
    "else changed."
)
PRIMARY_SCHEMA_PROMPT = (
    "[MASK INSTRUCTION] The primary image contains flat red marker pixels. Red areas are "
    "the ONLY places you may change. The following reference images are conditioning images, "
    "not mask images. Preserve all non-red primary-image content and do not treat references "
    "as the red mask."
)
SCHEMA_VERSION = "image-payload-v1"



@dataclass
class PreparedPayload:
    fields: dict[str, str]
    files: OrderedDict[str, tuple[str, bytes, str]]
    preview_bytes: bytes | None = None
    coverage: float | None = None
    normalization_warnings: list[str] = field(default_factory=list)
    input_fidelity: str = ""
    effective_params: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    payload_sha256: str = ""
    total_bytes: int = 0
    slots: list[str] = field(default_factory=list)
    operation: str = "edit"
    endpoint: str = "/images/edits"

    def __post_init__(self) -> None:
        self.files = OrderedDict(self.files)
        self.total_bytes = sum(len(item[1]) for item in self.files.values())
        self.slots = list(self.files)
        self.payload_sha256 = payload_hash(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields), "files": self.files,
            "preview_bytes": self.preview_bytes, "coverage": self.coverage,
            "normalization_warnings": list(self.normalization_warnings),
            "input_fidelity": self.input_fidelity,
            "effective_params": dict(self.effective_params),
            "schema_version": self.schema_version,
            "payload_sha256": self.payload_sha256,
            "total_bytes": self.total_bytes, "slots": list(self.slots),
            "operation": self.operation, "endpoint": self.endpoint,
        }


def normalise_to_size(img: Image.Image, tw: int, th: int, pad_mode: str) -> Image.Image:
    """Fit RGB image to the exact target size, using centered crop or white pad."""
    if tw <= 0 or th <= 0:
        raise ValueError("target size must be positive")
    img = img.convert("RGB")
    src_ar, target_ar = img.width / img.height, tw / th
    if pad_mode == "crop":
        if src_ar > target_ar:
            nw = int(img.height * target_ar)
            left = max(0, (img.width - nw) // 2)
            box = (left, 0, left + nw, img.height)
        else:
            nh = int(img.width / target_ar)
            top = max(0, (img.height - nh) // 2)
            box = (0, top, img.width, top + nh)
        return img.crop(box).resize((tw, th), Image.Resampling.LANCZOS)
    if pad_mode != "pad":
        raise ValueError("pad must be 'crop' or 'pad'")
    scale = min(tw / img.width, th / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (tw, th), (255, 255, 255))
    canvas.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def make_visual_mask(image: Image.Image, canvas_mask: Image.Image,
                     tw: int, th: int, pad_mode: str, invert: bool = False):
    """Return PNG red-overlay bytes and target-size boolean painted array."""
    image = image.convert("RGB")
    fitted = normalise_to_size(image, tw, th, pad_mode)
    if canvas_mask.mode in ("L", "1", "I", "F"):
        gray = canvas_mask.convert("L")
        mask = Image.new("RGBA", gray.size, (0, 0, 0, 0))
        mask.putalpha(gray)
    else:
        mask = canvas_mask.convert("RGBA")
    if pad_mode == "pad":
        scale = min(tw / image.width, th / image.height)
        nw, nh = max(1, round(image.width * scale)), max(1, round(image.height * scale))
        ox, oy = (tw - nw) // 2, (th - nh) // 2
    else:
        nw, nh, ox, oy = tw, th, 0, 0
        source_ar, target_ar = image.width / image.height, tw / th
        if source_ar > target_ar:
            crop = int(image.height * target_ar)
            left = max(0, (image.width - crop) // 2)
            mask = mask.crop((left, 0, left + crop, image.height))
        else:
            crop = int(image.width / target_ar)
            top = max(0, (image.height - crop) // 2)
            mask = mask.crop((0, top, image.width, top + crop))
    painted_small = np.asarray(mask.resize((nw, nh), Image.Resampling.NEAREST).split()[3]) > 127
    if invert:
        painted_small = ~painted_small
    painted = np.zeros((th, tw), dtype=bool)
    painted[oy:oy + nh, ox:ox + nw] = painted_small
    pixels = np.asarray(fitted).astype(np.float32)
    pixels[painted] = (255, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(pixels.astype(np.uint8), "RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue(), painted


def build_mask_alpha(painted: np.ndarray) -> bytes:
    """Encode OpenAI-style alpha mask (transparent where the edit is painted)."""
    out = np.zeros((*painted.shape, 4), dtype=np.uint8)
    out[:, :, 3] = np.where(painted, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _image_bytes(img: Image.Image, tw: int, th: int, pad: str, encode: str, quality: int):
    fitted = normalise_to_size(img, tw, th, pad)
    buf = io.BytesIO()
    if encode == "jpg":
        fitted.save(buf, "JPEG", quality=max(1, min(100, int(quality))), optimize=True)
        return ("image.jpg", buf.getvalue(), "image/jpeg"), fitted
    if encode != "png":
        raise ValueError("encode must be 'png' or 'jpg'")
    fitted.save(buf, "PNG", optimize=True)
    return ("image.png", buf.getvalue(), "image/png"), fitted


def _coerce_image(value: Any) -> Image.Image:
    if isinstance(value, Image.Image):
        return value
    with Image.open(value) as image:
        return image.convert("RGB")


def _endpoint(operation: str, endpoint: str | None) -> str:
    if endpoint:
        return endpoint
    return "/images/generations" if operation == "generate" else "/images/edits"


def prepare_payload(content, refs, prompt: str, size: str, pad: str, quality: str,
                    model: str, mask=None, invert: bool = False, primary: bool = False,
                    encode: str = "png", jpg_quality: int = 90,
                    input_fidelity: str = "", operation: str = "edit",
                    schema_version: str = SCHEMA_VERSION, endpoint: str | None = None,
                    n: str = "1", response_format: str | None = None,
                    budget_bytes: int | None = None) -> PreparedPayload:
    """Prepare fields and multipart files without reading credentials or using a network."""
    try:
        tw, th = (int(x) for x in str(size).lower().split("x", 1))
    except Exception as exc:
        raise ValueError("size must be WIDTHxHEIGHT") from exc
    refs = list(refs or [])
    model_text = str(model or "")
    grok = "grok" in model_text.lower()
    if grok and (mask is not None or refs):
        raise ValueError("unsupported_grok_features: Grok does not accept mask or refs")
    if operation not in ("edit", "generate"):
        raise ValueError("operation must be 'edit' or 'generate'")
    if operation == "edit" and content is None:
        raise ValueError("edit operation requires content")
    if operation == "generate" and (mask is not None or refs):
        raise ValueError("generate operation does not accept mask or refs")

    fields: dict[str, str] = {"model": model_text, "prompt": str(prompt), "n": str(n), "size": str(size)}
    if grok:
        fields["resolution"] = "2k" if max(tw, th) >= 1536 else "1k"
    else:
        fields["quality"] = str(quality)
    if input_fidelity:
        fields["input_fidelity"] = str(input_fidelity)
    if response_format:
        fields["response_format"] = str(response_format)

    files: OrderedDict[str, tuple[str, bytes, str]] = OrderedDict()
    preview = None
    coverage = None
    warnings: list[str] = []
    if operation == "edit":
        source = _coerce_image(content)
        if mask is not None:
            mask_img = _coerce_image(mask) if not isinstance(mask, Image.Image) else mask
            # Preserve alpha for masks; _coerce_image intentionally normalizes ordinary images.
            if not isinstance(mask, Image.Image):
                with Image.open(mask) as opened:
                    mask_img = opened.convert("RGBA")
            red, painted = make_visual_mask(source, mask_img, tw, th, pad, invert)
            if not painted.any():
                raise ValueError("mask is empty")
            alpha = build_mask_alpha(painted)
            coverage = float(painted.mean()) * 100.0
            preview = red
            if primary:
                files["image"] = ("image.png", red, "image/png")
            else:
                base, _ = _image_bytes(source, tw, th, pad, encode, jpg_quality)
                files["image"] = base
                files["image[1]"] = ("mask_ref.png", red, "image/png")
            files["mask"] = ("mask.png", alpha, "image/png")
            start = 1 if primary else 2
            if refs:
                warnings.append("mask refs use image[1..] after primary red image" if primary else
                                "mask refs use image[2..] after visual mask")
            for index, ref in enumerate(refs, start=start):
                item, _ = _image_bytes(_coerce_image(ref), tw, th, pad, encode, jpg_quality)
                files[f"image[{index}]"] = (f"ref{index}.{'jpg' if encode == 'jpg' else 'png'}", item[1], item[2])
        else:
            base, _ = _image_bytes(source, tw, th, pad, encode, jpg_quality)
            files["image"] = base
            for index, ref in enumerate(refs, start=1):
                item, _ = _image_bytes(_coerce_image(ref), tw, th, pad, encode, jpg_quality)
                files[f"image[{index}]"] = (f"ref{index}.{'jpg' if encode == 'jpg' else 'png'}", item[1], item[2])
    if mask is not None and operation == "edit":
        prefix = PRIMARY_SCHEMA_PROMPT if primary else SCHEMA_PROMPT
        if not str(prompt).startswith(prefix):
            fields["prompt"] = prefix + str(prompt)
    effective = {
        "size": str(size), "pad": pad, "quality": quality, "model": model_text,
        "encode": encode, "jpg_quality": int(jpg_quality), "invert": bool(invert),
        "primary": bool(primary), "input_fidelity": input_fidelity,
        "operation": operation, "endpoint": _endpoint(operation, endpoint),
        "schema_version": schema_version,
    }
    result = PreparedPayload(fields, files, preview, coverage, warnings, input_fidelity,
                             effective, schema_version, operation=operation,
                             endpoint=_endpoint(operation, endpoint))
    if budget_bytes is not None and result.total_bytes > int(budget_bytes):
        raise ValueError(f"payload exceeds budget: {result.total_bytes} > {int(budget_bytes)}")
    return result


def _canonical_bytes(prepared: PreparedPayload) -> bytes:
    out = bytearray()
    out.extend(json.dumps({"fields": prepared.fields, "effective": prepared.effective_params,
                           "schema_version": prepared.schema_version,
                           "operation": prepared.operation, "endpoint": prepared.endpoint},
                          sort_keys=True, separators=(",", ":")).encode())
    for key, (filename, data, ctype) in prepared.files.items():
        out.extend(key.encode() + b"\0" + filename.encode() + b"\0" + ctype.encode() + b"\0" + data)
    return bytes(out)


def payload_hash(prepared: PreparedPayload) -> str:
    return hashlib.sha256(_canonical_bytes(prepared)).hexdigest()


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def persist_payload(prepared: PreparedPayload, path, source_info=None) -> Path:
    """Persist manifest plus file bytes; credentials are never included."""
    path = Path(path)
    root = path.parent / (path.stem + ".files")
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, (slot, (filename, data, ctype)) in enumerate(prepared.files.items()):
        item_path = root / f"{index:02d}-{filename}"
        _atomic_write(item_path, data)
        entries.append({"slot": slot, "path": str(item_path), "filename": filename,
                        "mime": ctype, "content_type": ctype, "bytes": len(data),
                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                        "hash": hashlib.sha256(data).hexdigest()})
    preview = None
    if prepared.preview_bytes is not None:
        preview_path = root / "preview.png"
        _atomic_write(preview_path, prepared.preview_bytes)
        preview = {"path": str(preview_path), "size": len(prepared.preview_bytes),
                   "hash": hashlib.sha256(prepared.preview_bytes).hexdigest()}
    manifest = {"version": 1, "schema_version": prepared.schema_version,
                "operation": prepared.operation, "endpoint": prepared.endpoint,
                "fields": prepared.fields, "files": entries,
                "payloadSHA256": prepared.payload_sha256,
                "effective_params": prepared.effective_params,
                "input_fidelity": prepared.input_fidelity,
                "schema_hash": hashlib.sha256(prepared.schema_version.encode()).hexdigest(),
                "preview": preview, "coverage": prepared.coverage,
                "normalization_warnings": prepared.normalization_warnings,
                "source_info": source_info or {}}
    _atomic_write(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
    return path


def load_prepared_payload(path, expected_fields=None, expected_params=None) -> PreparedPayload:
    """Load and verify a prepared manifest; never reads source images or rebuilds bytes."""
    path = Path(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not manifest.get("schema_version"):
        raise ValueError("invalid prepared payload manifest")
    if expected_fields is not None and dict(expected_fields) != manifest.get("fields"):
        raise ValueError("prepared payload fields mismatch")
    if expected_params:
        for key, value in expected_params.items():
            if manifest.get("effective_params", {}).get(key) != value:
                raise ValueError(f"prepared payload parameter mismatch: {key}")
    files: OrderedDict[str, tuple[str, bytes, str]] = OrderedDict()
    for entry in manifest.get("files", []):
        item_path = Path(entry["path"])
        if not item_path.is_absolute():
            item_path = path.parent / item_path
        data = item_path.read_bytes()
        expected_bytes = entry.get("bytes", entry.get("size"))
        expected_sha = entry.get("sha256", entry.get("hash"))
        if len(data) != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha:
            raise ValueError(f"prepared payload file mismatch: {entry.get('slot')}")
        files[entry["slot"]] = (entry["filename"], data,
                                  entry.get("mime", entry.get("content_type", "application/octet-stream")))
    result = PreparedPayload(dict(manifest["fields"]), files, None,
                             manifest.get("coverage"), manifest.get("normalization_warnings", []),
                             manifest.get("input_fidelity", ""), manifest.get("effective_params", {}),
                             manifest["schema_version"], operation=manifest["operation"],
                             endpoint=manifest["endpoint"])
    if result.payload_sha256 != manifest.get("payloadSHA256"):
        raise ValueError("prepared payload hash mismatch")
    return result
