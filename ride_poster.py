from __future__ import annotations

import argparse
import json
import math
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")

import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.patches import Circle, FancyBboxPatch, Wedge
from matplotlib.transforms import Affine2D


# Flat / material-ish palette.
BG = "#F5F7FB"
CARD = "#FFFFFF"
INK = "#14213D"
SUBTLE = "#6E7C93"
BORDER = "#E2E8F2"
PRIMARY = "#2F6BFF"
PRIMARY_SOFT = "#E8F0FF"
SECONDARY = "#1DB9AA"
SECONDARY_SOFT = "#E7FBF7"
ACCENT = "#FF6A55"
ACCENT_SOFT = "#FFF1ED"
MUTED = "#A8B6CA"
PROFILE_FILL = "#DDE7FA"
SHADOW = (0, 0, 0, 0.07)

# Reference canvas is 10 x 12.5 inches; fonts/linewidths scale with the actual canvas.
_REF_AREA_IN2 = 10.0 * 12.5
_SCALE = 1.0


def fs(v: float) -> float:
    return v * _SCALE


def lw(v: float) -> float:
    return v * _SCALE


@dataclass
class RouteData:
    lat: np.ndarray
    lon: np.ndarray
    ele: np.ndarray
    dist_km: np.ndarray
    # Total ascent computed from the raw GPX elevations before resampling and
    # smoothing flatten the profile; None when constructed from derived data.
    gain_m: float | None = None


@dataclass
class ClimbInfo:
    start_km: float
    end_km: float
    length_km: float
    gain_m: float
    avg_grade: float
    max_grade: float
    label: str


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    r = 6371.0088
    lat1_r = np.radians(lat1)
    lon1_r = np.radians(lon1)
    lat2_r = np.radians(lat2)
    lon2_r = np.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2.0) ** 2
    return 2.0 * r * np.arcsin(np.sqrt(a))


def cumulative_distance_km(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    if len(lat) < 2:
        return np.zeros(len(lat))
    seg = haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:])
    return np.concatenate([[0.0], np.cumsum(seg)])


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return values.copy()
    window = min(window, len(values))
    pad = window // 2
    padded = np.pad(values, (pad, pad), mode="edge")
    kernel = np.ones(window) / window
    conv = np.convolve(padded, kernel, mode="valid")
    return conv[: len(values)]


def parse_gpx(gpx_path: Path) -> RouteData:
    tree = ET.parse(gpx_path)
    root = tree.getroot()
    pts: list[tuple[float, float, float]] = []
    for tag in ["trkpt", "rtept"]:
        for point in root.iterfind(f".//{{*}}{tag}"):
            lat = float(point.attrib["lat"])
            lon = float(point.attrib["lon"])
            ele_el = point.find("{*}ele")
            ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
            pts.append((lat, lon, ele))
        if pts:
            break

    if len(pts) < 2:
        raise ValueError(f"No usable route points found in {gpx_path}")

    lat = np.array([p[0] for p in pts], dtype=float)
    lon = np.array([p[1] for p in pts], dtype=float)
    ele = np.array([p[2] for p in pts], dtype=float)
    dist_km = cumulative_distance_km(lat, lon)
    gain_m = hysteresis_gain_m(ele)
    return resample_route(RouteData(lat=lat, lon=lon, ele=ele, dist_km=dist_km, gain_m=gain_m), step_m=100.0)


def resample_route(route: RouteData, step_m: float = 100.0) -> RouteData:
    total_m = float(route.dist_km[-1] * 1000.0)
    if total_m <= step_m or len(route.dist_km) < 3:
        return route
    new_dist_m = np.arange(0.0, total_m + step_m, step_m)
    old_dist_m = route.dist_km * 1000.0
    lat = np.interp(new_dist_m, old_dist_m, route.lat)
    lon = np.interp(new_dist_m, old_dist_m, route.lon)
    ele = np.interp(new_dist_m, old_dist_m, route.ele)
    ele = moving_average(ele, 7)
    return RouteData(lat=lat, lon=lon, ele=ele, dist_km=new_dist_m / 1000.0, gain_m=route.gain_m)


def total_gain_m(ele: np.ndarray) -> float:
    diff = np.diff(ele)
    return float(np.sum(diff[diff > 0.0]))


def hysteresis_gain_m(ele: np.ndarray, threshold_m: float = 2.0) -> float:
    """Total ascent with a small hysteresis so GPS noise does not inflate it."""
    gain = 0.0
    ref = float(ele[0])
    for v in ele[1:]:
        v = float(v)
        if v - ref >= threshold_m:
            gain += v - ref
            ref = v
        elif v < ref:
            ref = v
    return gain


def grade_percent(route: RouteData, window_m: float = 250.0) -> np.ndarray:
    d_m = route.dist_km * 1000.0
    grades = np.zeros(len(d_m))
    for i in range(len(d_m)):
        start_d = d_m[i] - window_m / 2.0
        end_d = d_m[i] + window_m / 2.0
        start_idx = max(0, int(np.searchsorted(d_m, start_d, side="left")))
        end_idx = min(len(d_m) - 1, int(np.searchsorted(d_m, end_d, side="right") - 1))
        dd = d_m[end_idx] - d_m[start_idx]
        if dd > 10.0:
            grades[i] = 100.0 * (route.ele[end_idx] - route.ele[start_idx]) / dd
    return moving_average(grades, 5)


def summarize_climb(
    dist_km: np.ndarray,
    ele_m: np.ndarray,
    local_grade: np.ndarray,
    start_idx: int,
    end_idx: int,
) -> ClimbInfo | None:
    start_idx = max(0, start_idx)
    end_idx = min(len(dist_km) - 1, end_idx)
    if end_idx - start_idx < 2:
        return None

    local_slice = slice(start_idx, end_idx + 1)
    rel_start = int(np.argmin(ele_m[local_slice]))
    rel_end = int(np.argmax(ele_m[start_idx + rel_start : end_idx + 1]))
    s = start_idx + rel_start
    e = start_idx + rel_start + rel_end
    if e <= s:
        return None

    length = float(dist_km[e] - dist_km[s])
    gain = float(ele_m[e] - ele_m[s])
    if length <= 0.2 or gain <= 20.0:
        return None
    mask = slice(s, e + 1)
    max_grade = float(np.max(local_grade[mask])) if e > s else 0.0
    avg_grade = gain / (length * 10.0)
    return ClimbInfo(
        start_km=float(dist_km[s]),
        end_km=float(dist_km[e]),
        length_km=length,
        gain_m=gain,
        avg_grade=avg_grade,
        max_grade=max_grade,
        label="",
    )


def detect_major_climbs(route: RouteData, names: Iterable[str] | None = None) -> list[ClimbInfo]:
    d = route.dist_km
    e = moving_average(route.ele, 9)
    local_grade = grade_percent(RouteData(route.lat, route.lon, e, d), window_m=300.0)

    candidates: list[ClimbInfo] = []
    start_idx: int | None = None
    for i, g in enumerate(local_grade):
        if start_idx is None and g >= 1.7:
            start_idx = i
        elif start_idx is not None and g < 0.8:
            climb = summarize_climb(d, e, local_grade, start_idx, i)
            if climb is not None:
                candidates.append(climb)
            start_idx = None
    if start_idx is not None:
        climb = summarize_climb(d, e, local_grade, start_idx, len(d) - 1)
        if climb is not None:
            candidates.append(climb)

    merged: list[ClimbInfo] = []
    for climb in candidates:
        if not merged:
            merged.append(climb)
            continue
        prev = merged[-1]
        if climb.start_km - prev.end_km < 1.0 and (climb.avg_grade > 2.0 or prev.avg_grade > 2.0):
            merged[-1] = summarize_climb(
                d,
                e,
                local_grade,
                int(np.searchsorted(d, prev.start_km, side="left")),
                int(np.searchsorted(d, climb.end_km, side="right") - 1),
            ) or prev
        else:
            merged.append(climb)

    major = [c for c in merged if c.length_km >= 1.2 and c.gain_m >= 55.0 and c.avg_grade >= 2.0]
    major.sort(key=lambda c: (c.gain_m * c.avg_grade, c.length_km), reverse=True)
    major = major[:4]
    major.sort(key=lambda c: c.start_km)

    name_list = list(names or [])
    for i, climb in enumerate(major, start=1):
        if i <= len(name_list) and name_list[i - 1].strip():
            climb.label = name_list[i - 1]
        else:
            climb.label = f"Climb {i}"
    return major


def fmt_km(v: float) -> str:
    return f"{v:.0f} km" if abs(v - round(v)) < 0.05 else f"{v:.1f} km"


def fmt_m(v: float) -> str:
    return f"{int(round(v)):,} m".replace(",", " ")


def normalize_pace(value: float | int | None) -> float:
    if value is None:
        return 2.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 2.0
    v = max(1.0, min(3.0, v))
    return round(v * 2.0) / 2.0


def default_config() -> dict:
    return {
        "title": "GROUP ROAD RIDE",
        "subtitle": "Tempo / endurance loop",
        "date": "Sat 18 Apr 2026",
        "time": "09:00",
        "start_label": "START",
        "start": "Ada Ciganlija, Belgrade",
        "route_name": "Avala / Kosmaj loop",
        "notes": "Steady rollout · regroup on major climbs · coffee stop if needed",
        "pace": 2.0,
        "distance_km": None,
        "elevation_m": None,
        "climb_names": ["Climb 1", "Climb 2", "Climb 3", "Climb 4"],
        "output_width_px": 1600,
        "output_height_px": 2000,
        "dpi": 160,
    }


def load_config(config_path: Path | None) -> dict:
    config = default_config()
    if config_path is None:
        return config
    user_cfg = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(user_cfg)
    return config


# ---------------------------------------------------------------------------
# Measurement-based text fitting: every label is measured with the renderer and
# shrunk/wrapped until it fits its box, so nothing overflows whatever the data.
# ---------------------------------------------------------------------------


def _renderer(fig):
    return fig.canvas.get_renderer()


def _fit_wrapped(fig, t, raw, max_w_px: float, max_h_px: float | None, max_lines: int, min_fontsize: float):
    raw = " ".join(str(raw).split())
    if not raw:
        t.set_text("")
        return t
    r = _renderer(fig)
    size = float(t.get_fontsize())
    while True:
        t.set_fontsize(size)
        t.set_text(raw)
        bb = t.get_window_extent(renderer=r)
        if bb.width <= max_w_px and (max_h_px is None or bb.height <= max_h_px):
            return t
        if max_lines > 1 and bb.width > max_w_px:
            est = max(4, int(len(raw) * max_w_px / bb.width))
            while est >= 4:
                t.set_text(textwrap.fill(raw, est, break_long_words=False))
                if t.get_window_extent(renderer=r).width <= max_w_px:
                    break
                est -= 2
            bb = t.get_window_extent(renderer=r)
            lines = t.get_text().count("\n") + 1
            if lines <= max_lines and bb.width <= max_w_px and (max_h_px is None or bb.height <= max_h_px):
                return t
        if size <= min_fontsize:
            break
        size = max(min_fontsize, size - 0.6)
    # Last resort at minimum size: keep the first max_lines lines, add an ellipsis.
    lines = t.get_text().split("\n")[:max_lines]
    if lines and (t.get_text().count("\n") + 1) > max_lines:
        lines[-1] = lines[-1].rstrip(" ,·;") + "…"
    t.set_text("\n".join(lines))
    return t


def fig_fit_text(
    fig,
    x: float,
    y: float,
    raw,
    *,
    fontsize: float,
    max_w: float,
    max_h: float | None = None,
    max_lines: int = 1,
    min_fontsize: float = 6.0,
    **kwargs,
):
    t = fig.text(x, y, "", fontsize=fontsize, **kwargs)
    w_px = fig.get_figwidth() * fig.dpi
    h_px = fig.get_figheight() * fig.dpi
    return _fit_wrapped(fig, t, raw, max_w * w_px, max_h * h_px if max_h is not None else None, max_lines, min_fontsize)


def ax_fit_text(
    ax,
    x: float,
    y: float,
    raw,
    *,
    fontsize: float,
    max_w: float,
    max_h: float | None = None,
    max_lines: int = 1,
    min_fontsize: float = 6.0,
    **kwargs,
):
    fig = ax.figure
    pos = ax.get_position()
    t = ax.text(x, y, "", fontsize=fontsize, transform=ax.transAxes, **kwargs)
    w_px = pos.width * fig.get_figwidth() * fig.dpi
    h_px = pos.height * fig.get_figheight() * fig.dpi
    return _fit_wrapped(fig, t, raw, max_w * w_px, max_h * h_px if max_h is not None else None, max_lines, min_fontsize)


def text_h_fig(fig, t) -> float:
    return t.get_window_extent(renderer=_renderer(fig)).height / (fig.get_figheight() * fig.dpi)


def text_w_fig(fig, t) -> float:
    return t.get_window_extent(renderer=_renderer(fig)).width / (fig.get_figwidth() * fig.dpi)


# Circles drawn in figure/axes fractions come out as ellipses when the canvas is
# not square; these transforms keep them truly round (radius in x-fraction units).


def _fig_circle_tr(fig) -> tuple[Affine2D, float]:
    a = fig.get_figwidth() / fig.get_figheight()
    return Affine2D().scale(1.0, a) + fig.transFigure, a


def fig_circle(fig, cx: float, cy: float, r: float, **kwargs) -> Circle:
    tr, a = _fig_circle_tr(fig)
    return Circle((cx, cy / a), r, transform=tr, **kwargs)


def ax_circle(ax, cx: float, cy: float, r: float, **kwargs) -> Circle:
    fig = ax.figure
    pos = ax.get_position()
    a = (pos.width * fig.get_figwidth()) / (pos.height * fig.get_figheight())
    tr = Affine2D().scale(1.0, a) + ax.transAxes
    return Circle((cx, cy / a), r, transform=tr, **kwargs)


# ---------------------------------------------------------------------------
# Poster building blocks
# ---------------------------------------------------------------------------


def rounded_panel(
    fig,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str | None = None,
    subtitle: str | None = None,
    face: str = CARD,
    footer_ratio: float = 0.08,
) -> tuple[plt.Axes, tuple[float, float, float, float]]:
    shadow = FancyBboxPatch(
        (x + 0.006, y - 0.006),
        w,
        h,
        boxstyle="round,pad=0.008,rounding_size=0.024",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=SHADOW,
        zorder=1,
    )
    panel = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.008,rounding_size=0.024",
        transform=fig.transFigure,
        linewidth=lw(1.0),
        edgecolor=BORDER,
        facecolor=face,
        zorder=2,
    )
    fig.patches.extend([shadow, panel])

    tx = x + 0.04 * w
    cursor = y + h - 0.016
    if title:
        t = fig_fit_text(
            fig, tx, cursor, title.upper(), fontsize=fs(12), max_w=0.92 * w,
            fontweight="bold", color=SUBTLE, va="top", zorder=4,
        )
        cursor -= text_h_fig(fig, t) + 0.006
    if subtitle:
        st = fig_fit_text(
            fig, tx, cursor, subtitle, fontsize=fs(10), max_w=0.92 * w,
            max_lines=3 if w > 0.5 else 2, color=SUBTLE, va="top", zorder=4, linespacing=1.18,
        )
        cursor -= text_h_fig(fig, st) + 0.004
    bottom = y + footer_ratio * h
    ax = fig.add_axes([tx, bottom, w * 0.92, max(0.02, cursor - 0.008 - bottom)], zorder=3)
    return ax, (x, y, w, h)


# Outward padding of the hero chip backgrounds; the chip stack layout must
# account for it, otherwise the translucent boxes visually overlap.
CHIP_PAD = 0.0045


def add_info_chip(fig, x: float, y: float, w: float, h: float, label: str, value: str) -> None:
    chip = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad={CHIP_PAD},rounding_size=0.016",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=(1, 1, 1, 0.15),
        zorder=4,
    )
    fig.patches.append(chip)
    fig.text(x + 0.020, y + 0.73 * h, label.upper(), fontsize=fs(9.3), color=(1, 1, 1, 0.86), va="center", zorder=5)
    fig_fit_text(
        fig, x + 0.020, y + 0.27 * h, value, fontsize=fs(15.5), max_w=w - 0.040,
        color=CARD, fontweight="bold", va="center", zorder=5,
    )


def draw_pace_dots_figure(fig, x_right: float, cy: float, pace: float) -> float:
    """Draw three pace dots right-aligned at x_right; return leftmost x extent."""
    pace = normalize_pace(pace)
    full = int(math.floor(pace))
    half = 1 if abs(pace - full - 0.5) < 0.1 else 0
    radius = 0.0082
    gap = 0.0195
    tr, a = _fig_circle_tr(fig)
    centers = [x_right - radius - (2 - i) * gap for i in range(3)]
    for i, cx in enumerate(centers):
        c = (cx, cy / a)
        base = Circle(c, radius, transform=tr, facecolor=(1, 1, 1, 0.22), edgecolor=(1, 1, 1, 0.35), linewidth=lw(1.0), zorder=6)
        fig.patches.append(base)
        if i < full:
            fig.patches.append(Circle(c, radius * 0.98, transform=tr, facecolor=CARD, edgecolor="none", zorder=7))
        elif i == full and half:
            fig.patches.append(Wedge(c, radius * 0.98, 90, 270, transform=tr, facecolor=CARD, edgecolor="none", zorder=7))
    return centers[0] - radius


def add_pace_chip(fig, x: float, y: float, w: float, h: float, pace: float) -> None:
    chip = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad={CHIP_PAD},rounding_size=0.016",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=(1, 1, 1, 0.15),
        zorder=4,
    )
    fig.patches.append(chip)
    fig.text(x + 0.020, y + 0.73 * h, "PACE", fontsize=fs(9.3), color=(1, 1, 1, 0.86), va="center", zorder=5)
    dots_left = draw_pace_dots_figure(fig, x + w - 0.018, y + 0.30 * h, pace)
    fig_fit_text(
        fig, x + 0.020, y + 0.27 * h, f"{normalize_pace(pace):.1f}/3",
        fontsize=fs(14.5), max_w=max(0.02, dots_left - 0.008 - (x + 0.020)),
        color=CARD, fontweight="bold", va="center", zorder=5,
    )


def draw_stat_card(
    fig,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    value: str,
    accent: str,
    soft: str,
    value_fontsize: float = 21.0,
    max_value_lines: int = 1,
) -> None:
    shadow = FancyBboxPatch(
        (x + 0.004, y - 0.004),
        w,
        h,
        boxstyle="round,pad=0.006,rounding_size=0.022",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=SHADOW,
        zorder=5,
    )
    card = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.006,rounding_size=0.022",
        transform=fig.transFigure,
        linewidth=lw(1.0),
        edgecolor=BORDER,
        facecolor=CARD,
        zorder=6,
    )
    pill_w = 0.026
    pill_h = 0.55 * h
    pill_x = x + 0.014
    pill_y = y + 0.225 * h
    badge = FancyBboxPatch(
        (pill_x, pill_y),
        pill_w,
        pill_h,
        boxstyle="round,pad=0.004,rounding_size=0.012",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=soft,
        zorder=7,
    )
    fig.patches.extend([shadow, card, badge])
    fig.patches.append(fig_circle(fig, pill_x + pill_w / 2.0, pill_y + pill_h / 2.0, 0.0072, facecolor=accent, edgecolor="none", zorder=8))

    text_x = pill_x + pill_w + 0.014
    text_w = x + w - 0.016 - text_x
    fig.text(text_x, y + 0.68 * h, label.upper(), fontsize=fs(10), color=SUBTLE, va="center", zorder=8)
    fig_fit_text(
        fig, text_x, y + 0.32 * h, value,
        fontsize=value_fontsize, max_w=text_w, max_h=0.50 * h, max_lines=max_value_lines,
        fontweight="bold", color=INK, va="center", zorder=8, linespacing=1.12,
    )


def normalize_route_for_map(route: RouteData) -> tuple[np.ndarray, np.ndarray]:
    lat = route.lat
    lon = route.lon * math.cos(math.radians(float(np.mean(route.lat))))
    x = lon - np.min(lon)
    y = lat - np.min(lat)
    span_x = float(np.ptp(x)) or 1.0
    span_y = float(np.ptp(y)) or 1.0
    pad = 0.06
    usable = 1.0 - 2.0 * pad
    if span_x >= span_y:
        x = pad + usable * (x / span_x)
        extra = (span_x - span_y) / span_x
        y = pad + usable * (y / span_x + extra / 2.0)
    else:
        y = pad + usable * (y / span_y)
        extra = (span_y - span_x) / span_y
        x = pad + usable * (x / span_y + extra / 2.0)
    return x, y


def index_for_distance(route: RouteData, dist_km: float) -> int:
    return int(np.clip(np.searchsorted(route.dist_km, dist_km), 0, len(route.dist_km) - 1))


def gradient_colors(grades: np.ndarray) -> np.ndarray:
    colors = np.empty((len(grades), 4))
    flat = np.array([168 / 255, 181 / 255, 204 / 255, 1.0])
    rolling = np.array([29 / 255, 185 / 255, 170 / 255, 1.0])
    steep = np.array([1.0, 106 / 255, 85 / 255, 1.0])
    for i, g in enumerate(grades):
        if g < 2.5:
            colors[i] = flat
        elif g < 5.0:
            colors[i] = rolling
        else:
            colors[i] = steep
    return colors


def draw_map(ax: plt.Axes, route: RouteData, climbs: list[ClimbInfo]) -> None:
    x, y = normalize_route_for_map(route)
    ax.plot(x, y, color=PRIMARY_SOFT, linewidth=lw(14), solid_capstyle="round", zorder=1)
    ax.plot(x, y, color=PRIMARY, linewidth=lw(5.2), solid_capstyle="round", zorder=2)

    start_x, start_y = float(x[0]), float(y[0])
    finish_x, finish_y = float(x[-1]), float(y[-1])
    sf_dist = math.hypot(finish_x - start_x, finish_y - start_y)

    if sf_dist < 0.08:
        ax.add_patch(Circle((start_x, start_y), 0.034, facecolor=ACCENT, edgecolor=CARD, linewidth=lw(2.8), zorder=4))
        ax.add_patch(Circle((start_x, start_y), 0.020, facecolor=SECONDARY, edgecolor="none", zorder=5))
        label_dy = 0.072 if start_y < 0.14 else -0.072
        ax.text(
            start_x, start_y + label_dy, "S/F", ha="center", va="center",
            fontsize=fs(9.6), color=SUBTLE, fontweight="bold", zorder=6, clip_on=False,
            path_effects=[pe.withStroke(linewidth=lw(3.0), foreground=CARD)],
        )
    else:
        ax.add_patch(Circle((start_x, start_y), 0.030, facecolor=SECONDARY, edgecolor=CARD, linewidth=lw(2.5), zorder=4))
        ax.text(start_x, start_y, "S", ha="center", va="center", fontsize=fs(10), color=CARD, fontweight="bold", zorder=5)
        ax.add_patch(Circle((finish_x, finish_y), 0.028, facecolor=ACCENT, edgecolor=CARD, linewidth=lw(2.5), zorder=4))
        ax.text(finish_x, finish_y, "F", ha="center", va="center", fontsize=fs(10), color=CARD, fontweight="bold", zorder=5)

    for i, climb in enumerate(climbs, start=1):
        idx = index_for_distance(route, (climb.start_km + climb.end_km) / 2.0)
        cx, cy = float(x[idx]), float(y[idx])
        ax.add_patch(Circle((cx, cy), 0.028, facecolor=CARD, edgecolor=PRIMARY, linewidth=lw(2.0), zorder=6))
        ax.text(cx, cy, str(i), ha="center", va="center", fontsize=fs(10), color=PRIMARY, fontweight="bold", zorder=7)

    ax.set_aspect("equal")
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)
    ax.axis("off")


def nice_tick_step(total_km: float, max_ticks: int = 9) -> float:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200):
        if total_km / step <= max_ticks:
            return float(step)
    return float(max(1.0, round(total_km / max_ticks)))


def draw_profile(fig, ax: plt.Axes, rect: tuple[float, float, float, float], route: RouteData, climbs: list[ClimbInfo]) -> None:
    d = route.dist_km
    e = moving_average(route.ele, 7)
    grades = np.clip(grade_percent(RouteData(route.lat, route.lon, e, d), 250.0), 0.0, None)
    total = float(d[-1])
    min_e = float(np.min(e))
    max_e = float(np.max(e))
    rng_e = max(30.0, max_e - min_e)
    base_y = min_e - 0.07 * rng_e

    for climb in climbs:
        ax.axvspan(climb.start_km, climb.end_km, color=PRIMARY_SOFT, alpha=0.95, linewidth=0)

    ax.fill_between(d, base_y, e, color=PROFILE_FILL, zorder=1)
    points = np.array([d, e]).T.reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)
    lc = LineCollection(segments, colors=gradient_colors(grades[:-1]), linewidths=lw(3.4), capstyle="round", zorder=3)
    ax.add_collection(lc)
    ax.plot(d, e, color=INK, linewidth=lw(1.0), alpha=0.18, zorder=2)

    # Climb number badges live in a reserved headroom band above the profile, so
    # they never sit on the line; neighbours alternate height to avoid collisions.
    head = 0.30 * rng_e
    y_top = max_e + head
    y_hi = max_e + 0.66 * head
    y_lo = max_e + 0.26 * head
    placed: list[tuple[float, float]] = []
    for i, climb in enumerate(climbs, start=1):
        mid = (climb.start_km + climb.end_km) / 2.0
        y_lab = y_hi
        if placed and (mid - placed[-1][0]) < 0.08 * total and placed[-1][1] == y_hi:
            y_lab = y_lo
        placed.append((mid, y_lab))
        ax.text(
            mid,
            y_lab,
            str(i),
            ha="center",
            va="center",
            fontsize=fs(9.5),
            color=PRIMARY,
            fontweight="bold",
            bbox=dict(boxstyle="circle,pad=0.28", facecolor=CARD, edgecolor=PRIMARY, linewidth=lw(1.4)),
            zorder=5,
        )

    ax.set_xlim(0.0, total)
    ax.set_ylim(base_y, y_top)
    step = nice_tick_step(total)
    ticks = np.arange(0.0, total + step * 0.25, step)
    ticks = ticks[ticks <= total + 1e-9]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t:g}" for t in ticks], fontsize=fs(10), color=SUBTLE)
    ax.set_yticks([])
    ax.tick_params(axis="x", length=0, pad=lw(4.0))
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(axis="x", color=BORDER, linewidth=lw(1.0))

    # Legend and the km unit share the footer band of the panel (figure coords),
    # below the tick labels, so they cannot collide with the chart itself.
    rx, ry, rw, rh = rect
    pos = ax.get_position()
    footer_y = ry + 0.0135
    cx = pos.x0
    for label, color in (("< 2.5%", MUTED), ("2.5–5%", SECONDARY), ("5%+", ACCENT)):
        fig.text(cx, footer_y, "●", fontsize=fs(11), color=color, va="center", zorder=4)
        cx += 0.013
        t = fig.text(cx, footer_y, label, fontsize=fs(9.5), color=SUBTLE, va="center", zorder=4)
        cx += text_w_fig(fig, t) + 0.024
    fig.text(pos.x1, footer_y, "km", ha="right", va="center", fontsize=fs(10), color=SUBTLE, zorder=4)


def draw_climb_list(ax: plt.Axes, climbs: list[ClimbInfo]) -> None:
    ax.axis("off")
    if not climbs:
        ax_fit_text(
            ax, 0.0, 0.92, "No major climbs detected with the current thresholds.",
            fontsize=fs(11), max_w=1.0, max_lines=3, color=SUBTLE, va="top",
        )
        return

    n = min(4, len(climbs))
    gap = 0.035
    row_h = min(0.275, (1.0 - gap * (n - 1)) / n)
    left = 0.16
    right = 0.95
    for i, climb in enumerate(climbs[:n], start=1):
        top = 1.0 - i * row_h - (i - 1) * gap
        box = FancyBboxPatch(
            (0.012, top),
            0.976,
            row_h,
            boxstyle="round,pad=0.010,rounding_size=0.028",
            linewidth=lw(1.0),
            edgecolor=BORDER,
            facecolor=BG,
            transform=ax.transAxes,
        )
        ax.add_patch(box)

        name_cy = top + row_h * 0.74
        ax.add_patch(ax_circle(ax, 0.075, name_cy, 0.042, facecolor=PRIMARY, edgecolor="none"))
        ax.text(0.075, name_cy, str(i), ha="center", va="center", fontsize=fs(10.5), color=CARD, fontweight="bold", transform=ax.transAxes)

        max_txt = ax_fit_text(
            ax, right, top + row_h * 0.78, f"max {climb.max_grade:.0f}%",
            fontsize=fs(10.4), max_w=0.26, ha="right", va="center",
            color=PRIMARY, fontweight="bold",
        )
        ax_fit_text(
            ax, left, name_cy, climb.label,
            fontsize=fs(12.5), max_w=0.52, max_h=row_h * 0.46, max_lines=2,
            color=INK, fontweight="bold", va="center", linespacing=1.05,
        )

        ax_fit_text(
            ax, left, top + row_h * 0.38, f"{climb.length_km:.1f} km · {climb.avg_grade:.1f}%",
            fontsize=fs(10.5), max_w=0.50, color=SUBTLE, va="center",
        )
        ax_fit_text(
            ax, right, top + row_h * 0.38, f"+{int(round(climb.gain_m))} m",
            fontsize=fs(10.2), max_w=0.24, ha="right", va="center", color=SUBTLE,
        )
        ax_fit_text(
            ax, left, top + row_h * 0.15, f"{climb.start_km:.0f}–{climb.end_km:.0f} km",
            fontsize=fs(9.6), max_w=0.50, color=SUBTLE, va="center",
        )


def draw_hero(fig, config: dict, pace: float) -> None:
    hx, hy, hw, hh = 0.05, 0.805, 0.90, 0.145
    hero = FancyBboxPatch(
        (hx, hy),
        hw,
        hh,
        boxstyle="round,pad=0.01,rounding_size=0.032",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=PRIMARY,
        zorder=2,
    )
    fig.patches.append(hero)

    inner_left = hx + 0.030
    inner_bottom = hy + 0.014

    # Tag pill sized to its text.
    tag_h = 0.025
    tag_y = hy + hh - 0.012 - tag_h
    tag_label = fig.text(inner_left + 0.012, tag_y + tag_h / 2.0, "ROAD RIDE", fontsize=fs(9.8), color=CARD, fontweight="bold", va="center", zorder=4)
    tag_w = text_w_fig(fig, tag_label) + 0.024
    tag = FancyBboxPatch(
        (inner_left, tag_y),
        tag_w,
        tag_h,
        boxstyle="round,pad=0.004,rounding_size=0.012",
        transform=fig.transFigure,
        linewidth=0,
        facecolor=(1, 1, 1, 0.15),
        zorder=3,
    )
    fig.patches.append(tag)

    # Right-hand chip column: date / time / pace stacked inside the hero.
    # Layout works with the chips' VISUAL extents (rect + CHIP_PAD on each side)
    # so the translucent backgrounds keep a real gap between each other.
    chip_x, chip_w = 0.66, 0.26
    chip_margin, chip_gap = 0.012, 0.007
    vis_h = (hh - 2.0 * chip_margin - 2.0 * chip_gap) / 3.0
    chip_h = vis_h - 2.0 * CHIP_PAD
    chip_ys = [hy + hh - chip_margin - CHIP_PAD - chip_h - i * (vis_h + chip_gap) for i in range(3)]
    add_info_chip(fig, chip_x, chip_ys[0], chip_w, chip_h, "Date", str(config.get("date", "TBD")))
    add_info_chip(fig, chip_x, chip_ys[1], chip_w, chip_h, "Time", str(config.get("time", "TBD")))
    add_pace_chip(fig, chip_x, chip_ys[2], chip_w, chip_h, pace)

    # Title and subtitle flow top-down in the remaining left column.
    text_w = chip_x - 0.018 - inner_left
    title_top = tag_y - 0.016
    title = fig_fit_text(
        fig, inner_left, title_top, str(config.get("title", "GROUP ROAD RIDE")),
        fontsize=fs(26.5), max_w=text_w, max_h=max(0.02, title_top - inner_bottom - 0.016),
        max_lines=2, color=CARD, fontweight="bold", va="top", zorder=4, linespacing=1.04,
    )
    sub_top = title_top - text_h_fig(fig, title) - 0.009
    subtitle = config.get("subtitle", "")
    if subtitle:
        fig_fit_text(
            fig, inner_left, sub_top, subtitle,
            fontsize=fs(12.5), max_w=text_w, max_h=max(0.012, sub_top - inner_bottom),
            max_lines=2, color=(1, 1, 1, 0.88), va="top", zorder=4, linespacing=1.12,
        )


def render_poster(config: dict, route: RouteData, output_path: Path) -> None:
    global _SCALE

    distance_km = float(config["distance_km"]) if config.get("distance_km") is not None else float(route.dist_km[-1])
    if config.get("elevation_m") is not None:
        elevation_m = float(config["elevation_m"])
    elif route.gain_m is not None:
        elevation_m = route.gain_m
    else:
        elevation_m = total_gain_m(route.ele)
    climbs = detect_major_climbs(route, names=config.get("climb_names"))
    pace = normalize_pace(config.get("pace"))

    width_px = int(config.get("output_width_px", 1600))
    height_px = int(config.get("output_height_px", 2000))
    dpi = int(config.get("dpi", 160))
    fig = plt.figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    _SCALE = math.sqrt((width_px / dpi) * (height_px / dpi) / _REF_AREA_IN2)
    fig.patch.set_facecolor(BG)

    fig.patches.append(fig_circle(fig, 0.91, 0.92, 0.12, facecolor=PRIMARY_SOFT, edgecolor="none", zorder=0))
    fig.patches.append(fig_circle(fig, 0.09, 0.10, 0.09, facecolor=SECONDARY_SOFT, edgecolor="none", zorder=0))

    draw_hero(fig, config, pace)

    cards_y, cards_h = 0.700, 0.085
    draw_stat_card(fig, 0.050, cards_y, 0.210, cards_h, "Distance", fmt_km(distance_km), PRIMARY, PRIMARY_SOFT)
    draw_stat_card(fig, 0.284, cards_y, 0.210, cards_h, "Elevation", fmt_m(elevation_m), SECONDARY, SECONDARY_SOFT)
    draw_stat_card(
        fig,
        0.518,
        cards_y,
        0.432,
        cards_h,
        str(config.get("start_label", "START")),
        str(config.get("start", "Set in config")),
        ACCENT,
        ACCENT_SOFT,
        value_fontsize=fs(15.5),
        max_value_lines=3,
    )

    map_ax, _ = rounded_panel(fig, 0.05, 0.39, 0.54, 0.275, "Route overview", config.get("route_name", ""))
    climbs_ax, _ = rounded_panel(fig, 0.61, 0.39, 0.34, 0.275, "Main climbs", None)
    profile_ax, profile_rect = rounded_panel(fig, 0.05, 0.08, 0.90, 0.27, "Elevation profile", config.get("notes", ""), footer_ratio=0.15)

    draw_map(map_ax, route, climbs)
    draw_climb_list(climbs_ax, climbs)
    draw_profile(fig, profile_ax, profile_rect, route, climbs)

    fig_fit_text(
        fig,
        0.05,
        0.035,
        "Generated from GPX route data. Date, time, start text and pace are editable fields.",
        fontsize=fs(9.6),
        max_w=0.70,
        color=SUBTLE,
    )
    fig.text(0.95, 0.035, "flat / material", ha="right", fontsize=fs(9.6), color=MUTED)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate a flat/material-style poster for a group road ride.")
    p.add_argument("--config", type=Path, default=None, help="JSON config with title/date/time/start and optional overrides")
    p.add_argument("--gpx", type=Path, required=True, help="GPX route file")
    p.add_argument("--out", type=Path, default=Path("ride_poster_v2.png"), help="Output PNG/SVG/PDF path")
    return p


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    route = parse_gpx(args.gpx)
    render_poster(config, route, args.out)
    print(f"Poster saved to {args.out}")


if __name__ == "__main__":
    main()
