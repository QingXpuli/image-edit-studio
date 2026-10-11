"""Offline execution contract tests: prepared payload, review SHA, and owner lock."""
from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "compose/images"))
sys.path.insert(0, str(ROOT / "zcode-image-edit"))
import image_payload as P
import reliability as R


def image_bytes(color=(50, 80, 120)):
    b = io.BytesIO(); Image.new("RGB", (32, 32), color).save(b, "PNG"); return b.getvalue()


def lock_worker(root, fingerprint, output):
    try:
        lock = R.acquire_submission_lock(Path(root), fingerprint, job_id=os.getpid().__str__())
        Path(output).write_text("owner", encoding="utf-8")
        # Keep the owner lock persistent as if a POST has become submitted.
        lock.mark("submitted")
    except Exception as exc:
        Path(output).write_text("blocked:" + type(exc).__name__, encoding="utf-8")


class ExecutionContract(unittest.TestCase):
    def test_prepared_mask_primary_roundtrip_preserves_refs(self):
        source = Image.open(io.BytesIO(image_bytes())).convert("RGB")
        ref = Image.open(io.BytesIO(image_bytes((150, 40, 80)))).convert("RGB")
        mask = Image.new("RGBA", source.size, (255, 255, 255, 0)); mask.paste((255, 255, 255, 255), (8, 8, 24, 24))
        prepared = P.prepare_payload(source, [ref], "edit", "1024x1024", "crop", "low", "gpt-image-2",
                                     mask=mask, primary=True, encode="png")
        self.assertEqual(list(prepared.files), ["image", "mask", "image[1]"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            P.persist_payload(prepared, path, source_info={"role": "identity"})
            loaded = P.load_prepared_payload(path)
            self.assertEqual(loaded.payload_sha256, prepared.payload_sha256)
            self.assertEqual(list(loaded.files), list(prepared.files))
            self.assertEqual(loaded.files["image[1]"][1], prepared.files["image[1]"][1])

    def test_review_binds_output_sha_and_required_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cache = root / "cache"; source = root / "result.png"
            source.write_bytes(image_bytes())
            fp = R.request_fingerprint(source=source, mask=None, prompt="x", model="gpt-image-2",
                                       size="32x32", quality="low", pad="crop", mask_invert=False)
            R.store_cache(cache, fp, source, validation={"status": "NEEDS_REVIEW", "semantic": {"status": "PENDING"}})
            result_sha = R.sha256_file(source)
            data = R.update_review(cache, fp, verdict="pass", checks={"requested_change": "pass"},
                                   required=["requested_change"], output_sha256=result_sha, technical_status="PASS")
            self.assertEqual(data["validation"]["status"], "PASS")
            self.assertEqual(data["review"]["output_sha256"], result_sha)
            with self.assertRaises(ValueError):
                R.update_review(cache, fp, verdict="pass", checks={}, required=["requested_change"],
                                output_sha256="bad", technical_status="PASS")

    def test_same_fingerprint_two_processes_one_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"; root.mkdir()
            fp = hashlib.sha256(b"same-request").hexdigest()
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            ctx = multiprocessing.get_context("spawn")
            a = ctx.Process(target=lock_worker, args=(str(root), fp, str(first)))
            b = ctx.Process(target=lock_worker, args=(str(root), fp, str(second)))
            a.start(); a.join(10); b.start(); b.join(10)
            values = [p.read_text(encoding="utf-8") for p in (first, second) if p.exists()]
            self.assertEqual(values.count("owner"), 1)
            self.assertEqual(sum(v.startswith("blocked:") for v in values), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
