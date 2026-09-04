"""Compare max-grade estimates: current ride_poster pipeline vs. less-smoothed variants.

Usage:
    python tools/grade_experiment.py routes/some.gpx komoot_coords.json ...

Accepts GPX files or Komoot coordinate JSON (`/v007/tours/{id}/coordinates`).
Prints, per detected climb, the max grade from the current pipeline and from
several lighter smoothing variants, so the chosen variant can be checked
against the value Komoot shows for the same tour.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import ride_poster as rp  # noqa: E402


def load_raw(path: Path) -> rp.RouteData:
    if path.suffix == ".json":
        items = json.loads(path.read_text(encoding="utf-8"))["items"]
        lat = np.array([p["lat"] for p in items], float)
        lon = np.array([p["lng"] for p in items], float)
        ele = np.array([p.get("alt", 0.0) for p in items], float)
    else:
        import xml.etree.ElementTree as ET

        root = ET.parse(path).getroot()
        pts: list[tuple[float, float, float]] = []
        for tag in ("trkpt", "rtept"):
            for p in root.iterfind(f".//{{*}}{tag}"):
                e = p.find("{*}ele")
                ele_v = float(e.text) if e is not None and e.text else 0.0
                pts.append((float(p.attrib["lat"]), float(p.attrib["lon"]), ele_v))
            if pts:
                break
        lat = np.array([p[0] for p in pts])
        lon = np.array([p[1] for p in pts])
        ele = np.array([p[2] for p in pts])
    dist = rp.cumulative_distance_km(lat, lon)
    return rp.RouteData(
        lat=lat, lon=lon, ele=ele, dist_km=dist, gain_m=rp.hysteresis_gain_m(ele),
        raw_ele=ele, raw_dist_km=dist,
    )


def resample(route: rp.RouteData, step_m: float, ma: int) -> rp.RouteData:
    total_m = route.dist_km[-1] * 1000.0
    new_d = np.arange(0.0, total_m + step_m, step_m)
    old_d = route.dist_km * 1000.0
    ele = np.interp(new_d, old_d, route.ele)
    if ma > 1:
        ele = rp.moving_average(ele, ma)
    return rp.RouteData(
        lat=np.interp(new_d, old_d, route.lat),
        lon=np.interp(new_d, old_d, route.lon),
        ele=ele,
        dist_km=new_d / 1000.0,
        gain_m=route.gain_m,
    )


def grade(route: rp.RouteData, window_m: float, post_ma: int) -> np.ndarray:
    d_m = route.dist_km * 1000.0
    g = np.zeros(len(d_m))
    for i in range(len(d_m)):
        s = np.searchsorted(d_m, d_m[i] - window_m / 2, side="left")
        e = min(len(d_m) - 1, np.searchsorted(d_m, d_m[i] + window_m / 2, side="right") - 1)
        dd = d_m[e] - d_m[s]
        if dd > 10:
            g[i] = 100.0 * (route.ele[e] - route.ele[s]) / dd
    return rp.moving_average(g, post_ma) if post_ma > 1 else g


def max_in(route: rp.RouteData, g: np.ndarray, start_km: float, end_km: float) -> float:
    m = (route.dist_km >= start_km) & (route.dist_km <= end_km)
    return float(np.max(g[m])) if m.any() else float("nan")


VARIANTS = {
    "A: 100m, MA3, 200m win, no post-MA": (100.0, 3, 200.0, 1),
    "B: 50m, MA3, 100m win, no post-MA": (50.0, 3, 100.0, 1),
    "C: 50m, MA5, 100m win, MA3": (50.0, 5, 100.0, 3),
    "D: 25m, MA5, 100m win, MA3": (25.0, 5, 100.0, 3),
    "E: 50m, no MA, 100m win, no post-MA (raw)": (50.0, 1, 100.0, 1),
}


def main(path: Path) -> None:
    raw = load_raw(path)
    current = rp.resample_route(raw, 100.0)  # 100 m + MA7, as in parse_gpx
    climbs = rp.detect_major_climbs(current)
    print(f"\n=== {path.name}: {raw.dist_km[-1]:.1f} km, {len(raw.lat)} raw pts, gain {raw.gain_m:.0f} m ===")
    grades = {}
    for name, (step, ma, win, post) in VARIANTS.items():
        r = resample(raw, step, ma)
        grades[name] = (r, grade(r, win, post))
    for c in climbs:
        print(f"\nclimb {c.start_km:.1f}-{c.end_km:.1f} km, +{c.gain_m:.0f} m, avg {c.avg_grade:.1f}%")
        print(f"  {'detect_major_climbs() as shipped':55s} max {c.max_grade:5.1f}%")
        for name, (r, g) in grades.items():
            print(f"  {name:55s} max {max_in(r, g, c.start_km, c.end_km):5.1f}%")
    print("\nwhole-route max grade per variant:")
    for name, (r, g) in grades.items():
        print(f"  {name:55s} {np.max(g):5.1f}%")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for p in sys.argv[1:]:
        main(Path(p))
