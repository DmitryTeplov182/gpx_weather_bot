#!/usr/bin/env python3
"""
Weather dashboard for cycling routes
"""

import sys
import argparse
import importlib.util  # Совместимость с niquests/openmeteo на Python 3.11
import gpxpy
import openmeteo_requests
import requests_cache
from retry_requests import retry
from datetime import datetime, timedelta
import os
import math

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz
from matplotlib.patches import Circle, FancyBboxPatch
from timezonefinder import TimezoneFinder

import ride_poster as rp
from ride_poster import (
    ACCENT,
    ACCENT_SOFT,
    BG,
    BORDER,
    CARD,
    CHIP_PAD,
    INK,
    MUTED,
    PRIMARY,
    PRIMARY_SOFT,
    SECONDARY,
    SECONDARY_SOFT,
    SHADOW,
    SUBTLE,
    add_info_chip,
    draw_stat_card,
    fig_circle,
    fig_fit_text,
    fmt_km,
    fs,
    lw,
    rounded_panel,
    text_h_fig,
)

try:
    import contextily as ctx
except ImportError:
    ctx = None

DEFAULT_OSM_TILE_USER_AGENT = (
    "gpx_weather_bot/1.0 (weather dashboard; contact: @gpx_weather_bot)"
)


def osm_tile_headers():
    """Headers for OSM tiles: identifiable user-agent (required by tile usage policy)."""
    ua = os.getenv("OSM_TILE_USER_AGENT", DEFAULT_OSM_TILE_USER_AGENT).strip()
    if not ua:
        ua = DEFAULT_OSM_TILE_USER_AGENT
    # contextily merges {"user-agent": random_id, **headers}; lowercase overrides it.
    return {"user-agent": ua}


def get_timezone(preferred_tz_name=None):
    """Get timezone: preferred -> TIMEZONE -> TZ -> Europe/Belgrade."""
    tz_name = preferred_tz_name or os.getenv('TIMEZONE') or os.getenv('TZ', 'Europe/Belgrade')
    try:
        return pytz.timezone(tz_name)
    except pytz.exceptions.UnknownTimeZoneError:
        print(f"⚠️ Unknown timezone: {tz_name}, using Europe/Belgrade")
        return pytz.timezone('Europe/Belgrade')


def detect_timezone_from_points(points):
    """Detect IANA timezone using first route point."""
    if not points:
        return None
    first = points[0]
    lat = first.get('lat')
    lon = first.get('lon')
    if lat is None or lon is None:
        return None
    try:
        tf = TimezoneFinder()
        return tf.timezone_at(lat=lat, lng=lon)
    except Exception as e:
        print(f"⚠️ Failed to detect timezone by coordinate: {e}")
        return None


def detect_timezone_from_gpx(gpx_file):
    """Detect IANA timezone directly from GPX file."""
    try:
        points = get_route_points_with_time(gpx_file)
        return detect_timezone_from_points(points)
    except Exception as e:
        print(f"⚠️ Failed to detect timezone from GPX: {e}")
        return None

def get_route_points_with_time(gpx_file):
    """Получает точки маршрута с временными метками"""
    with open(gpx_file, 'r', encoding='utf-8') as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                if point.time:
                    points.append({
                        'lat': point.latitude,
                        'lon': point.longitude,
                        'time': point.time,
                        'ele': point.elevation if point.elevation else 0
                    })
    
    # print(f"📍 Загружено {len(points)} точек маршрута с временными метками")  # Убрано для чистоты вывода
    return points

def calculate_route_time_points(points, start_time, speed_kmh=27, timezone_name=None):
    """Вычисляет точки маршрута через равные интервалы времени"""
    if not points:
        return []
    
    # Получаем временную зону
    tz = get_timezone(timezone_name)
    
    # Конвертируем start_time в нужную временную зону
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    
    # Конвертируем скорость в км/ч в м/с
    speed_ms = speed_kmh * 1000 / 3600
    
    # Вычисляем общее время маршрута
    total_distance = 0
    for i in range(1, len(points)):
        lat1, lon1 = points[i-1]['lat'], points[i-1]['lon']
        lat2, lon2 = points[i]['lat'], points[i]['lon']
        distance = calculate_distance(lat1, lon1, lat2, lon2)
        total_distance += distance
    
    # print(f"📏 Общая дистанция: {total_distance/1000:.2f} км")  # Убрано для чистоты вывода
    # print(f"⏱️  Время маршрута: {total_distance/speed_ms/3600:.2f} часов")  # Убрано для чистоты вывода
    
    # Разбиваем маршрут на интервалы по 6 км, включая старт (0 км) и финиш.
    interval_distance_km = 6.0  # 6 км между точками
    interval_distance_m = interval_distance_km * 1000
    route_points = []

    # Стартовая точка маршрута (0 км) обязательна, иначе на карте "обрезается" начало трека.
    route_points.append({
        'lat': points[0]['lat'],
        'lon': points[0]['lon'],
        'time': start_time,
        'distance_km': 0.0,
        'ele': points[0].get('ele', 0)
    })

    target_distance = interval_distance_m
    while target_distance < total_distance:
        
        # Находим точку на нужном расстоянии
        accumulated_distance = 0
        for j in range(1, len(points)):
            lat1, lon1 = points[j-1]['lat'], points[j-1]['lon']
            lat2, lon2 = points[j]['lat'], points[j]['lon']
            segment_distance = calculate_distance(lat1, lon1, lat2, lon2)
            
            if accumulated_distance + segment_distance >= target_distance:
                # Интерполируем точку на сегменте
                ratio = (target_distance - accumulated_distance) / segment_distance
                lat = lat1 + (lat2 - lat1) * ratio
                lon = lon1 + (lon2 - lon1) * ratio
                
                # Вычисляем время для этой точки
                time_offset = target_distance / speed_ms
                point_time = start_time + timedelta(seconds=time_offset)
                
                # Находим высоту для этой точки (интерполируем)
                ele = 0
                if j > 0 and j < len(points):
                    ele1 = points[j-1]['ele'] if 'ele' in points[j-1] else 0
                    ele2 = points[j]['ele'] if 'ele' in points[j] else 0
                    ele = ele1 + (ele2 - ele1) * ratio
                
                route_points.append({
                    'lat': lat,
                    'lon': lon,
                    'time': point_time,
                    'distance_km': target_distance / 1000,
                    'ele': ele
                })
                break
            
            accumulated_distance += segment_distance

        target_distance += interval_distance_m

    # Всегда добавляем финишную точку, если её ещё нет в выборке.
    finish_distance_km = total_distance / 1000
    if route_points[-1]['distance_km'] < finish_distance_km:
        finish_time = start_time + timedelta(seconds=total_distance / speed_ms)
        route_points.append({
            'lat': points[-1]['lat'],
            'lon': points[-1]['lon'],
            'time': finish_time,
            'distance_km': finish_distance_km,
            'ele': points[-1].get('ele', 0)
        })

    return route_points

def calculate_distance(lat1, lon1, lat2, lon2):
    """Вычисляет расстояние между двумя точками в метрах (формула Haversine)"""
    R = 6371000  # Радиус Земли в метрах
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_lat / 2) ** 2 + 
         math.cos(lat1_rad) * math.cos(lat2_rad) * 
         math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

def latlon_to_web_mercator(lat, lon):
    """Конвертирует WGS84 (lat/lon) в Web Mercator (EPSG:3857)."""
    # Ограничение широты для устойчивости проекции
    lat = max(min(lat, 85.05112878), -85.05112878)
    r = 6378137.0
    x = r * math.radians(lon)
    y = r * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y

def route_map_bounds(xs, ys, pad_frac=0.14, min_pad_frac=0.06):
    """Mercator bounds with proportional padding around the route."""
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    x_range = max(max_x - min_x, 1.0)
    y_range = max(max_y - min_y, 1.0)
    dominant = max(x_range, y_range)
    min_pad = dominant * min_pad_frac
    x_pad = max(x_range * pad_frac, min_pad)
    y_pad = max(y_range * pad_frac, min_pad)
    return min_x - x_pad, max_x + x_pad, min_y - y_pad, max_y + y_pad


def expand_bounds_to_axes_aspect(min_x, max_x, min_y, max_y, axes_ratio):
    """Pad bounds with neighboring map area so OSM tiles fill the whole panel."""
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    data_ratio = width / height

    if data_ratio > axes_ratio:
        target_height = width / axes_ratio
        pad = (target_height - height) / 2.0
        min_y -= pad
        max_y += pad
    else:
        target_width = height * axes_ratio
        pad = (target_width - width) / 2.0
        min_x -= pad
        max_x += pad

    return min_x, max_x, min_y, max_y


def map_panel(fig, x, y, w, h, inner_pad=0.006):
    """Map card without title — axes use almost the full panel area."""
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
        facecolor=CARD,
        zorder=2,
    )
    fig.patches.extend([shadow, panel])
    ax = fig.add_axes([x + inner_pad, y + inner_pad, w - 2 * inner_pad, h - 2 * inner_pad], zorder=3)
    return ax, (x, y, w, h)


# Figure layout rhythm copied from ride_poster.render_poster.
_LAYOUT = {
    "margin_x": 0.05,
    "panel_w": 0.90,
    "cards_y": 0.700,
    "cards_h": 0.085,
    "section_gap": 0.035,
    "block_gap": 0.040,
    "bottom_y": 0.08,
    "footer_y": 0.035,
    # Middle row: two charts side-by-side (same geometry as map + climbs on the poster).
    "charts_y": 0.39,
    "charts_h": 0.275,
    "charts_left_w": 0.54,
    "charts_right_x": 0.61,
    "charts_right_w": 0.34,
    "map_h": 0.27,
}

def get_weather_data_for_route(route_points):
    """Получает данные о погоде для всех точек маршрута"""
    if not route_points:
        return []

    cache_session = requests_cache.CachedSession('.cache', expire_after=3600)
    retry_session = retry(cache_session, retries=3, backoff_factor=0.2)
    openmeteo = openmeteo_requests.Client(session=retry_session)
    
    weather_data = []
    
    # Определяем временной диапазон заезда
    start_time = min(point['time'] for point in route_points)
    end_time = max(point['time'] for point in route_points)
    
    # Добавляем небольшой буфер (1 час до и после)
    buffer = timedelta(hours=1)
    start_time = start_time - buffer
    end_time = end_time + buffer
    
    for i, point in enumerate(route_points):
        # print(f"🌪️  Получение данных о погоде {i+1}/{len(route_points)}...")  # Убрано для чистоты вывода
        
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": point['lat'],
            "longitude": point['lon'],
            "hourly": [
                "temperature_2m",
                "apparent_temperature", 
                "relative_humidity_2m",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
                "pressure_msl",
                "weather_code",
                "precipitation",
                "precipitation_probability",
                "cloud_cover"
            ],
            "timezone": "auto",
            "start_date": start_time.strftime('%Y-%m-%d'),
            "end_date": end_time.strftime('%Y-%m-%d')
        }
        
        try:
            responses = openmeteo.weather_api(url, params=params)
            response = responses[0]
            
            hourly = response.Hourly()
            hourly_time = range(hourly.Time(), hourly.TimeEnd(), hourly.Interval())
            
            # Находим ближайший час
            target_timestamp = int(point['time'].timestamp())
            closest_time = None
            min_diff = float('inf')
            
            for j, timestamp in enumerate(hourly_time):
                time_diff = abs(timestamp - target_timestamp)
                if time_diff < min_diff:
                    min_diff = time_diff
                    closest_time = j
            
            if closest_time is None:
                weather_data.append(None)
                continue
            
            # Получаем данные для найденного времени
            hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()
            hourly_apparent_temperature = hourly.Variables(1).ValuesAsNumpy()
            hourly_relative_humidity_2m = hourly.Variables(2).ValuesAsNumpy()
            hourly_wind_speed_10m = hourly.Variables(3).ValuesAsNumpy()
            hourly_wind_direction_10m = hourly.Variables(4).ValuesAsNumpy()
            hourly_wind_gusts_10m = hourly.Variables(5).ValuesAsNumpy()
            hourly_pressure_msl = hourly.Variables(6).ValuesAsNumpy()
            hourly_weather_code = hourly.Variables(7).ValuesAsNumpy()
            hourly_precipitation = hourly.Variables(8).ValuesAsNumpy()
            hourly_precipitation_probability = hourly.Variables(9).ValuesAsNumpy()
            hourly_cloud_cover = hourly.Variables(10).ValuesAsNumpy()
            
            # Open-Meteo отдает wind_speed_10m в км/ч, если явно не запрошена другая единица.
            weather_data.append({
                'time': point['time'],
                'distance_km': point['distance_km'],
                'temperature': hourly_temperature_2m[closest_time],
                'feels_like': hourly_apparent_temperature[closest_time],
                'humidity': hourly_relative_humidity_2m[closest_time],
                'wind_speed': hourly_wind_speed_10m[closest_time],
                'wind_direction': hourly_wind_direction_10m[closest_time],
                'wind_gusts': hourly_wind_gusts_10m[closest_time],
                'pressure': hourly_pressure_msl[closest_time],
                'weather_code': int(hourly_weather_code[closest_time]),
                'precipitation_mm': hourly_precipitation[closest_time],
                'precipitation_probability': hourly_precipitation_probability[closest_time],
                'cloud_cover': hourly_cloud_cover[closest_time]
            })
            
        except Exception as e:
            # print(f"❌ Ошибка получения данных о ветре: {e}")  # Убрано для чистоты вывода
            weather_data.append(None)
    
    return weather_data


def _init_dashboard_figure():
    width_px, height_px, dpi = 1600, 2000, 160
    fig = plt.figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    rp._SCALE = math.sqrt((width_px / dpi) * (height_px / dpi) / rp._REF_AREA_IN2)
    fig.patch.set_facecolor(BG)
    fig.patches.append(fig_circle(fig, 0.91, 0.92, 0.12, facecolor=PRIMARY_SOFT, edgecolor="none", zorder=0))
    fig.patches.append(fig_circle(fig, 0.09, 0.10, 0.09, facecolor=SECONDARY_SOFT, edgecolor="none", zorder=0))
    return fig, dpi


def _style_chart_ax(ax):
    ax.set_facecolor(CARD)
    ax.grid(True, axis="y", color=BORDER, linewidth=lw(0.9))
    ax.grid(False, axis="x")
    ax.tick_params(axis="both", length=0, pad=3, colors=SUBTLE, labelsize=fs(9))
    for spine in ax.spines.values():
        spine.set_visible(False)


def _format_time_axis(ax, plot_tz):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=plot_tz))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, fontsize=fs(8), color=SUBTLE)


def _flat_legend(ax, loc="upper left"):
    leg = ax.legend(
        loc=loc,
        fontsize=fs(8),
        frameon=True,
        framealpha=1.0,
        facecolor=CARD,
        edgecolor=BORDER,
        labelcolor=INK,
    )
    leg.set_zorder(20)
    return leg


def _fmt_duration(start, end) -> str:
    secs = max(0, (end - start).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)} min"
    hours = secs / 3600
    return f"{hours:.1f} h" if hours < 10 else f"{int(round(hours))} h"


def draw_weather_hero(fig, *, title, subtitle, date_str, time_str, speed_kmh):
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

    chip_x, chip_w = 0.66, 0.26
    chip_margin, chip_gap = 0.012, 0.007
    vis_h = (hh - 2.0 * chip_margin - 2.0 * chip_gap) / 3.0
    chip_h = vis_h - 2.0 * CHIP_PAD
    chip_ys = [hy + hh - chip_margin - CHIP_PAD - chip_h - i * (vis_h + chip_gap) for i in range(3)]
    add_info_chip(fig, chip_x, chip_ys[0], chip_w, chip_h, "Date", date_str)
    add_info_chip(fig, chip_x, chip_ys[1], chip_w, chip_h, "Time", time_str)
    speed_label = f"{speed_kmh:g} km/h" if speed_kmh is not None else "—"
    add_info_chip(fig, chip_x, chip_ys[2], chip_w, chip_h, "Speed", speed_label)

    text_w = chip_x - 0.018 - inner_left
    title_top = hy + hh - 0.012
    title_obj = fig_fit_text(
        fig,
        inner_left,
        title_top,
        title,
        fontsize=fs(26.5),
        max_w=text_w,
        max_h=max(0.02, title_top - inner_bottom - 0.016),
        max_lines=2,
        color=CARD,
        fontweight="bold",
        va="top",
        zorder=4,
        linespacing=1.04,
    )
    if subtitle:
        sub_top = title_top - text_h_fig(fig, title_obj) - 0.009
        fig_fit_text(
            fig,
            inner_left,
            sub_top,
            subtitle,
            fontsize=fs(12.5),
            max_w=text_w,
            max_h=max(0.012, sub_top - inner_bottom),
            max_lines=2,
            color=(1, 1, 1, 0.88),
            va="top",
            zorder=4,
            linespacing=1.12,
        )


def create_weather_dashboard(
    route_points,
    weather_data,
    output_path="weather_dashboard.png",
    route_length_km=None,
    timezone_name=None,
    speed_kmh=None,
    title="WEATHER DASHBOARD",
    subtitle=None,
):
    """Создает дашборд с графиками погоды в flat/material стиле ride_poster."""
    fig, dpi = _init_dashboard_figure()
    
    # Фильтруем данные (убираем None)
    valid_data = [(p, w) for p, w in zip(route_points, weather_data) if w is not None]
    if not valid_data:
        print("❌ No weather data available to build dashboard")
        plt.close(fig)
        return False
    
    route_points_clean, weather_data_clean = zip(*valid_data)
    
    # Вычисляем длину маршрута, если не передана
    if route_length_km is None:
        total_distance = 0
        for i in range(1, len(route_points_clean)):
            lat1, lon1 = route_points_clean[i-1]['lat'], route_points_clean[i-1]['lon']
            lat2, lon2 = route_points_clean[i]['lat'], route_points_clean[i]['lon']
            distance = calculate_distance(lat1, lon1, lat2, lon2)
            total_distance += distance
        route_length_km = total_distance / 1000
    
    times = [w['time'] for w in weather_data_clean]
    # Use timezone detected from GPX as a single source of truth for chart X axes.
    # Fallback to first point tzinfo, then env-based timezone.
    plot_tz = get_timezone(timezone_name)
    if not timezone_name and times and getattr(times[0], "tzinfo", None):
        plot_tz = times[0].tzinfo
    
    start_time = min(times)
    end_time = max(times)
    date_str = start_time.strftime("%d.%m.%Y")
    time_str = start_time.strftime("%H:%M")
    tz_label = timezone_name or getattr(plot_tz, "zone", str(plot_tz))
    hero_subtitle = subtitle or f"{fmt_km(route_length_km)} · {_fmt_duration(start_time, end_time)} · {tz_label}"

    draw_weather_hero(
        fig,
        title=title,
        subtitle=hero_subtitle,
        date_str=date_str,
        time_str=time_str,
        speed_kmh=speed_kmh,
    )

    temperatures = [w["temperature"] for w in weather_data_clean]
    feels_like_vals = [w["feels_like"] for w in weather_data_clean]
    wind_speeds = [w["wind_speed"] for w in weather_data_clean]
    ly = _LAYOUT
    cards_y, cards_h = ly["cards_y"], ly["cards_h"]
    temp_range = f"{min(temperatures):.0f}–{max(temperatures):.0f} °C"
    feels_range = f"{min(feels_like_vals):.0f}–{max(feels_like_vals):.0f} °C"
    draw_stat_card(fig, 0.050, cards_y, 0.210, cards_h, "Distance", fmt_km(route_length_km), PRIMARY, PRIMARY_SOFT)
    draw_stat_card(fig, 0.284, cards_y, 0.210, cards_h, "Temperature", temp_range, SECONDARY, SECONDARY_SOFT, value_fontsize=fs(17))
    draw_stat_card(fig, 0.518, cards_y, 0.432, cards_h, "Feels Like", feels_range, ACCENT, ACCENT_SOFT, value_fontsize=fs(17))

    panel_x, panel_w = ly["margin_x"], ly["panel_w"]
    charts_y, charts_h = ly["charts_y"], ly["charts_h"]
    map_y, map_h = ly["bottom_y"], ly["map_h"]

    ax_precip, _ = rounded_panel(
        fig, panel_x, charts_y, ly["charts_left_w"], charts_h, "Precipitation", footer_ratio=0.12
    )
    ax_wind, _ = rounded_panel(
        fig, ly["charts_right_x"], charts_y, ly["charts_right_w"], charts_h, "Wind", footer_ratio=0.12
    )
    ax_map, _ = map_panel(fig, panel_x, map_y, panel_w, map_h)

    cloud_cover = [min(100, max(0, w["cloud_cover"])) for w in weather_data_clean]

    # Precipitation
    precipitation_mm = [max(0, w['precipitation_mm']) for w in weather_data_clean]
    max_precipitation_mm = max(precipitation_mm) if precipitation_mm else 0
    precipitation_ymax = max(1, max_precipitation_mm * 1.3)
    precipitation_probability_raw = [
        min(100, max(0, w['precipitation_probability']))
        for w in weather_data_clean
    ]
    precipitation_probability = [
        probability if precipitation > 0 else 0
        for precipitation, probability in zip(precipitation_mm, precipitation_probability_raw)
    ]
    precip_bar_width_days = 1 / 24  # fallback: 1 hour
    if len(times) > 1:
        positive_steps = sorted(
            (times[i + 1] - times[i]).total_seconds() / 86400
            for i in range(len(times) - 1)
            if (times[i + 1] - times[i]).total_seconds() > 0
        )
        if positive_steps:
            precip_bar_width_days = positive_steps[len(positive_steps) // 2] * 0.8
    
    ax_precip_twin = ax_precip.twinx()
    ax_precip_twin.bar(
        times,
        precipitation_probability,
        alpha=0.75,
        color=MUTED,
        label="Precipitation probability (%)",
        width=precip_bar_width_days,
        zorder=1,
    )
    ax_precip_twin.set_ylim(0, 100)
    ax_precip_twin.set_xlim(min(times), max(times))
    ax_precip_twin.plot(
        times,
        cloud_cover,
        color=SUBTLE,
        linewidth=lw(2.0),
        linestyle="--",
        label="Cloud cover (%)",
        zorder=3,
    )
    ax_precip.bar(
        times,
        precipitation_mm,
        alpha=0.95,
        color=PRIMARY,
        label="Precipitation (mm)",
        width=precip_bar_width_days * 0.62,
        zorder=5,
    )
    ax_precip.set_zorder(ax_precip_twin.get_zorder() + 1)
    ax_precip.patch.set_alpha(0)
    ax_precip.set_ylim(0, precipitation_ymax)
    ax_precip.set_xlim(min(times), max(times))
    _style_chart_ax(ax_precip)
    for spine in ax_precip_twin.spines.values():
        spine.set_visible(False)
    ax_precip_twin.tick_params(axis="y", length=0, pad=3, colors=MUTED, labelsize=fs(8))
    ax_precip_twin.grid(False)
    lines1, labels1 = ax_precip.get_legend_handles_labels()
    lines2, labels2 = ax_precip_twin.get_legend_handles_labels()
    legend_items = dict(zip(labels1 + labels2, lines1 + lines2))
    legend_order = [
        "Precipitation (mm)",
        "Precipitation probability (%)",
        "Cloud cover (%)",
    ]
    precip_legend = ax_precip.legend(
        [legend_items[label] for label in legend_order if label in legend_items],
        [label for label in legend_order if label in legend_items],
        loc="upper left",
        fontsize=fs(7.5),
        frameon=True,
        framealpha=1.0,
        facecolor=CARD,
        edgecolor=BORDER,
        labelcolor=INK,
    )
    precip_legend.set_zorder(20)
    _format_time_axis(ax_precip, plot_tz)

    wind_gusts = [max(0, w["wind_gusts"]) for w in weather_data_clean]
    wind_ymax = max(1, max(max(wind_speeds), max(wind_gusts)) * 1.2)

    ax_wind.bar(
        times,
        wind_gusts,
        alpha=0.75,
        color=MUTED,
        label="Wind gusts (km/h)",
        width=precip_bar_width_days,
        zorder=1,
    )
    ax_wind.plot(times, wind_speeds, color=PRIMARY, linewidth=lw(3.2), label="Wind (km/h)", zorder=5)
    ax_wind.set_ylim(0, wind_ymax)
    _style_chart_ax(ax_wind)
    _flat_legend(ax_wind)
    _format_time_axis(ax_wind, plot_tz)
    ax_wind.set_xlim(min(times), max(times))

    # Wind direction map — OSM basemap in Web Mercator.
    print(f"🔍 Route length: {route_length_km:.2f} km")

    lats = [p["lat"] for p in route_points_clean]
    lons = [p["lon"] for p in route_points_clean]
    merc_points = [latlon_to_web_mercator(lat, lon) for lat, lon in zip(lats, lons)]
    xs = [p[0] for p in merc_points]
    ys = [p[1] for p in merc_points]
    x_range = max(xs) - min(xs)
    y_range = max(ys) - min(ys)

    if route_length_km < 20:
        route_arrow_scale = 450.0
    elif route_length_km < 100:
        route_arrow_scale = 700.0
    elif route_length_km < 200:
        route_arrow_scale = 950.0
    else:
        route_arrow_scale = 1100.0

    map_min_x, map_max_x, map_min_y, map_max_y = route_map_bounds(xs, ys)
    fig_w, fig_h = fig.get_size_inches()
    ax_pos = ax_map.get_position()
    axes_ratio = (ax_pos.width * fig_w) / (ax_pos.height * fig_h)
    map_min_x, map_max_x, map_min_y, map_max_y = expand_bounds_to_axes_aspect(
        map_min_x, map_max_x, map_min_y, map_max_y, axes_ratio
    )
    ax_map.set_xlim(map_min_x, map_max_x)
    ax_map.set_ylim(map_min_y, map_max_y)
    ax_map.set_aspect("equal", adjustable="box")

    if ctx is not None:
        try:
            ctx.add_basemap(
                ax_map,
                source=ctx.providers.OpenStreetMap.Mapnik,
                crs="EPSG:3857",
                attribution="© OpenStreetMap contributors",
                zoom="auto",
                headers=osm_tile_headers(),
            )
        except Exception as e:
            print(f"⚠️ Failed to load OSM basemap: {e}")
    else:
        print("⚠️ OSM basemap unavailable: contextily is missing or failed to load")

    ax_map.plot(xs, ys, color=PRIMARY_SOFT, linewidth=lw(10), solid_capstyle="round", zorder=6)
    ax_map.plot(xs, ys, color=PRIMARY, linewidth=lw(4.2), solid_capstyle="round", zorder=7)

    target_route_arrows = max(14, min(64, int(route_length_km / 2.6) + 10))
    arrow_step = max(1, len(xs) // target_route_arrows)
    for i in range(0, len(xs) - 1, arrow_step):
        dx_route = xs[i + 1] - xs[i]
        dy_route = ys[i + 1] - ys[i]
        length = math.hypot(dx_route, dy_route)
        if length <= 0:
            continue
        dx_route = dx_route / length * route_arrow_scale
        dy_route = dy_route / length * route_arrow_scale
        head_size = route_arrow_scale * (0.65 if route_length_km < 20 else 0.7 if route_length_km < 100 else 0.8)
        ax_map.arrow(
            xs[i], ys[i], dx_route, dy_route,
            head_width=head_size * 1.2, head_length=head_size * 1.2,
            fc=INK, ec=INK, linewidth=lw(3.2), alpha=0.45,
            length_includes_head=True, zorder=8,
        )
        ax_map.arrow(
            xs[i], ys[i], dx_route, dy_route,
            head_width=head_size, head_length=head_size,
            fc=ACCENT, ec=ACCENT, linewidth=lw(2.0), alpha=0.95,
            length_includes_head=True, zorder=9,
        )

    wind_arrow_len_pts = 22.0
    for i, (point, weather) in enumerate(zip(route_points_clean, weather_data_clean)):
        if not weather or weather["wind_speed"] <= 0:
            continue
        px, py = latlon_to_web_mercator(point["lat"], point["lon"])
        wind_to_rad = math.radians((float(weather["wind_direction"]) + 180.0) % 360.0)
        dx_pts = wind_arrow_len_pts * math.sin(wind_to_rad)
        dy_pts = wind_arrow_len_pts * math.cos(wind_to_rad)
        for color, lw_val, alpha, scale in (
            (INK, lw(3.2), 0.45, 15),
            (CARD, lw(2.2), 0.95, 12),
        ):
            ax_map.annotate(
                "",
                xy=(px, py),
                xycoords="data",
                xytext=(-dx_pts, -dy_pts),
                textcoords="offset points",
                arrowprops=dict(
                    arrowstyle="-|>",
                    color=color,
                    lw=lw_val,
                    mutation_scale=scale,
                    alpha=alpha,
                    shrinkA=0,
                    shrinkB=0,
                ),
                zorder=11,
            )

    marker_r = max(x_range, y_range, 1.0) * 0.018
    start_x, start_y = xs[0], ys[0]
    finish_x, finish_y = xs[-1], ys[-1]
    if math.hypot(finish_x - start_x, finish_y - start_y) < max(x_range, y_range) * 0.06:
        ax_map.add_patch(
            Circle((start_x, start_y), marker_r * 1.15, facecolor=ACCENT, edgecolor=CARD, linewidth=lw(2.5), zorder=15)
        )
        ax_map.add_patch(
            Circle((start_x, start_y), marker_r * 0.65, facecolor=SECONDARY, edgecolor="none", zorder=16)
        )
        ax_map.text(start_x, start_y, "S/F", ha="center", va="center", fontsize=fs(9), color=CARD, fontweight="bold", zorder=17)
    else:
        ax_map.add_patch(
            Circle((start_x, start_y), marker_r, facecolor=SECONDARY, edgecolor=CARD, linewidth=lw(2.2), zorder=15)
        )
        ax_map.text(start_x, start_y, "S", ha="center", va="center", fontsize=fs(9), color=CARD, fontweight="bold", zorder=16)
        ax_map.add_patch(
            Circle((finish_x, finish_y), marker_r * 0.92, facecolor=ACCENT, edgecolor=CARD, linewidth=lw(2.2), zorder=15)
        )
        ax_map.text(finish_x, finish_y, "F", ha="center", va="center", fontsize=fs(9), color=CARD, fontweight="bold", zorder=16)

    ax_map.set_facecolor(CARD)
    ax_map.set_xticks([])
    ax_map.set_yticks([])
    for spine in ax_map.spines.values():
        spine.set_visible(False)
    ax_map.grid(False)
    map_legend = ax_map.legend(
        handles=[
            plt.Line2D([0], [0], color=PRIMARY, linewidth=lw(4), label="Route"),
            plt.Line2D([0], [0], color=INK, linewidth=lw(2), label="Wind"),
        ],
        loc="upper right",
        fontsize=fs(8),
        frameon=True,
        framealpha=1.0,
        facecolor=CARD,
        edgecolor=BORDER,
        labelcolor=INK,
    )
    map_legend.set_zorder(20)

    fig_fit_text(
        fig,
        ly["margin_x"],
        ly["footer_y"],
        "@gpx_weather_bot · OpenStreetMap · Open-Meteo",
        fontsize=fs(9.6),
        max_w=0.70,
        color=SUBTLE,
    )
    fig.text(0.95, ly["footer_y"], "flat / material", ha="right", fontsize=fs(9.6), color=MUTED)

    fig.savefig(output_path, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    
    print(f"✅ Dashboard saved to: {output_path}")
    return True

def main():
    tz_for_default = get_timezone()
    default_start = datetime.now(tz_for_default) + timedelta(days=1)
    default_date = default_start.strftime('%d.%m.%Y')
    default_time = '08:30'

    parser = argparse.ArgumentParser(description='Weather dashboard for cycling routes')
    parser.add_argument('gpx_file', help='Path to GPX file')
    parser.add_argument('-o', '--output', default='weather_dashboard.png',
                       help='Output image path (default: weather_dashboard.png)')
    parser.add_argument('-s', '--speed', type=float, default=27.0,
                       help='Riding speed in km/h (default: 27)')
    parser.add_argument('-d', '--date', default=default_date,
                       help=f'Start date in DD.MM.YYYY format (default: {default_date})')
    parser.add_argument('-t', '--time', default=default_time,
                       help=f'Start time in HH:MM format (default: {default_time})')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.gpx_file):
        print(f"❌ File {args.gpx_file} not found!")
        sys.exit(1)
    
    print("🌤️  Building weather dashboard for route")
    print(f"📁 File: {args.gpx_file}")
    print(f"🖼️  Output: {args.output}")
    print(f"🚗 Speed: {args.speed} km/h")
    print()
    
    # Получаем точки маршрута
    points = get_route_points_with_time(args.gpx_file)
    if not points:
        print("❌ Failed to read route points")
        sys.exit(1)

    detected_timezone = detect_timezone_from_points(points)
    tz = get_timezone(detected_timezone)
    if detected_timezone:
        print(f"🕓 Route timezone detected: {detected_timezone}")
    else:
        print("🕓 Failed to detect timezone from GPX, using TIMEZONE/TZ from environment")
    
    # Парсим дату и время из аргументов
    try:
        date_parts = args.date.split('.')
        if len(date_parts) != 3:
            raise ValueError("Invalid date format")
        
        day, month, year = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
        
        time_parts = args.time.split(':')
        if len(time_parts) != 2:
            raise ValueError("Invalid time format")
        
        hour, minute = int(time_parts[0]), int(time_parts[1])
        
        start_time = datetime(year, month, day, hour, minute, 0)
        print(f"🕐 Start time: {start_time.strftime('%Y-%m-%d %H:%M')}")
        
    except (ValueError, IndexError) as e:
        print(f"❌ Date/time format error: {e}")
        print("Use format: -d DD.MM.YYYY -t HH:MM")
        sys.exit(1)
    
    # Вычисляем точки маршрута через равные интервалы
    route_points = calculate_route_time_points(points, start_time, args.speed, detected_timezone)
    print(f"📍 Weather sample points: {len(route_points)} (every 6 km)")
    
    # Получаем данные о погоде
    weather_data = get_weather_data_for_route(route_points)
    
    # Вычисляем длину маршрута
    total_distance = 0
    for i in range(1, len(route_points)):
        lat1, lon1 = route_points[i-1]['lat'], route_points[i-1]['lon']
        lat2, lon2 = route_points[i]['lat'], route_points[i]['lon']
        distance = calculate_distance(lat1, lon1, lat2, lon2)
        total_distance += distance
    route_length_km = total_distance / 1000
    
    # Создаем дашборд
    success = create_weather_dashboard(
        route_points,
        weather_data,
        args.output,
        route_length_km,
        detected_timezone,
        speed_kmh=args.speed,
    )
    
    if success:
        print("\n🎉 Done! Weather dashboard created.")
        print("📊 Dashboard contains:")
        print("   💨 Wind speed graph")
        print("   🌡️  Temperature graph")
        print("   🗺️  Route map with wind direction")
    else:
        print("\n❌ Failed to create dashboard")

if __name__ == "__main__":
    main()
