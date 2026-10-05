"""Offline checks for red-marker compositing. No network."""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mask_edit_app import encode_jpeg, mask_to_binary, paint_red, parse_multipart, resize_pair


def assert_eq(a, b, msg):
    if a != b:
        raise SystemExit(f"FAIL {msg}: {a!r} != {b!r}")


def test_rgba_red_stroke_becomes_pure_red():
    img = Image.new("RGB", (8, 8), (10, 20, 30))
    mask = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    mask.putpixel((2, 3), (255, 0, 0, 255))
    out = paint_red(img, mask, invert=False)
    assert_eq(out.getpixel((2, 3)), (255, 0, 0), "painted pixel")
    assert_eq(out.getpixel((0, 0)), (10, 20, 30), "protected pixel")
    binary = mask_to_binary(mask, invert=False)
    assert_eq(binary.getpixel((2, 3)), 255, "alpha counted")
    assert_eq(binary.getpixel((0, 0)), 0, "empty alpha")


def test_gray_convert_would_have_failed():
    mask = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    gray = mask.convert("L").getpixel((0, 0))
    if gray >= 128:
        raise SystemExit(f"unexpected: red L={gray} would pass a 128 threshold")
    binary = mask_to_binary(mask, invert=False)
    assert_eq(binary.getpixel((0, 0)), 255, "alpha path ignores red luminance")


def test_resize_keeps_alpha_stroke():
    img = Image.new("RGB", (10, 10), (1, 2, 3))
    mask = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    mask.putpixel((1, 1), (255, 0, 0, 255))
    im2, m2 = resize_pair(img, mask, (20, 20), "stretch")
    out = paint_red(im2, m2, invert=False)
    raw = out.tobytes()  # getdata() 在 Pillow 14 起移除；按字节扫描 RGB 三元组
    reds = sum(1 for i in range(0, len(raw), 3) if raw[i:i + 3] == b"\xff\x00\x00")
    if reds < 1:
        raise SystemExit("FAIL stretch resize dropped the stroke")


def test_empty_mask_extrema():
    mask = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    binary = mask_to_binary(mask, invert=False)
    assert_eq(binary.getextrema(), (0, 0), "empty mask")


def test_jpeg_under_cap():
    img = Image.new("RGB", (1024, 1024), (40, 80, 120))
    data, q = encode_jpeg(img)
    if len(data) > 1_900_000:
        raise SystemExit(f"FAIL jpeg too big {len(data)} q={q}")


def test_multipart_roundtrip():
    body = (
        b"------B\r\n"
        b'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        b"hello\r\n"
        b"------B\r\n"
        b'Content-Disposition: form-data; name="image"; filename="a.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        b"PNGDATA\r\n"
        b"------B--\r\n"
    )
    fields, files = parse_multipart("multipart/form-data; boundary=----B", body)
    assert_eq(fields.get("prompt"), "hello", "prompt field")
    assert_eq(files[0]["data"], b"PNGDATA", "file body")


if __name__ == "__main__":
    test_rgba_red_stroke_becomes_pure_red()
    test_gray_convert_would_have_failed()
    test_resize_keeps_alpha_stroke()
    test_empty_mask_extrema()
    test_jpeg_under_cap()
    test_multipart_roundtrip()
    print("ok")
