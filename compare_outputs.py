#!/usr/bin/env python3
"""Combine ride poster and weather dashboard PNGs side-by-side for comparison."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = "#F5F7FB"
INK = "#14213D"
SUBTLE = "#6E7C93"
GAP_PX = 28
PAD_PX = 24
LABEL_BAR_PX = 52


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_image(path: Path) -> Image.Image:
    if not path.is_file():
        raise FileNotFoundError(f"Image not found: {path}")
    return Image.open(path).convert("RGB")


def _match_height(left: Image.Image, right: Image.Image) -> tuple[Image.Image, Image.Image]:
    target = max(left.height, right.height)
    if left.height == right.height:
        return left, right

    def resize_to_height(img: Image.Image) -> Image.Image:
        if img.height == target:
            return img
        scale = target / img.height
        new_w = max(1, int(round(img.width * scale)))
        return img.resize((new_w, target), Image.Resampling.LANCZOS)

    return resize_to_height(left), resize_to_height(right)


def combine_images(
    poster: Image.Image,
    weather: Image.Image,
    *,
    poster_label: str = "Ride poster",
    weather_label: str = "Weather dashboard",
    show_labels: bool = True,
) -> Image.Image:
    poster, weather = _match_height(poster, weather)
    content_w = poster.width + GAP_PX + weather.width
    content_h = poster.height
    label_bar = LABEL_BAR_PX if show_labels else 0

    canvas = Image.new(
        "RGB",
        (PAD_PX * 2 + content_w, PAD_PX * 2 + label_bar + content_h),
        BG,
    )
    draw = ImageDraw.Draw(canvas)

    if show_labels:
        title_font = _font(22)
        draw.text((PAD_PX, PAD_PX + 6), poster_label, fill=INK, font=title_font)
        draw.text(
            (PAD_PX + poster.width + GAP_PX, PAD_PX + 6),
            weather_label,
            fill=INK,
            font=title_font,
        )
        draw.line(
            [(PAD_PX, PAD_PX + label_bar - 8), (canvas.width - PAD_PX, PAD_PX + label_bar - 8)],
            fill="#E2E8F2",
            width=2,
        )

    y = PAD_PX + label_bar
    canvas.paste(poster, (PAD_PX, y))
    canvas.paste(weather, (PAD_PX + poster.width + GAP_PX, y))
    return canvas


def _run_generator(cmd: list[str], cwd: Path) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Side-by-side compare ride_poster and weather_dashboard outputs.",
    )
    p.add_argument("--poster", type=Path, default=Path("ride_test.png"), help="Ride poster PNG")
    p.add_argument("--weather", type=Path, default=Path("weather_test.png"), help="Weather dashboard PNG")
    p.add_argument("-o", "--output", type=Path, default=Path("compare.png"), help="Combined PNG path")
    p.add_argument("--no-labels", action="store_true", help="Hide column titles")
    p.add_argument(
        "--gpx",
        type=Path,
        default=None,
        help="Regenerate both images from GPX before combining",
    )
    p.add_argument("--config", type=Path, default=None, help="Poster JSON config (with --gpx)")
    p.add_argument("-s", "--speed", type=float, default=30.0, help="Speed km/h for weather (with --gpx)")
    p.add_argument("-d", "--date", default="13.06.2026", help="Ride date DD.MM.YYYY (with --gpx)")
    p.add_argument("-t", "--time", default="13:00", help="Ride time HH:MM (with --gpx)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    root = Path(__file__).resolve().parent

    poster_path = args.poster if args.poster.is_absolute() else root / args.poster
    weather_path = args.weather if args.weather.is_absolute() else root / args.weather
    output_path = args.output if args.output.is_absolute() else root / args.output

    if args.gpx is not None:
        gpx = args.gpx if args.gpx.is_absolute() else root / args.gpx
        config = args.config
        if config is None:
            sibling = gpx.with_suffix(".json")
            poster_json = gpx.parent / f"{gpx.stem}_poster.json"
            config = poster_json if poster_json.is_file() else (sibling if sibling.is_file() else None)
        poster_cmd = [sys.executable, "ride_poster.py", "--gpx", str(gpx), "--out", str(poster_path)]
        if config is not None:
            cfg = config if config.is_absolute() else root / config
            poster_cmd.extend(["--config", str(cfg)])
        weather_cmd = [
            sys.executable,
            "weather_dashboard.py",
            str(gpx),
            "-o",
            str(weather_path),
            "-s",
            str(args.speed),
            "-d",
            args.date,
            "-t",
            args.time,
        ]
        _run_generator(poster_cmd, root)
        _run_generator(weather_cmd, root)

    combined = combine_images(
        _load_image(poster_path),
        _load_image(weather_path),
        show_labels=not args.no_labels,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined.save(output_path, format="PNG", optimize=True)
    print(f"Comparison saved to {output_path} ({combined.width}×{combined.height})")


if __name__ == "__main__":
    main()
