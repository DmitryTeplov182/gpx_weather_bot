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

from matplotlib.patches import FancyBboxPatch
import pytz
from timezonefinder import TimezoneFinder

from ride_poster import BORDER, CARD, SHADOW, lw

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


def gpx_route_name(gpx_file):
    """Route name from GPX metadata/track, or '' when absent."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(gpx_file).getroot()
        for xpath in ("{*}metadata/{*}name", "{*}trk/{*}name"):
            el = root.find(xpath)
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
    except Exception as e:
        print(f"⚠️ Failed to read route name from GPX: {e}")
    return ""


def main():
    tz_for_default = get_timezone()
    default_start = datetime.now(tz_for_default) + timedelta(days=1)
    default_date = default_start.strftime('%d.%m.%Y')
    default_time = '08:30'

    parser = argparse.ArgumentParser(description='Weather dashboard for cycling routes (rendered by ride_dashboard.py)')
    parser.add_argument('gpx_file', help='Path to GPX file')
    parser.add_argument('-o', '--output', default='weather_dashboard.png',
                       help='Output image path (default: weather_dashboard.png)')
    parser.add_argument('-s', '--speed', type=float, default=27.0,
                       help='Riding speed in km/h (default: 27)')
    parser.add_argument('-d', '--date', default=default_date,
                       help=f'Start date in DD.MM.YYYY format (default: {default_date})')
    parser.add_argument('-t', '--time', default=default_time,
                       help=f'Start time in HH:MM format (default: {default_time})')
    parser.add_argument('--title', default=None, help='Route name shown in the header (default: from GPX)')

    args = parser.parse_args()

    if not os.path.exists(args.gpx_file):
        print(f"❌ File {args.gpx_file} not found!")
        sys.exit(1)

    try:
        day, month, year = (int(v) for v in args.date.split('.'))
        hour, minute = (int(v) for v in args.time.split(':'))
        start_naive = datetime(year, month, day, hour, minute, 0)
    except (ValueError, IndexError) as e:
        print(f"❌ Date/time format error: {e}")
        print("Use format: -d DD.MM.YYYY -t HH:MM")
        sys.exit(1)

    print("🌤️  Building weather dashboard for route")
    print(f"📁 File: {args.gpx_file}")
    print(f"🖼️  Output: {args.output}")
    print(f"🚗 Speed: {args.speed} km/h")
    print(f"🕐 Start time: {start_naive.strftime('%Y-%m-%d %H:%M')} (route timezone)")

    # Imported here: ride_dashboard imports this module for the data helpers.
    from pathlib import Path
    from ride_dashboard import default_config, render

    cfg = default_config()
    cfg.update({
        'kicker': 'WEATHER CHECK',
        'route_name': args.title or gpx_route_name(args.gpx_file),
        'start_iso': start_naive.isoformat(),   # naive: localised to the route timezone by the renderer
        'speed_kmh': args.speed,
        'speed_range': [args.speed, args.speed],
        'weather': True,
    })
    if render(cfg, Path(args.gpx_file), Path(args.output)):
        print("\n🎉 Done! Weather dashboard created.")
    else:
        print("\n❌ Failed to create dashboard")
        sys.exit(1)

if __name__ == "__main__":
    main()
