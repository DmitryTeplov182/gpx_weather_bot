#!/usr/bin/env python3
"""Ride dashboard: one image for a group-ride announcement.

Flat layout on a light ground: header (date, time, start, pace), notes,
a row of stat tiles with icons, the route map on an OSM basemap with the
climbs highlighted and an average-wind compass, the elevation profile with
the main climbs and a tailwind/headwind chart.

Weather comes from Open-Meteo and is optional: when it cannot be fetched the
image still renders with route-only tiles and no wind chart, so an
announcement never loses its picture because of a network hiccup.

Usage:
    python ride_dashboard.py --gpx route.gpx --config config.json --out dashboard.png
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.image import AxesImage
from matplotlib.lines import Line2D
from matplotlib.patches import Arc, Circle, FancyBboxPatch, Patch, Polygon, Rectangle, Wedge
from matplotlib.collections import Collection
from matplotlib.text import Text

import ride_poster as rp
import weather_dashboard as wd
from ride_poster import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    INK,
    MUTED,
    PRIMARY,
    PRIMARY_SOFT,
    SECONDARY,
    SECONDARY_SOFT,
    SUBTLE,
    fig_circle,
    fig_fit_text,
    fmt_km,
    fmt_m,
    fs,
    lw,
    normalize_pace,
    text_h_fig,
    text_w_fig,
)

# Progress messages use emoji; on a cp1252 console (Windows dev box) they must
# not turn a finished render into a crash.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

WIDTH_PX, HEIGHT_PX, DPI = 1600, 2200, 160
DEFAULT_WATERMARK = "whatever"
DEFAULT_ATTRIBUTION = "@burekovoz_bot · OpenStreetMap · Open-Meteo"
DEFAULT_SPEED_KMH = 27.0
WEATHER_SAMPLE_KM = 6.0
WIND_BIN_MINUTES = 10
TILE_CACHE_DIR = os.path.join("cache", "tiles")
MAP_CORNER_PX = 26

# Figure fractions, y measured from the bottom. Sections sit directly on the
# ground with no cards, so the map and charts get most of the height.
LAYOUT = {
    "margin_x": 0.05,
    "panel_w": 0.90,
    "kicker_y": 0.975,
    "title_y": 0.958,
    "head_label_y": 0.905,
    "head_value_y": 0.873,
    "notes_top": 0.836,
    "notes_h": 0.062,
    "tiles_y": 0.698,
    "tiles_h": 0.062,
    "map_y": 0.365,
    "map_h": 0.312,
    "profile_y": 0.178,
    "profile_h": 0.165,
    "profile_w": 0.56,
    "climbs_x": 0.645,
    "wind_y": 0.045,
    "wind_h": 0.110,
    "footer_y": 0.018,
}


def default_config() -> dict:
    return {
        "kicker": "ROAD RIDE",
        "route_name": "",
        "start_iso": None,        # e.g. "2026-09-10T18:00:00+02:00"
        "timezone": None,         # IANA name; detected from the GPX when missing
        "start": "",              # start place
        "pace": None,             # 1.0..3.0 (moons) or None
        "speed_range": None,      # [lo, hi] km/h or None
        "speed_kmh": None,        # explicit planned speed; ignored when pace or speed_range is set
        "notes": "",
        "distance_km": None,
        "elevation_m": None,
        "climb_names": [],
        "weather": True,
        "watermark": DEFAULT_WATERMARK,
        "attribution": DEFAULT_ATTRIBUTION,
    }


def load_config(path: Path | None) -> dict:
    cfg = default_config()
    if path is not None:
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def parse_start(start_iso: str | None, tz) -> datetime:
    if start_iso:
        dt = datetime.fromisoformat(start_iso)
        if dt.tzinfo is None:
            return tz.localize(dt)
        return dt.astimezone(tz)
    default = datetime.now(tz) + timedelta(days=1)
    return default.replace(hour=8, minute=30, second=0, microsecond=0)


def planned_speed(cfg: dict, route: rp.RouteData) -> tuple[float, float, str]:
    """(average km/h, moving hours, note for the distance tile).

    A speed range wins; otherwise the pace level is run through the route
    profile (rp.estimate_ride_hours); otherwise the explicit speed or 27.
    """
    total_km = float(route.dist_km[-1])
    speed_range = cfg.get("speed_range")
    pace = cfg.get("pace")
    if speed_range:
        speed = (float(speed_range[0]) + float(speed_range[1])) / 2.0
        return speed, total_km / speed, f"at {fmt_speed_range(speed_range)}"
    if pace is not None:
        level = normalize_pace(pace)
        flat_kmh, w_per_kg = rp.PACE_MODEL[level]
        hours = rp.estimate_ride_hours(route, flat_kmh, w_per_kg)
        speed = total_km / hours if hours > 0 else flat_kmh
        return speed, hours, f"at {speed:.0f} km/h"
    speed = float(cfg.get("speed_kmh") or DEFAULT_SPEED_KMH)
    if speed <= 0:
        speed = DEFAULT_SPEED_KMH
    return speed, total_km / speed, f"at {speed:g} km/h"


def sample_points(route: rp.RouteData, start_dt: datetime, speed_kmh: float, interval_km: float = WEATHER_SAMPLE_KM) -> list[dict]:
    """Route points every `interval_km` (plus start and finish) with the time we expect to be there."""
    total = float(route.dist_km[-1])
    targets = list(np.arange(0.0, total, interval_km))
    if not targets or total - targets[-1] > 0.5:
        targets.append(total)
    pts = []
    for t in targets:
        i = int(np.clip(np.searchsorted(route.dist_km, t), 0, len(route.dist_km) - 1))
        pts.append({
            "lat": float(route.lat[i]),
            "lon": float(route.lon[i]),
            "ele": float(route.ele[i]),
            "distance_km": float(route.dist_km[i]),
            "time": start_dt + timedelta(hours=float(route.dist_km[i]) / speed_kmh),
        })
    return pts


def fetch_weather(points: list[dict]) -> list[tuple[dict, dict]]:
    """(point, weather) pairs for the points Open-Meteo answered; [] on any failure."""
    try:
        data = wd.get_weather_data_for_route(points)
    except Exception as e:  # network, API, parsing: the dashboard renders without weather
        print(f"⚠️ Weather unavailable: {e}")
        return []
    pairs = [(p, w) for p, w in zip(points, data) if w is not None]
    if len(pairs) < max(1, len(points) // 2):
        print(f"⚠️ Weather covers only {len(pairs)}/{len(points)} points, skipping weather blocks")
        return []
    return pairs


def bearings_deg(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    lat1 = np.radians(lat[:-1])
    lat2 = np.radians(lat[1:])
    dlon = np.radians(lon[1:] - lon[:-1])
    x = np.sin(dlon) * np.cos(lat2)
    y = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    return (np.degrees(np.arctan2(x, y)) + 360.0) % 360.0


def wind_series(route: rp.RouteData, samples: list[tuple[dict, dict]], start_dt: datetime, speed_kmh: float) -> list[tuple[datetime, float, float]]:
    """Along-track wind per time bucket: (bucket time, tailwind km/h (+) / headwind (-), crosswind km/h).

    Each 100 m segment of the route takes the wind of the nearest forecast
    sample, so a twisting road through a valley gets its true mix of head and
    tail wind rather than one arrow per 6 km.
    """
    if not samples or len(route.dist_km) < 2:
        return []
    d = route.dist_km
    mid_km = (d[:-1] + d[1:]) / 2.0
    heading = bearings_deg(route.lat, route.lon)
    sample_km = np.array([p["distance_km"] for p, _ in samples])
    speed = np.array([float(w["wind_speed"]) for _, w in samples])
    wind_from = np.array([float(w["wind_direction"]) for _, w in samples])
    idx = np.abs(mid_km[:, None] - sample_km[None, :]).argmin(axis=1)
    rel = np.radians(wind_from[idx] - heading)
    along = -speed[idx] * np.cos(rel)   # wind blows *from* wind_from; +: pushes along the heading
    cross = np.abs(speed[idx] * np.sin(rel))
    bin_h = WIND_BIN_MINUTES / 60.0
    bins = np.floor((mid_km / speed_kmh) / bin_h).astype(int)
    out = []
    for b in np.unique(bins):
        m = bins == b
        t = start_dt + timedelta(hours=(float(b) + 0.5) * bin_h)
        out.append((t, float(along[m].mean()), float(cross[m].mean())))
    return out


COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_label(deg: float) -> str:
    return COMPASS_POINTS[int((deg + 11.25) // 22.5) % 16]


def mean_wind(samples) -> tuple[float, float, float]:
    """(mean speed km/h, mean direction the wind comes FROM, mean direction it blows TO).

    Direction is the vector mean so opposite gusts cancel; speed is the plain
    mean so the label still says how windy the ride is.
    """
    speeds = np.array([float(w["wind_speed"]) for _, w in samples])
    dirs = np.radians([float(w["wind_direction"]) for _, w in samples])
    u = -speeds * np.sin(dirs)   # x component of where the wind blows to
    v = -speeds * np.cos(dirs)
    to_deg = (math.degrees(math.atan2(float(u.mean()), float(v.mean()))) + 360.0) % 360.0
    return float(speeds.mean()), (to_deg + 180.0) % 360.0, to_deg


# ---------------------------------------------------------------------------
# Small drawing helpers
# ---------------------------------------------------------------------------


def _init_figure():
    fig = plt.figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    rp._SCALE = math.sqrt((WIDTH_PX / DPI) * (HEIGHT_PX / DPI) / rp._REF_AREA_IN2)
    fig.patch.set_facecolor(BG)
    fig.patches.append(fig_circle(fig, 0.94, 0.965, 0.085, facecolor=PRIMARY_SOFT, edgecolor="none", zorder=0))
    return fig


def _style_chart_ax(ax):
    ax.set_facecolor("none")
    ax.grid(True, axis="y", color=BORDER, linewidth=lw(0.9))
    ax.grid(False, axis="x")
    ax.tick_params(axis="both", length=0, pad=3, colors=SUBTLE, labelsize=fs(9))
    for spine in ax.spines.values():
        spine.set_visible(False)


def section_title(fig, x: float, y_top: float, text: str) -> float:
    """Uppercase section heading; returns the y just below it."""
    t = fig.text(x, y_top, text.upper(), fontsize=fs(12), color=INK, fontweight="bold", va="top", zorder=4)
    return y_top - text_h_fig(fig, t)


def fig_line(fig, x0: float, y0: float, x1: float, y1: float, **kwargs) -> None:
    fig.add_artist(Line2D([x0, x1], [y0, y1], transform=fig.transFigure, **kwargs))


def fmt_speed_range(rng) -> str:
    lo, hi = float(rng[0]), float(rng[1])
    if abs(lo - hi) < 0.05:
        return f"{lo:g} km/h"
    return f"{lo:g}–{hi:g} km/h"


def fmt_range(lo: float, hi: float, unit: str) -> str:
    """'18–24 °C', or '0 %' when both ends round to the same value."""
    lo_s, hi_s = f"{lo:.0f}", f"{hi:.0f}"
    return f"{lo_s} {unit}" if lo_s == hi_s else f"{lo_s}–{hi_s} {unit}"


def _fmt_hours(hours: float) -> str:
    """'1h 20m' / '45m'."""
    total_min = int(round(hours * 60))
    h, m = divmod(total_min, 60)
    if h == 0:
        return f"{m}m"
    return f"{h}h {m:02d}m" if m else f"{h}h"


def draw_pace_dots(fig, x_left: float, cy: float, pace: float, radius: float = 0.0105, gap: float = 0.030) -> float:
    """Three ink dots (full / half / empty) starting at x_left; returns the right edge."""
    pace = normalize_pace(pace)
    full = int(math.floor(pace))
    half = 1 if abs(pace - full - 0.5) < 0.1 else 0
    tr, a = rp._fig_circle_tr(fig)
    for i in range(3):
        cx = x_left + radius + i * gap
        c = (cx, cy / a)
        fig.patches.append(Circle(c, radius, transform=tr, facecolor=PRIMARY_SOFT, edgecolor=PRIMARY, linewidth=lw(1.4), zorder=6))
        if i < full:
            fig.patches.append(Circle(c, radius * 0.98, transform=tr, facecolor=PRIMARY, edgecolor="none", zorder=7))
        elif i == full and half:
            fig.patches.append(Wedge(c, radius * 0.98, 90, 270, transform=tr, facecolor=PRIMARY, edgecolor="none", zorder=7))
    return x_left + 2 * gap + 2 * radius


def _cloud(ax, color: str, lift: float) -> None:
    for cx, cy, r in ((0.30, 0.42 + lift, 0.19), (0.54, 0.54 + lift, 0.26), (0.78, 0.42 + lift, 0.18)):
        ax.add_patch(Circle((cx, cy), r, facecolor=color, edgecolor="none"))
    ax.add_patch(Rectangle((0.12, 0.24 + lift), 0.84, 0.20, facecolor=color, edgecolor="none"))


def draw_icon(fig, x: float, y: float, w: float, h: float, kind: str, color: str):
    """Flat vector icon in its own square axes (figure fractions)."""
    ax = fig.add_axes([x, y, w, h], zorder=8)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    if kind == "route":
        # A winding road between two waypoints: solid line, smooth S-curve.
        t = np.linspace(0.0, 1.0, 80)
        px = 0.16 + 0.68 * t
        py = 0.16 + 0.68 * t + 0.16 * np.sin(2.0 * math.pi * t)
        ax.plot(px, py, color=color, linewidth=lw(3.2), solid_capstyle="round", solid_joinstyle="round")
        for cx, cy in ((px[0], py[0]), (px[-1], py[-1])):
            ax.add_patch(Circle((cx, cy), 0.13, facecolor=CARD, edgecolor=color, linewidth=lw(2.4)))
    elif kind == "mountains":
        ax.add_patch(Polygon([(0.02, 0.18), (0.36, 0.62), (0.64, 0.18)], closed=True, facecolor=color, alpha=0.5, edgecolor="none"))
        ax.add_patch(Polygon([(0.34, 0.18), (0.70, 0.86), (1.0, 0.18)], closed=True, facecolor=color, edgecolor="none"))
    elif kind == "grade":
        ax.add_patch(Polygon([(0.08, 0.18), (0.92, 0.18), (0.92, 0.82)], closed=True, facecolor=color, edgecolor="none"))
    elif kind == "cloud":
        _cloud(ax, color, 0.0)
    elif kind == "rain":
        _cloud(ax, color, 0.16)
        for cx in (0.34, 0.54, 0.74):
            ax.plot([cx, cx - 0.07], [0.26, 0.08], color=color, linewidth=lw(2.0), solid_capstyle="round")
    elif kind == "thermo":
        ax.add_patch(FancyBboxPatch((0.42, 0.36), 0.16, 0.50, boxstyle="round,pad=0,rounding_size=0.08", facecolor=CARD, edgecolor=color, linewidth=lw(1.8)))
        ax.add_patch(Rectangle((0.465, 0.40), 0.07, 0.30, facecolor=color, edgecolor="none"))
        ax.add_patch(Circle((0.50, 0.24), 0.15, facecolor=color, edgecolor="none"))
    elif kind == "wind":
        for y0, x1, r in ((0.74, 0.62, 0.09), (0.50, 0.88, 0.10), (0.26, 0.55, 0.08)):
            ax.plot([0.05, x1], [y0, y0], color=color, linewidth=lw(2.2), solid_capstyle="round")
            ax.add_patch(Arc((x1, y0 + r), 2 * r, 2 * r, theta1=-90, theta2=180, edgecolor=color, linewidth=lw(2.2)))
    return ax


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def draw_header(fig, cfg: dict, start_dt: datetime) -> None:
    L = LAYOUT
    x0 = L["margin_x"]
    fig.text(x0, L["kicker_y"], str(cfg.get("kicker") or "ROAD RIDE").upper(), fontsize=fs(10.5), color=SUBTLE, fontweight="bold", va="center", zorder=4)
    fig_fit_text(
        fig, x0, L["title_y"], cfg.get("route_name") or "Group ride",
        fontsize=fs(25), max_w=L["panel_w"], max_lines=1, min_fontsize=fs(13),
        color=INK, fontweight="bold", va="top", zorder=4,
    )

    label_y, value_y = L["head_label_y"], L["head_value_y"]
    columns = [("DATE", 0.05), ("TIME", 0.27)]
    start_text = str(cfg.get("start") or "").strip()
    if start_text:
        columns.append(("START", 0.45))
    pace = cfg.get("pace")
    speed_range = cfg.get("speed_range")
    if pace is not None or speed_range:
        columns.append(("PACE", 0.75))
    for label, x in columns:
        fig.text(x, label_y, label, fontsize=fs(10), color=SUBTLE, va="center", zorder=4)

    fig_fit_text(fig, 0.05, value_y, start_dt.strftime("%a %d %b"), fontsize=fs(27), max_w=0.20, color=INK, fontweight="bold", va="center", zorder=4)
    fig_fit_text(fig, 0.27, value_y, start_dt.strftime("%H:%M"), fontsize=fs(34), max_w=0.16, color=PRIMARY, fontweight="bold", va="center", zorder=4)
    if start_text:
        fig_fit_text(
            fig, 0.45, value_y, start_text,
            fontsize=fs(20), max_w=0.28, max_h=0.055, max_lines=2, min_fontsize=fs(11),
            color=INK, fontweight="bold", va="center", zorder=4, linespacing=1.05,
        )
    if speed_range:
        fig_fit_text(fig, 0.75, value_y, fmt_speed_range(speed_range), fontsize=fs(22), max_w=0.20, min_fontsize=fs(12), color=INK, fontweight="bold", va="center", zorder=4)
    elif pace is not None:
        right = draw_pace_dots(fig, 0.75, value_y, float(pace))
        fig.text(right + 0.012, value_y, f"{normalize_pace(pace):g}/3", fontsize=fs(13), color=SUBTLE, va="center", zorder=4)


def draw_notes(fig, notes: str) -> None:
    if not notes:
        return
    L = LAYOUT
    fig_fit_text(
        fig, L["margin_x"], L["notes_top"], notes,
        fontsize=fs(13.5), max_w=L["panel_w"], max_h=L["notes_h"], max_lines=4, min_fontsize=fs(10),
        color=INK, va="top", zorder=4, linespacing=1.28,
    )


def build_tiles(route: rp.RouteData, climbs, cfg: dict, samples, hours: float, speed_note: str) -> list[tuple[str, str, str, str, str]]:
    """(label, value, sub, icon, colour) per tile."""
    distance_km = float(cfg["distance_km"]) if cfg.get("distance_km") is not None else float(route.dist_km[-1])
    if cfg.get("elevation_m") is not None:
        elevation_m = float(cfg["elevation_m"])
    elif route.gain_m is not None:
        elevation_m = float(route.gain_m)
    else:
        elevation_m = rp.total_gain_m(route.ele)
    climbs_note = f"{len(climbs)} main climb{'s' if len(climbs) != 1 else ''}" if climbs else "no major climbs"
    tiles = [
        ("Distance", fmt_km(distance_km), f"~{_fmt_hours(hours)} {speed_note}", "route", PRIMARY),
        ("Elevation", fmt_m(elevation_m), climbs_note, "mountains", SECONDARY),
    ]
    if samples:
        temps = [float(w["temperature"]) for _, w in samples]
        feels = [float(w["feels_like"]) for _, w in samples]
        probs = [min(100.0, max(0.0, float(w["precipitation_probability"]))) for _, w in samples]
        mm = [max(0.0, float(w["precipitation_mm"])) for _, w in samples]
        tiles.append(("Temperature", fmt_range(min(temps), max(temps), "°C"), f"feels like {fmt_range(min(feels), max(feels), '°C')}", "thermo", ACCENT))
        rain_sub = f"up to {max(mm):.1f} mm/h" if max(mm) > 0 else "no rain in the forecast"
        tiles.append(("Rain chance", fmt_range(min(probs), max(probs), "%"), rain_sub, "rain", PRIMARY))
    else:
        fine = rp.fine_grade_profile(route)
        max_grade = float(np.max(fine[1])) if len(fine[1]) else 0.0
        tiles.append(("Max grade", f"{max_grade:.0f} %", "steepest 100 m", "grade", ACCENT))
        tiles.append(("Weather", "n/a", "forecast unavailable", "cloud", MUTED))
    return tiles


def draw_tiles(fig, tiles: list[tuple[str, str, str, str, str]]) -> None:
    """Stat tiles as icon + text columns separated by thin rules, no cards."""
    L = LAYOUT
    n = len(tiles)
    col_w = L["panel_w"] / n
    y, h = L["tiles_y"], L["tiles_h"]
    icon_w = 0.040
    icon_h = icon_w * WIDTH_PX / HEIGHT_PX
    for i, (label, value, sub, kind, color) in enumerate(tiles):
        x = L["margin_x"] + i * col_w
        if i > 0:
            fig_line(fig, x - 0.014, y + 0.004, x - 0.014, y + h - 0.004, color=BORDER, linewidth=lw(1.2), zorder=4)
        draw_icon(fig, x, y + (h - icon_h) / 2.0 + 0.003, icon_w, icon_h, kind, color)
        tx = x + icon_w + 0.012
        tw = col_w - icon_w - 0.032
        fig.text(tx, y + h - 0.002, label.upper(), fontsize=fs(9.5), color=SUBTLE, va="top", zorder=8)
        fig_fit_text(fig, tx, y + h * 0.50, value, fontsize=fs(21), max_w=tw, max_lines=1, min_fontsize=fs(12), fontweight="bold", color=INK, va="center", zorder=8)
        if sub:
            fig_fit_text(fig, tx, y + 0.002, sub, fontsize=fs(9.5), max_w=tw, max_lines=1, min_fontsize=fs(7), color=SUBTLE, va="bottom", zorder=8)


def _track_arrays(route: rp.RouteData) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(x, y, dist_km) in Web Mercator for the map: the raw GPX track when
    available, so bridges, underpasses and U-turns look exactly as routed."""
    if route.raw_lat is not None and route.raw_lon is not None and route.raw_dist_km is not None:
        lat, lon, dist = route.raw_lat, route.raw_lon, route.raw_dist_km
    else:
        lat, lon, dist = route.lat, route.lon, route.dist_km
    pts = [wd.latlon_to_web_mercator(float(la), float(lo)) for la, lo in zip(lat, lon)]
    return np.array([p[0] for p in pts]), np.array([p[1] for p in pts]), np.asarray(dist)


def draw_scale_bar(ax, lat_deg: float) -> None:
    """Two-segment scale bar in the bottom-left corner, in ground kilometres."""
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    k = math.cos(math.radians(lat_deg))  # ground metres per Web Mercator metre
    span_km = (x1 - x0) * k / 1000.0
    target = span_km * 0.16
    length_km = max([s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200) if s <= target], default=1)
    length = length_km * 1000.0 / k
    bx = x0 + (x1 - x0) * 0.016
    by = y0 + (y1 - y0) * 0.085
    bar_h = (y1 - y0) * 0.012
    pad_x = (x1 - x0) * 0.008
    # The last label ("10 km") is centred on the bar end, so the backing box
    # needs room to its right for half of it.
    label_room = (x1 - x0) * 0.034
    ax.add_patch(FancyBboxPatch(
        (bx - pad_x, by - (y1 - y0) * 0.02), length + 2 * pad_x + label_room, bar_h + (y1 - y0) * 0.075,
        boxstyle="round,pad=0", facecolor=CARD, edgecolor="none", alpha=0.85, zorder=20,
    ))
    ax.add_patch(Rectangle((bx, by), length / 2.0, bar_h, facecolor=INK, edgecolor=INK, linewidth=lw(0.8), zorder=21))
    ax.add_patch(Rectangle((bx + length / 2.0, by), length / 2.0, bar_h, facecolor=CARD, edgecolor=INK, linewidth=lw(0.8), zorder=21))
    for frac, label in ((0.0, "0"), (0.5, f"{length_km / 2:g}"), (1.0, f"{length_km:g} km")):
        ax.text(bx + frac * length, by + bar_h * 1.7, label, ha="center", va="bottom", fontsize=fs(6.5), color=INK, zorder=22)


def draw_wind_compass(fig, ax, samples) -> None:
    """Small compass in the top-left map corner: arrow shows where the average wind blows to."""
    avg_speed, from_deg, to_deg = mean_wind(samples)
    pos = ax.get_position()
    fig_w, fig_h = fig.get_size_inches()
    size_in = 0.95
    w = size_in / (pos.width * fig_w)
    h = size_in / (pos.height * fig_h)
    x0, y0 = 0.012, 1.0 - h - 0.02
    inset = ax.inset_axes([x0, y0, w, h], zorder=25)
    inset.set_xlim(-1.25, 1.25)
    inset.set_ylim(-1.25, 1.25)
    inset.set_aspect("equal")
    inset.axis("off")
    inset.add_patch(Circle((0, 0), 1.2, facecolor=CARD, edgecolor=BORDER, linewidth=lw(1.0), alpha=0.97))
    inset.add_patch(Circle((0, 0), 0.9, facecolor="none", edgecolor=BORDER, linewidth=lw(1.0)))
    for letter, ang in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        a = math.radians(ang)
        inset.text(
            0.9 * math.sin(a), 0.9 * math.cos(a), letter, ha="center", va="center",
            fontsize=fs(6.5), color=PRIMARY if letter == "N" else SUBTLE, fontweight="bold",
            bbox=dict(boxstyle="circle,pad=0.18", facecolor=CARD, edgecolor="none"),
        )
    t = math.radians(to_deg)
    dx, dy = math.sin(t), math.cos(t)
    inset.annotate(
        "", xy=(0.7 * dx, 0.7 * dy), xytext=(-0.7 * dx, -0.7 * dy),
        arrowprops=dict(arrowstyle="-|>", color=INK, lw=lw(2.2), mutation_scale=fs(12), shrinkA=0, shrinkB=0),
    )
    ax.text(
        x0 + 0.004, y0 - 0.025, f"Avg wind {avg_speed:.0f} km/h from {compass_label(from_deg)}",
        transform=ax.transAxes, ha="left", va="top", fontsize=fs(7.5), color=INK, fontweight="bold", zorder=26,
        bbox=dict(boxstyle="round,pad=0.3", facecolor=CARD, edgecolor=BORDER, linewidth=lw(0.8)),
    )


def _round_map_corners(fig, ax) -> None:
    """Clip everything drawn in the map axes to a rounded rectangle."""
    pos = ax.get_position()
    w_px = pos.width * fig.get_figwidth() * fig.dpi
    h_px = pos.height * fig.get_figheight() * fig.dpi
    clip = FancyBboxPatch(
        (0, 0), 1, 1, boxstyle=f"round,pad=0,rounding_size={MAP_CORNER_PX / w_px}",
        mutation_aspect=w_px / h_px, transform=ax.transAxes, facecolor="none", edgecolor="none",
    )
    for artist in ax.get_children():
        if isinstance(artist, Axes):
            continue
        if isinstance(artist, (AxesImage, Line2D, Patch, Collection, Text)):
            artist.set_clip_path(clip)


def draw_map(fig, route: rp.RouteData, climbs, samples) -> None:
    L = LAYOUT
    ax = fig.add_axes([L["margin_x"], L["map_y"], L["panel_w"], L["map_h"]], zorder=3)
    xs, ys, dist_km = _track_arrays(route)
    x_range = float(np.ptp(xs)) or 1.0
    y_range = float(np.ptp(ys)) or 1.0

    min_x, max_x, min_y, max_y = wd.route_map_bounds(list(xs), list(ys))
    fig_w, fig_h = fig.get_size_inches()
    pos = ax.get_position()
    axes_ratio = (pos.width * fig_w) / (pos.height * fig_h)
    min_x, max_x, min_y, max_y = wd.expand_bounds_to_axes_aspect(min_x, max_x, min_y, max_y, axes_ratio)
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(min_y, max_y)
    ax.set_aspect("equal", adjustable="box")

    if wd.ctx is not None:
        try:
            os.makedirs(TILE_CACHE_DIR, exist_ok=True)
            wd.ctx.set_cache_dir(TILE_CACHE_DIR)
            wd.ctx.add_basemap(
                ax, source=wd.ctx.providers.OpenStreetMap.Mapnik, crs="EPSG:3857",
                attribution="© OpenStreetMap contributors", attribution_size=fs(6.5),
                zoom="auto", headers=wd.osm_tile_headers(),
            )
        except Exception as e:
            print(f"⚠️ Failed to load OSM basemap: {e}")
    else:
        print("⚠️ OSM basemap unavailable: contextily is missing")

    ax.plot(xs, ys, color=PRIMARY_SOFT, linewidth=lw(9), solid_capstyle="round", zorder=6)
    ax.plot(xs, ys, color=PRIMARY, linewidth=lw(3.8), solid_capstyle="round", solid_joinstyle="round", zorder=7)

    # A few small direction markers along the route: a triangle rotated to the
    # local heading (over ~200 m) reads cleaner than a drawn arrow at map scale.
    n = len(xs)
    total_km = float(dist_km[-1]) if len(dist_km) else 0.0
    n_arrows = 5
    for k in range(1, n_arrows + 1):
        i = int(np.clip(np.searchsorted(dist_km, total_km * k / (n_arrows + 1)), 0, n - 1))
        j = int(np.clip(np.searchsorted(dist_km, dist_km[i] + 0.2), 0, n - 1))
        if j <= i:
            continue
        angle = math.degrees(math.atan2(ys[j] - ys[i], xs[j] - xs[i]))
        ax.plot(
            [xs[i]], [ys[i]], linestyle="none", marker=(3, 0, angle - 90.0), markersize=fs(8.5),
            markerfacecolor=ACCENT, markeredgecolor=CARD, markeredgewidth=lw(1.2), zorder=9,
        )

    marker_r = max(x_range, y_range) * 0.016
    sx, sy, fx, fy = xs[0], ys[0], xs[-1], ys[-1]
    if math.hypot(fx - sx, fy - sy) < max(x_range, y_range) * 0.06:
        ax.add_patch(Circle((sx, sy), marker_r * 1.15, facecolor=ACCENT, edgecolor=CARD, linewidth=lw(2.4), zorder=15))
        ax.add_patch(Circle((sx, sy), marker_r * 0.62, facecolor=SECONDARY, edgecolor="none", zorder=16))
        ax.text(sx, sy, "S/F", ha="center", va="center", fontsize=fs(7.5), color=CARD, fontweight="bold", zorder=17)
    else:
        ax.add_patch(Circle((sx, sy), marker_r, facecolor=SECONDARY, edgecolor=CARD, linewidth=lw(2.2), zorder=15))
        ax.text(sx, sy, "S", ha="center", va="center", fontsize=fs(8.5), color=CARD, fontweight="bold", zorder=16)
        ax.add_patch(Circle((fx, fy), marker_r * 0.92, facecolor=ACCENT, edgecolor=CARD, linewidth=lw(2.2), zorder=15))
        ax.text(fx, fy, "F", ha="center", va="center", fontsize=fs(8.5), color=CARD, fontweight="bold", zorder=16)

    # Climb segments in the accent colour on top of the route (the same colour
    # as "5%+" on the profile), numbered by a callout circle beside the
    # segment: a badge sitting on the line used to hide the climb itself.
    for i, climb in enumerate(climbs, start=1):
        s = int(np.clip(np.searchsorted(dist_km, climb.start_km), 0, n - 1))
        e = int(np.clip(np.searchsorted(dist_km, climb.end_km), 0, n - 1))
        if e <= s:
            continue
        ax.plot(xs[s:e + 1], ys[s:e + 1], color=CARD, linewidth=lw(7.5), solid_capstyle="round", zorder=12)
        ax.plot(xs[s:e + 1], ys[s:e + 1], color=ACCENT, linewidth=lw(4.2), solid_capstyle="round", solid_joinstyle="round", zorder=13)
        m = int(np.clip(np.searchsorted(dist_km, (climb.start_km + climb.end_km) / 2.0), 0, n - 1))
        j = int(np.clip(np.searchsorted(dist_km, dist_km[m] + 0.15), 0, n - 1))
        k = int(np.clip(np.searchsorted(dist_km, dist_km[m] - 0.15), 0, n - 1))
        hx, hy = xs[j] - xs[k], ys[j] - ys[k]
        norm = math.hypot(hx, hy) or 1.0
        offset_pt = fs(20)
        ox, oy = -hy / norm * offset_pt, hx / norm * offset_pt   # perpendicular, left of travel
        ax.annotate(
            str(i), xy=(xs[m], ys[m]), xytext=(ox, oy), textcoords="offset points",
            ha="center", va="center", fontsize=fs(8.5), color=ACCENT, fontweight="bold", zorder=19,
            bbox=dict(boxstyle="circle,pad=0.32", facecolor=CARD, edgecolor=ACCENT, linewidth=lw(1.6)),
            arrowprops=dict(arrowstyle="-", color=ACCENT, lw=lw(1.2), shrinkA=0, shrinkB=1),
        )

    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(False)
    ax.set_facecolor(BG)
    draw_scale_bar(ax, float(np.mean(route.lat)))
    handles = [Line2D([0], [0], color=PRIMARY, linewidth=lw(3.5), label="Route")]
    if climbs:
        handles.append(Line2D([0], [0], color=ACCENT, linewidth=lw(3.5), label="Climb"))
    leg = ax.legend(handles=handles, loc="upper right", fontsize=fs(8), frameon=True, framealpha=1.0, facecolor=CARD, edgecolor=BORDER, labelcolor=INK)
    leg.set_zorder(20)
    _round_map_corners(fig, ax)
    if samples:
        draw_wind_compass(fig, ax, samples)


def draw_profile_section(fig, route: rp.RouteData, climbs) -> None:
    L = LAYOUT
    x, y, w, h = L["margin_x"], L["profile_y"], L["profile_w"], L["profile_h"]
    body_top = section_title(fig, x, y + h, "Elevation profile") - 0.014
    ax = fig.add_axes([x + 0.012, y + 0.018, w - 0.024, max(0.03, body_top - y - 0.018)], zorder=3)
    ax.set_facecolor("none")
    # Gradient legend on the title row, right-aligned to the profile column.
    rp.draw_profile(fig, ax, (x, y, w, h), route, climbs, legend_at="title", legend_anchor=(x + w, y + h - 0.006))


def draw_climb_rows(fig, climbs, max_rows: int = 4) -> None:
    """Numbered climb rows separated by thin rules, no boxes."""
    L = LAYOUT
    x = L["climbs_x"]
    w = L["margin_x"] + L["panel_w"] - x
    y, h = L["profile_y"], L["profile_h"]
    top = section_title(fig, x, y + h, "Main climbs") - 0.010
    if not climbs:
        fig_fit_text(fig, x, top, "No major climbs on this route.", fontsize=fs(11), max_w=w, max_lines=2, color=SUBTLE, va="top", zorder=4)
        return
    n = min(max_rows, len(climbs))
    # Rows stack from the top at a fixed height; a single climb must not be
    # stretched over the whole section.
    row_h = min((top - y) / n, 0.040)
    tr, a = rp._fig_circle_tr(fig)
    r = 0.0085
    for i, climb in enumerate(climbs[:n], start=1):
        row_top = top - (i - 1) * row_h
        cy1 = row_top - row_h * 0.30
        cy2 = row_top - row_h * 0.70
        fig.patches.append(Circle((x + r, cy1 / a), r, transform=tr, facecolor=PRIMARY, edgecolor="none", zorder=6))
        fig.text(x + r, cy1, str(i), ha="center", va="center", fontsize=fs(8.5), color=CARD, fontweight="bold", zorder=7)
        tx = x + 2 * r + 0.012
        max_txt = fig_fit_text(fig, x + w, cy1, f"max {climb.max_grade:.0f}%", fontsize=fs(10.5), max_w=0.10, ha="right", va="center", color=PRIMARY, fontweight="bold", zorder=6)
        fig_fit_text(fig, tx, cy1, climb.label, fontsize=fs(12), max_w=w - (tx - x) - text_w_fig(fig, max_txt) - 0.012, max_lines=1, color=INK, fontweight="bold", va="center", zorder=6)
        rng = fig_fit_text(fig, x + w, cy2, f"{climb.start_km:.0f}–{climb.end_km:.0f} km", fontsize=fs(9.5), max_w=0.10, ha="right", va="center", color=SUBTLE, zorder=6)
        fig_fit_text(
            fig, tx, cy2, f"{climb.length_km:.1f} km · {climb.avg_grade:.1f}% · +{int(round(climb.gain_m))} m",
            fontsize=fs(9.5), max_w=w - (tx - x) - text_w_fig(fig, rng) - 0.012, max_lines=1, color=SUBTLE, va="center", zorder=6,
        )
        if i < n:
            fig_line(fig, x, row_top - row_h, x + w, row_top - row_h, color=BORDER, linewidth=lw(1.0), zorder=4)


def draw_wind_chart(fig, ax, series, plot_tz) -> None:
    times = [t for t, _, _ in series]
    along = np.array([a for _, a, _ in series])
    width_days = (WIND_BIN_MINUTES / 1440.0) * 0.82
    colors = [SECONDARY if a >= 0 else ACCENT for a in along]
    ax.bar(times, along, width=width_days, color=colors, linewidth=0, zorder=3)
    ax.axhline(0, color=INK, linewidth=lw(1.0), alpha=0.5, zorder=2)
    ymax = max(5.0, float(np.max(np.abs(along)))) * 1.3
    ax.set_ylim(-ymax, ymax)
    half = timedelta(minutes=WIND_BIN_MINUTES / 2.0)
    ax.set_xlim(times[0] - half, times[-1] + half)
    _style_chart_ax(ax)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: f"{v:+.0f}" if v else "0"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=plot_tz))
    plt.setp(ax.xaxis.get_majorticklabels(), fontsize=fs(8.5), color=SUBTLE)
    ax.text(0.995, 0.96, "tailwind", transform=ax.transAxes, ha="right", va="top", fontsize=fs(8.5), color=SECONDARY, fontweight="bold", zorder=6)
    ax.text(0.995, 0.04, "headwind", transform=ax.transAxes, ha="right", va="bottom", fontsize=fs(8.5), color=ACCENT, fontweight="bold", zorder=6)


def wind_summary(samples) -> str:
    speeds = [float(w["wind_speed"]) for _, w in samples]
    gusts = [float(w["wind_gusts"]) for _, w in samples]
    return (
        f"Wind {min(speeds):.0f}–{max(speeds):.0f} km/h, gusts to {max(gusts):.0f} km/h · "
        "km/h along the route: green pushes you, red holds you back · 10-min averages"
    )


def draw_wind_section(fig, series, samples, plot_tz) -> None:
    L = LAYOUT
    x, y, w, h = L["margin_x"], L["wind_y"], L["panel_w"], L["wind_h"]
    body_top = section_title(fig, x, y + h, "Tailwind / headwind") - 0.004
    if series:
        sub = fig_fit_text(fig, x, body_top, wind_summary(samples), fontsize=fs(10), max_w=w, max_lines=1, min_fontsize=fs(8), color=SUBTLE, va="top", zorder=4)
        body_top -= text_h_fig(fig, sub) + 0.012
        ax = fig.add_axes([x + 0.03, y + 0.016, w - 0.03, max(0.03, body_top - y - 0.016)], zorder=3)
        draw_wind_chart(fig, ax, series, plot_tz)
        return
    icon_w = 0.040
    icon_h = icon_w * WIDTH_PX / HEIGHT_PX
    draw_icon(fig, x, body_top - 0.014 - icon_h, icon_w, icon_h, "wind", MUTED)
    fig_fit_text(
        fig, x + icon_w + 0.014, body_top - 0.014 - icon_h / 2.0, "Weather forecast unavailable for this date, wind chart skipped.",
        fontsize=fs(11), max_w=w - icon_w - 0.02, max_lines=2, color=SUBTLE, va="center", zorder=4,
    )


def draw_footer(fig, cfg: dict) -> None:
    L = LAYOUT
    fig.text(L["margin_x"], L["footer_y"], str(cfg.get("attribution") or DEFAULT_ATTRIBUTION), fontsize=fs(9.6), color=SUBTLE, va="center", zorder=4)
    watermark = str(cfg.get("watermark") or "").strip()
    if watermark:
        fig.text(
            1.0 - L["margin_x"], L["footer_y"] + 0.004, watermark,
            fontsize=fs(44), color=INK, alpha=0.035, ha="right", va="center", fontweight="bold", zorder=0.5,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render(cfg: dict, gpx_path: Path, out_path: Path) -> bool:
    route = rp.parse_gpx(gpx_path)
    climbs = rp.detect_major_climbs(route, names=cfg.get("climb_names"))

    tz_name = cfg.get("timezone") or wd.detect_timezone_from_points([{"lat": float(route.lat[0]), "lon": float(route.lon[0])}])
    plot_tz = wd.get_timezone(tz_name)
    start_dt = parse_start(cfg.get("start_iso"), plot_tz)
    speed_kmh, hours, speed_note = planned_speed(cfg, route)

    samples: list[tuple[dict, dict]] = []
    if cfg.get("weather", True):
        samples = fetch_weather(sample_points(route, start_dt, speed_kmh))
    series = wind_series(route, samples, start_dt, speed_kmh) if samples else []

    fig = _init_figure()
    draw_header(fig, cfg, start_dt)
    draw_notes(fig, str(cfg.get("notes") or ""))
    draw_tiles(fig, build_tiles(route, climbs, cfg, samples, hours, speed_note))
    draw_map(fig, route, climbs, samples)
    draw_profile_section(fig, route, climbs)
    draw_climb_rows(fig, climbs)
    draw_wind_section(fig, series, samples, plot_tz)
    draw_footer(fig, cfg)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=DPI, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"✅ Dashboard saved to: {out_path}" + ("" if samples else " (without weather)"))
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a ride dashboard (route, climbs, weather) from a GPX file.")
    p.add_argument("--gpx", type=Path, required=True, help="GPX route file")
    p.add_argument("--config", type=Path, default=None, help="JSON config (see default_config())")
    p.add_argument("--out", type=Path, default=Path("ride_dashboard.png"), help="Output PNG path")
    p.add_argument("--no-weather", action="store_true", help="Skip the Open-Meteo request")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not args.gpx.exists():
        print(f"❌ File {args.gpx} not found!")
        sys.exit(1)
    cfg = load_config(args.config)
    if args.no_weather:
        cfg["weather"] = False
    render(cfg, args.gpx, args.out)


if __name__ == "__main__":
    main()
