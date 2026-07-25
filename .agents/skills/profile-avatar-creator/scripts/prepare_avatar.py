#!/usr/bin/env python3
"""Remove chroma key, normalize, and validate an Unnest profile avatar."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

SIZE = 512
COVERAGE = {
    "character": (0.04, 0.55),
    "scene": (0.35, 0.85),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=COVERAGE, required=True)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def chroma_helper() -> Path:
    codex_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    helper = codex_root / "skills/.system/imagegen/scripts/remove_chroma_key.py"
    if not helper.is_file():
        msg = f"imagegen chroma-key helper not found: {helper}"
        raise FileNotFoundError(msg)
    return helper


def remove_chroma(source: Path, output: Path) -> None:
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(chroma_helper()),
            "--input",
            str(source),
            "--out",
            str(output),
            "--auto-key",
            "border",
            "--soft-matte",
            "--transparent-threshold",
            "12",
            "--opaque-threshold",
            "220",
            "--despill",
            "--force",
        ],
        check=True,
    )


def validate(image: Image.Image, kind: str) -> tuple[float, tuple[int, int, int, int]]:
    if image.size != (SIZE, SIZE) or image.mode != "RGBA":
        msg = f"expected {SIZE}x{SIZE} RGBA, got {image.size} {image.mode}"
        raise ValueError(msg)

    alpha = image.getchannel("A")
    corners = ((0, 0), (SIZE - 1, 0), (0, SIZE - 1), (SIZE - 1, SIZE - 1))
    if any(alpha.getpixel(point) != 0 for point in corners):
        msg = "all four corners must be fully transparent"
        raise ValueError(msg)

    bounds = alpha.getbbox()
    if bounds is None:
        msg = "avatar is fully transparent"
        raise ValueError(msg)

    visible = sum(value > 0 for value in alpha.getdata())
    coverage = visible / (SIZE * SIZE)
    minimum, maximum = COVERAGE[kind]
    if not minimum <= coverage <= maximum:
        msg = f"{kind} alpha coverage {coverage:.1%} is outside {minimum:.0%}-{maximum:.0%}"
        raise ValueError(msg)
    return coverage, bounds


def main() -> None:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if args.output.exists() and not args.force:
        msg = f"output exists; pass --force to replace it: {args.output}"
        raise FileExistsError(msg)

    with Image.open(args.input) as source:
        if source.width != source.height:
            msg = f"input must be square, got {source.size}"
            raise ValueError(msg)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="profile-avatar-") as temp_dir:
        keyed = Path(temp_dir) / "transparent.png"
        remove_chroma(args.input, keyed)
        with Image.open(keyed) as prepared:
            avatar = prepared.convert("RGBA").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
            coverage, bounds = validate(avatar, args.kind)
            avatar.save(args.output, optimize=True)

    print(f"Wrote {args.output} ({args.kind}, coverage={coverage:.1%}, bounds={bounds})")  # noqa: T201


if __name__ == "__main__":
    main()
