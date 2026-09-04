#!/usr/bin/env python3
"""Ride dashboard: one image for a group-ride announcement.

Header (date, time, start, pace), notes, stat tiles, route map on an OSM
basemap, elevation profile with the main climbs and a tailwind/headwind chart.
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
from matplotlib.patches import Circle, FancyBboxPatch, Wedge

import ride_poster as rp
import weather_dashboard as wd
from ride_poster import (
    ACCENT,
    ACCENT_SOFT,
    BG,
    BORDER,
    CARD,
    INK,
    MUTED,
    PRIMARY,
    PRIMARY_SOFT,
    SECONDARY,
    SECONDARY_SOFT,
    SHADOW,
    SUBTLE,
    ax_fit_text,
    fig_circle,
    fig_fit_text,
    fmt_km,
    fmt_m,
    fs,
    lw,
    normalize_pace,
    rounded_panel,
    text_h_fig,
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
DEFAULT_ATTRIBUTION = "@gpx_weather_bot · OpenStreetMap · Open-Meteo"
WEATHER_SAMPLE_KM = 6.0
WIND_BIN_MINUTES = 10
TILE_CACHE_DIR = os.path.join("cache", "tiles")

# Figure fractions, y measured from the bottom.
LAYOUT = {
    "margin_x": 0.05,
    "panel_w": 0.90,
    "kicker_y": 0.972,
    "title_y": 0.955,
    "head_label_y": 0.905,
    "head_value_y": 0.872,
    "notes_top": 0.828,
    "notes_h": 0.070,
    "tiles_y": 0.665,
    "tiles_h": 0.072,
    "map_y": 0.375,
    "map_h": 0.268,
    "row_y": 0.195,
    "row_h": 0.158,
    "wind_y": 0.058,
    "wind_h": 0.115,
    "footer_y": 0.026,
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
        "speed_kmh": 27.0,        # planned speed used for timing and weather sampling
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


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _init_figure():
    fig = plt.figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    rp._SCALE = math.sqrt((WIDTH_PX / DPI) * (HEIGHT_PX / DPI) / rp._REF_AREA_IN2)
    fig.patch.set_facecolor(BG)
    fig.patches.append(fig_circle(fig, 0.93, 0.95, 0.10, facecolor=PRIMARY_SOFT, edgecolor="none", zorder=0))
    fig.patches.append(fig_circle(fig, 0.08, 0.06, 0.07, facecolor=SECONDARY_SOFT, edgecolor="none", zorder=0))
    return fig


def _style_chart_ax(ax):
    ax.set_facecolor(CARD)
    ax.grid(True, axis="y", color=BORDER, linewidth=lw(0.9))
    ax.grid(False, axis="x")
    ax.tick_params(axis="both", length=0, pad=3, colors=SUBTLE, labelsize=fs(9))
    for spine in ax.spines.values():
        spine.set_visible(False)


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


def fmt_speed_range(rng) -> str:
    lo, hi = float(rng[0]), float(rng[1])
    if abs(lo - hi) < 0.05:
        return f"{lo:g} km/h"
    return f"{lo:g}–{hi:g} km/h"


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
    columns = [("DATE", 0.05, 0.20), ("TIME", 0.27, 0.16)]
    start_text = str(cfg.get("start") or "").strip()
    if start_text:
        columns.append(("START", 0.45, 0.28))
    pace = cfg.get("pace")
    speed_range = cfg.get("speed_range")
    if pace is not None or speed_range:
        columns.append(("PACE", 0.75, 0.20))
    for label, x, _w in columns:
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
        fontsize=fs(14), max_w=L["panel_w"], max_h=L["notes_h"], max_lines=4, min_fontsize=fs(10.5),
        color=INK, va="top", zorder=4, linespacing=1.28,
    )


def draw_tile(fig, x: float, y: float, w: float, h: float, label: str, value: str, sub: str, accent: str, soft: str) -> None:
    shadow = FancyBboxPatch((x + 0.004, y - 0.004), w, h, boxstyle="round,pad=0.006,rounding_size=0.022", transform=fig.transFigure, linewidth=0, facecolor=SHADOW, zorder=5)
    card = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.006,rounding_size=0.022", transform=fig.transFigure, linewidth=lw(1.0), edgecolor=BORDER, facecolor=CARD, zorder=6)
    pill_w, pill_h = 0.022, 0.55 * h
    pill_x, pill_y = x + 0.012, y + 0.225 * h
    badge = FancyBboxPatch((pill_x, pill_y), pill_w, pill_h, boxstyle="round,pad=0.004,rounding_size=0.012", transform=fig.transFigure, linewidth=0, facecolor=soft, zorder=7)
    fig.patches.extend([shadow, card, badge])
    fig.patches.append(fig_circle(fig, pill_x + pill_w / 2.0, pill_y + pill_h / 2.0, 0.0065, facecolor=accent, edgecolor="none", zorder=8))
    text_x = pill_x + pill_w + 0.012
    text_w = x + w - 0.012 - text_x
    fig.text(text_x, y + 0.78 * h, label.upper(), fontsize=fs(9.5), color=SUBTLE, va="center", zorder=8)
    fig_fit_text(fig, text_x, y + 0.47 * h, value, fontsize=fs(19.5), max_w=text_w, max_lines=1, min_fontsize=fs(11), fontweight="bold", color=INK, va="center", zorder=8)
    if sub:
        fig_fit_text(fig, text_x, y + 0.17 * h, sub, fontsize=fs(9.5), max_w=text_w, max_lines=1, min_fontsize=fs(7), color=SUBTLE, va="center", zorder=8)


def draw_tiles(fig, tiles: list[tuple[str, str, str, str, str]]) -> None:
    L = LAYOUT
    n = len(tiles)
    gap = 0.02
    w = (L["panel_w"] - gap * (n - 1)) / n
    for i, (label, value, sub, accent, soft) in enumerate(tiles):
        draw_tile(fig, L["margin_x"] + i * (w + gap), L["tiles_y"], w, L["tiles_h"], label, value, sub, accent, soft)


def fmt_range(lo: float, hi: float, unit: str) -> str:
    """'18–24 °C', or '0 %' when both ends round to the same value."""
    lo_s, hi_s = f"{lo:.0f}", f"{hi:.0f}"
    return f"{lo_s} {unit}" if lo_s == hi_s else f"{lo_s}–{hi_s} {unit}"


def _fmt_hours(hours: float) -> str:
    if hours < 1:
        return f"{int(round(hours * 60))} min"
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h} h {m:02d}" if m else f"{h} h"


def build_tiles(route: rp.RouteData, climbs, cfg: dict, samples, speed_kmh: float) -> list[tuple[str, str, str, str, str]]:
    distance_km = float(cfg["distance_km"]) if cfg.get("distance_km") is not None else float(route.dist_km[-1])
    if cfg.get("elevation_m") is not None:
        elevation_m = float(cfg["elevation_m"])
    elif route.gain_m is not None:
        elevation_m = float(route.gain_m)
    else:
        elevation_m = rp.total_gain_m(route.ele)
    tiles = [
        ("Distance", fmt_km(distance_km), f"about {_fmt_hours(distance_km / speed_kmh)} at {speed_kmh:g} km/h", PRIMARY, PRIMARY_SOFT),
        ("Elevation", fmt_m(elevation_m), f"{len(climbs)} main climb{'s' if len(climbs) != 1 else ''}" if climbs else "no major climbs", SECONDARY, SECONDARY_SOFT),
    ]
    if samples:
        temps = [float(w["temperature"]) for _, w in samples]
        feels = [float(w["feels_like"]) for _, w in samples]
        probs = [min(100.0, max(0.0, float(w["precipitation_probability"]))) for _, w in samples]
        mm = [max(0.0, float(w["precipitation_mm"])) for _, w in samples]
        tiles.append(("Temperature", fmt_range(min(temps), max(temps), "°C"), f"feels like {fmt_range(min(feels), max(feels), '°C')}", ACCENT, ACCENT_SOFT))
        rain_sub = f"up to {max(mm):.1f} mm/h" if max(mm) > 0 else "no rain in the forecast"
        tiles.append(("Rain chance", fmt_range(min(probs), max(probs), "%"), rain_sub, PRIMARY, PRIMARY_SOFT))
    else:
        fine = rp.fine_grade_profile(route)
        max_grade = float(np.max(fine[1])) if len(fine[1]) else 0.0
        tiles.append(("Max grade", f"{max_grade:.0f} %", "steepest 100 m", ACCENT, ACCENT_SOFT))
        tiles.append(("Weather", "n/a", "forecast unavailable", MUTED, BG))
    return tiles


def _mercator_arrays(route: rp.RouteData) -> tuple[np.ndarray, np.ndarray]:
    pts = [wd.latlon_to_web_mercator(float(la), float(lo)) for la, lo in zip(route.lat, route.lon)]
    return np.array([p[0] for p in pts]), np.array([p[1] for p in pts])


def draw_map(fig, route: rp.RouteData, climbs, samples) -> None:
    L = LAYOUT
    ax, _ = wd.map_panel(fig, L["margin_x"], L["map_y"], L["panel_w"], L["map_h"])
    xs, ys = _mercator_arrays(route)
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
                ax,
                source=wd.ctx.providers.OpenStreetMap.Mapnik,
                crs="EPSG:3857",
                attribution="© OpenStreetMap contributors",
                attribution_size=fs(6.5),
                zoom="auto",
                headers=wd.osm_tile_headers(),
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
    n_arrows = 5
    for k in range(1, n_arrows + 1):
        i = int(n * k / (n_arrows + 1))
        j = min(n - 1, i + 2)
        if j <= i:
            continue
        angle = math.degrees(math.atan2(ys[j] - ys[i], xs[j] - xs[i]))
        ax.plot(
            [xs[i]], [ys[i]], linestyle="none", marker=(3, 0, angle - 90.0), markersize=fs(8.5),
            markerfacecolor=ACCENT, markeredgecolor=CARD, markeredgewidth=lw(1.2), zorder=9,
        )

    # Small wind arrows at the forecast samples: the arrow points where the wind blows to.
    for point, weather in samples:
        if float(weather["wind_speed"]) <= 0:
            continue
        px, py = wd.latlon_to_web_mercator(point["lat"], point["lon"])
        to_rad = math.radians((float(weather["wind_direction"]) + 180.0) % 360.0)
        length_pt = fs(13)
        dx, dy = length_pt * math.sin(to_rad), length_pt * math.cos(to_rad)
        for color, width, scale, alpha in ((CARD, lw(3.0), fs(10.5), 0.95), (INK, lw(1.5), fs(8.5), 0.9)):
            ax.annotate(
                "", xy=(px + 0, py + 0), xycoords="data", xytext=(-dx, -dy), textcoords="offset points",
                arrowprops=dict(arrowstyle="-|>", color=color, lw=width, mutation_scale=scale, alpha=alpha, shrinkA=0, shrinkB=0),
                zorder=11,
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

    for i, climb in enumerate(climbs, start=1):
        idx = rp.index_for_distance(route, (climb.start_km + climb.end_km) / 2.0)
        ax.add_patch(Circle((xs[idx], ys[idx]), marker_r * 0.85, facecolor=CARD, edgecolor=PRIMARY, linewidth=lw(2.0), zorder=18))
        ax.text(xs[idx], ys[idx], str(i), ha="center", va="center", fontsize=fs(8.5), color=PRIMARY, fontweight="bold", zorder=19)

    ax.set_facecolor(CARD)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(False)
    handles = [plt.Line2D([0], [0], color=PRIMARY, linewidth=lw(3.5), label="Route")]
    if samples:
        handles.append(plt.Line2D([0], [0], color=INK, linewidth=lw(1.4), marker=">", markersize=fs(5), label="Wind"))
    if climbs:
        handles.append(plt.Line2D([0], [0], color=PRIMARY, marker="o", markerfacecolor=CARD, markersize=fs(7), linewidth=0, label="Climb"))
    leg = ax.legend(handles=handles, loc="upper right", fontsize=fs(8), frameon=True, framealpha=1.0, facecolor=CARD, edgecolor=BORDER, labelcolor=INK)
    leg.set_zorder(20)


def draw_climb_rows(ax, climbs, max_rows: int = 4) -> None:
    ax.axis("off")
    if not climbs:
        ax_fit_text(ax, 0.0, 0.92, "No major climbs on this route.", fontsize=fs(11), max_w=1.0, max_lines=2, color=SUBTLE, va="top")
        return
    n = min(max_rows, len(climbs))
    gap = 0.045
    row_h = (1.0 - gap * (n - 1)) / n
    for i, climb in enumerate(climbs[:n], start=1):
        top = 1.0 - i * row_h - (i - 1) * gap
        ax.add_patch(FancyBboxPatch((0.012, top), 0.976, row_h, boxstyle="round,pad=0.010,rounding_size=0.028", linewidth=lw(1.0), edgecolor=BORDER, facecolor=BG, transform=ax.transAxes))
        cy1 = top + row_h * 0.68
        cy2 = top + row_h * 0.28
        ax.add_patch(rp.ax_circle(ax, 0.075, cy1, 0.040, facecolor=PRIMARY, edgecolor="none"))
        ax.text(0.075, cy1, str(i), ha="center", va="center", fontsize=fs(10), color=CARD, fontweight="bold", transform=ax.transAxes)
        ax_fit_text(ax, 0.16, cy1, climb.label, fontsize=fs(12.5), max_w=0.52, max_lines=1, color=INK, fontweight="bold", va="center")
        ax_fit_text(ax, 0.96, cy1, f"max {climb.max_grade:.0f}%", fontsize=fs(11), max_w=0.24, ha="right", va="center", color=PRIMARY, fontweight="bold")
        ax_fit_text(ax, 0.16, cy2, f"{climb.length_km:.1f} km · {climb.avg_grade:.1f}% · +{int(round(climb.gain_m))} m", fontsize=fs(10), max_w=0.54, color=SUBTLE, va="center")
        ax_fit_text(ax, 0.96, cy2, f"{climb.start_km:.0f}–{climb.end_km:.0f} km", fontsize=fs(9.6), max_w=0.24, ha="right", va="center", color=SUBTLE)


def draw_wind_chart(fig, ax, series, plot_tz, samples) -> None:
    times = [t for t, _, _ in series]
    along = np.array([a for _, a, _ in series])
    cross = np.array([c for _, _, c in series])
    width_days = (WIND_BIN_MINUTES / 1440.0) * 0.82
    colors = [SECONDARY if a >= 0 else ACCENT for a in along]
    ax.bar(times, along, width=width_days, color=colors, linewidth=0, zorder=3)
    ax.plot(times, cross, color=SUBTLE, linewidth=lw(1.6), linestyle="--", zorder=4)
    ax.axhline(0, color=INK, linewidth=lw(1.0), alpha=0.5, zorder=2)
    ymax = max(5.0, float(np.max(np.abs(along))), float(np.max(cross))) * 1.3
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
        "green = tailwind, red = headwind, dashed = crosswind · 10-min averages"
    )


def draw_footer(fig, cfg: dict) -> None:
    L = LAYOUT
    fig.text(L["margin_x"], L["footer_y"], str(cfg.get("attribution") or DEFAULT_ATTRIBUTION), fontsize=fs(9.6), color=SUBTLE, va="center", zorder=4)
    watermark = str(cfg.get("watermark") or "").strip()
    if watermark:
        fig.text(
            1.0 - L["margin_x"], L["footer_y"] + 0.004, watermark,
            fontsize=fs(44), color=INK, alpha=0.08, ha="right", va="center", fontweight="bold", zorder=0.5,
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
    speed_kmh = float(cfg.get("speed_kmh") or 27.0)
    if speed_kmh <= 0:
        speed_kmh = 27.0

    samples: list[tuple[dict, dict]] = []
    if cfg.get("weather", True):
        samples = fetch_weather(sample_points(route, start_dt, speed_kmh))
    series = wind_series(route, samples, start_dt, speed_kmh) if samples else []

    fig = _init_figure()
    L = LAYOUT
    draw_header(fig, cfg, start_dt)
    draw_notes(fig, str(cfg.get("notes") or ""))
    draw_tiles(fig, build_tiles(route, climbs, cfg, samples, speed_kmh))
    draw_map(fig, route, climbs, samples)

    profile_ax, profile_rect = rounded_panel(fig, L["margin_x"], L["row_y"], 0.54, L["row_h"], "Elevation profile", None, footer_ratio=0.09)
    rp.draw_profile(fig, profile_ax, profile_rect, route, climbs, legend_at="title")
    climbs_ax, _ = rounded_panel(fig, 0.61, L["row_y"], 0.34, L["row_h"], "Main climbs", None, footer_ratio=0.04)
    draw_climb_rows(climbs_ax, climbs)

    if series:
        wind_ax, _ = rounded_panel(fig, L["margin_x"], L["wind_y"], L["panel_w"], L["wind_h"], "Tailwind / headwind", wind_summary(samples), footer_ratio=0.16)
        draw_wind_chart(fig, wind_ax, series, plot_tz, samples)
    else:
        wind_ax, _ = rounded_panel(fig, L["margin_x"], L["wind_y"], L["panel_w"], L["wind_h"], "Tailwind / headwind", None, footer_ratio=0.1)
        wind_ax.axis("off")
        ax_fit_text(wind_ax, 0.0, 0.7, "Weather forecast unavailable for this date, wind chart skipped.", fontsize=fs(11), max_w=1.0, max_lines=2, color=SUBTLE, va="center")

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
