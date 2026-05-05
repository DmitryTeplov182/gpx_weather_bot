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
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz
from timezonefinder import TimezoneFinder

try:
    import contextily as ctx
except ImportError:
    ctx = None

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

def fit_bounds_to_axes_aspect(min_x, max_x, min_y, max_y, axes_ratio):
    """Расширяет короткую ось, чтобы bbox карты совпадал с пропорциями осей."""
    width = max_x - min_x
    height = max_y - min_y

    # Защита от вырожденных случаев
    width = max(width, 1.0)
    height = max(height, 1.0)

    data_ratio = width / height

    if data_ratio > axes_ratio:
        # Трек более "горизонтальный": расширяем Y
        target_height = width / axes_ratio
        pad = (target_height - height) / 2.0
        min_y -= pad
        max_y += pad
    else:
        # Трек более "вертикальный": расширяем X
        target_width = height * axes_ratio
        pad = (target_width - width) / 2.0
        min_x -= pad
        max_x += pad

    return min_x, max_x, min_y, max_y

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

def create_weather_dashboard(
    route_points,
    weather_data,
    output_path="weather_dashboard.png",
    route_length_km=None,
    timezone_name=None,
):
    """Создает дашборд с графиками погоды в стиле Epic Ride Weather"""
    
    # Настройка стиля matplotlib для светлой темы
    plt.style.use('default')
    plt.rcParams.update({
        'font.size': 10,
        'axes.titlesize': 12,
        'axes.labelsize': 10,
        'xtick.labelsize': 9,
        'ytick.labelsize': 9,
        'legend.fontsize': 8,
        'figure.titlesize': 14,
        'axes.facecolor': 'white',
        'figure.facecolor': 'white',
        'axes.edgecolor': '#cccccc',
        'text.color': '#333333',
        'axes.labelcolor': '#333333',
        'xtick.color': '#333333',
        'ytick.color': '#333333',
        'font.weight': 'bold'  # Делаем все шрифты жирными
    })
    
    # Создаем фигуру для мобильного формата (узкая и длинная)
    fig = plt.figure(figsize=(10, 10))
    fig.patch.set_facecolor('white')
    
    # Фильтруем данные (убираем None)
    valid_data = [(p, w) for p, w in zip(route_points, weather_data) if w is not None]
    if not valid_data:
        print("❌ No weather data available to build dashboard")
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
    distances = [w['distance_km'] for w in weather_data_clean]
    # Use timezone detected from GPX as a single source of truth for chart X axes.
    # Fallback to first point tzinfo, then env-based timezone.
    plot_tz = get_timezone(timezone_name)
    if not timezone_name and times and getattr(times[0], "tzinfo", None):
        plot_tz = times[0].tzinfo
    
    # Заголовок дашборда убран
    
    # 1. Temperature (верхний левый)
    ax1 = plt.subplot(3, 2, 1)
    temperatures = [w['temperature'] for w in weather_data_clean]
    feels_like = [w['feels_like'] for w in weather_data_clean]
    cloud_cover = [min(100, max(0, w['cloud_cover'])) for w in weather_data_clean]
    
    ax1.plot(times, temperatures, color='#1f77b4', linewidth=4, label='Temperature (°C)')
    ax1.plot(times, feels_like, color='#ff7f0e', linewidth=4, label='Feels Like (°C)')
    ax1.set_title('Temperature', fontweight='bold', color='#333333')
    ax1.legend(loc='upper left', fontsize=8)
    ax1.grid(True, alpha=0.3, linewidth=0.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=plot_tz))
    ax1.set_xlim(min(times), max(times))  # Ограничиваем ось X только временем заезда
    ax1.tick_params(colors='#333333')
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, fontsize=8)
    
    # 2. Precipitation (верхний правый)
    ax2 = plt.subplot(3, 2, 2)
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
    
    ax2_twin = ax2.twinx()
    ax2_twin.bar(
        times,
        precipitation_probability,
        alpha=0.6,
        color='#d4d9df',
        label='Precipitation Probability (%)',
        width=precip_bar_width_days,
        zorder=1,
    )
    ax2_twin.set_ylim(0, 100)
    ax2_twin.set_xlim(min(times), max(times))
    ax2_twin.plot(
        times,
        cloud_cover,
        color='#7f8c8d',
        linewidth=2.5,
        linestyle='--',
        label='Cloud Cover (%)',
        zorder=3,
    )

    # График осадков (столбчатая диаграмма)
    ax2.bar(
        times,
        precipitation_mm,
        alpha=0.9,
        color='#1f77b4',
        label='Precipitation (mm)',
        width=precip_bar_width_days * 0.62,
        zorder=5,
    )
    ax2.set_zorder(ax2_twin.get_zorder() + 1)
    ax2.patch.set_alpha(0)
    ax2.set_ylim(0, precipitation_ymax)
    ax2.set_xlim(min(times), max(times))  # Ограничиваем ось X только временем заезда
    ax2.set_title('Precipitation', fontweight='bold', color='#333333')

    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2_twin.get_legend_handles_labels()
    precip_legend = ax2.legend(
        lines1 + lines2,
        labels1 + labels2,
        loc='upper left',
        fontsize=8,
        framealpha=0.9,
        facecolor='white',
        edgecolor='gray',
    )
    precip_legend.set_zorder(20)
    ax2.grid(True, alpha=0.3, linewidth=0.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=plot_tz))
    ax2.tick_params(colors='#333333')
    ax2_twin.tick_params(colors='#9aa3ad')
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, fontsize=8)
    
    # 3. Wind Direction Map (занимает 2 строки - средний и нижний левый)
    ax3 = plt.subplot(3, 2, (3, 5))
    
    # Получаем координаты маршрута в Web Mercator для корректной подложки map tiles
    lats = [p['lat'] for p in route_points_clean]
    lons = [p['lon'] for p in route_points_clean]
    merc_points = [latlon_to_web_mercator(lat, lon) for lat, lon in zip(lats, lons)]
    xs = [p[0] for p in merc_points]
    ys = [p[1] for p in merc_points]

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    
    # Добавляем отступы
    x_range = max_x - min_x
    y_range = max_y - min_y
    min_margin_m = 1200.0

    x_margin = max(x_range * 0.12, min_margin_m)
    y_margin = max(y_range * 0.12, min_margin_m)
    
    # Адаптивные размеры элементов в зависимости от длины трека в км
    print(f"🔍 Route length: {route_length_km:.2f} km")
    
    if route_length_km < 20:  # Очень маленький трек (как example_route.gpx ~29км)
        route_arrow_scale = 450.0
        print("📏 Using layout for small route")
    elif route_length_km < 100:  # Средний трек
        route_arrow_scale = 700.0
        print("📏 Using layout for medium route")
    elif route_length_km < 200:  # Большой трек
        route_arrow_scale = 950.0
        print("📏 Using layout for large route")
    else:  # Очень большой трек (≥200км)
        route_arrow_scale = 1100.0
        print("📏 Using layout for very large route")
    
    # Сначала добавляем базовые отступы, затем выравниваем bbox под форму осей.
    map_min_x = min_x - x_margin
    map_max_x = max_x + x_margin
    map_min_y = min_y - y_margin
    map_max_y = max_y + y_margin

    fig = plt.gcf()
    fig_w, fig_h = fig.get_size_inches()
    ax_pos = ax3.get_position()
    axes_ratio = (ax_pos.width * fig_w) / (ax_pos.height * fig_h)

    map_min_x, map_max_x, map_min_y, map_max_y = fit_bounds_to_axes_aspect(
        map_min_x, map_max_x, map_min_y, map_max_y, axes_ratio
    )

    ax3.set_xlim(map_min_x, map_max_x)
    ax3.set_ylim(map_min_y, map_max_y)
    ax3.set_aspect('equal', adjustable='box')

    # Подложка OpenStreetMap Standard (если contextily установлен и тайлы доступны)
    if ctx is not None:
        try:
            ctx.add_basemap(
                ax3,
                source=ctx.providers.OpenStreetMap.Mapnik,
                crs="EPSG:3857",
                attribution=False,
                zoom="auto",
            )
        except Exception as e:
            print(f"⚠️ Failed to load OSM basemap: {e}")
    else:
        print("⚠️ OSM basemap unavailable: contextily is missing or failed to load")
    
    # Рисуем маршрут сплошной линией
    ax3.plot(xs, ys, '#ff6b6b', linewidth=3, zorder=6)
    
    # Добавляем стрелки направления на маршруте.
    # Чем длиннее маршрут, тем больше стрелок (меньше шаг между ними).
    target_route_arrows = max(14, min(64, int(route_length_km / 2.6) + 10))
    arrow_step = max(1, len(xs) // target_route_arrows)
    for i in range(0, len(xs)-1, arrow_step):
        if i + 1 < len(xs):
            # Вычисляем направление между точками
            dx_route = xs[i+1] - xs[i]
            dy_route = ys[i+1] - ys[i]
            length = math.sqrt(dx_route**2 + dy_route**2)
            
            if length > 0:
                # Нормализуем и масштабируем (адаптивная длина)
                dx_route = (dx_route / length) * route_arrow_scale
                dy_route = (dy_route / length) * route_arrow_scale
                
                # Рисуем стрелку направления (адаптивный размер головки)
                if route_length_km < 20:  # Маленький трек
                    head_size = route_arrow_scale * 0.65
                elif route_length_km < 100:  # Средний трек
                    head_size = route_arrow_scale * 0.7
                else:  # Большой трек
                    head_size = route_arrow_scale * 0.8
                # Рисуем стрелку маршрута в 2 слоя для контраста на карте:
                # темный контур + красная верхняя стрелка.
                ax3.arrow(
                    xs[i], ys[i], dx_route, dy_route,
                    head_width=head_size * 1.2,
                    head_length=head_size * 1.2,
                    fc='#1f1f1f',
                    ec='#1f1f1f',
                    linewidth=4.0,
                    alpha=0.55,
                    length_includes_head=True,
                    zorder=8
                )
                ax3.arrow(
                    xs[i], ys[i], dx_route, dy_route,
                    head_width=head_size,
                    head_length=head_size,
                    fc='#ff3b30',
                    ec='#ff3b30',
                    linewidth=2.2,
                    alpha=0.95,
                    length_includes_head=True,
                    zorder=9
                )
    
    # Фиксированная длина стрелки в экранных points.
    # Не зависит от длины/масштаба трека, поэтому одинакова на коротких и длинных маршрутах.
    wind_arrow_len_pts = 22.0

    # Рисуем стрелки ветра.
    # Open-Meteo wind_direction_10m - это "откуда дует" (meteorological).
    # Для визуализации направления потока переводим в "куда дует": +180°.
    for i, (point, weather) in enumerate(zip(route_points_clean, weather_data_clean)):
        if weather and weather['wind_speed'] > 0:  # Каждая точка с данными о ветре
            px, py = latlon_to_web_mercator(point['lat'], point['lon'])

            wind_to_deg = (float(weather['wind_direction']) + 180.0) % 360.0
            wind_to_rad = math.radians(wind_to_deg)

            # 0° = север, 90° = восток => dx=sin, dy=cos
            # Смещение в points, чтобы визуальная длина "ножки" была постоянной.
            dx_pts = wind_arrow_len_pts * math.sin(wind_to_rad)
            dy_pts = wind_arrow_len_pts * math.cos(wind_to_rad)

            # Рисуем стрелку в 2 слоя: темная подложка + светлая основа.
            # xy - наконечник стрелки в data-координатах, xytext - начало в offset points.
            ax3.annotate(
                "",
                xy=(px, py),
                xycoords="data",
                xytext=(-dx_pts, -dy_pts),
                textcoords="offset points",
                arrowprops=dict(
                    arrowstyle="-|>",
                    color="#2c2c2c",
                    lw=4.0,
                    mutation_scale=17,
                    alpha=0.55,
                    shrinkA=0,
                    shrinkB=0,
                ),
                zorder=11,
            )
            ax3.annotate(
                "",
                xy=(px, py),
                xycoords="data",
                xytext=(-dx_pts, -dy_pts),
                textcoords="offset points",
                arrowprops=dict(
                    arrowstyle="-|>",
                    color="white",
                    lw=2.6,
                    mutation_scale=14,
                    alpha=0.95,
                    shrinkA=0,
                    shrinkB=0,
                ),
                zorder=12,
            )
    
    # Точки начала и конца
    ax3.plot(xs[0], ys[0], 'go', markersize=8, label='Start', zorder=15)
    ax3.plot(xs[-1], ys[-1], 'ro', markersize=8, label='Finish', zorder=15)
    
    ax3.set_title('Wind Direction', fontweight='bold', color='#333333')
    ax3.set_xticks([])
    ax3.set_yticks([])
    ax3.legend(loc='upper right', fontsize=8, 
              framealpha=0.9, facecolor='white', edgecolor='gray')
    ax3.grid(False)
    ax3.text(
        0.5,
        -0.06,
        "@gpx_weather_bot Powered by OpenStreetMap and OpenWeatherAPI",
        transform=ax3.transAxes,
        ha='center',
        va='top',
        fontsize=8,
        color='black'
    )
    
    # 4. Wind (средний правый)
    ax4 = plt.subplot(3, 2, 4)
    wind_speeds = [w['wind_speed'] for w in weather_data_clean]  # км/ч
    wind_gusts = [max(0, w['wind_gusts']) for w in weather_data_clean]  # км/ч
    wind_ymax = max(1, max(max(wind_speeds), max(wind_gusts)) * 1.2)
    
    ax4.bar(
        times,
        wind_gusts,
        alpha=0.6,
        color='#d4d9df',
        label='Wind Gusts (km/h)',
        width=precip_bar_width_days,
        zorder=1,
    )
    ax4.plot(times, wind_speeds, color='#1f77b4', linewidth=4, label='Wind (km/h)', zorder=5)
    ax4.set_title('Wind', fontweight='bold', color='#333333')
    ax4.set_ylim(0, wind_ymax)
    ax4.legend(loc='upper left', fontsize=8, 
              framealpha=0.9, facecolor='white', edgecolor='gray')
    ax4.grid(True, alpha=0.3, linewidth=0.5)
    ax4.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=plot_tz))
    ax4.set_xlim(min(times), max(times))  # Ограничиваем ось X только временем заезда
    ax4.tick_params(colors='#333333')
    plt.setp(ax4.xaxis.get_majorticklabels(), rotation=45, fontsize=8)
    
    # 5. Elevation (нижний правый)
    ax5 = plt.subplot(3, 2, 6)
    elevations = [p['ele'] for p in route_points_clean]
    min_elevation = min(elevations) if elevations else 0
    
    ax5.fill_between(times, elevations, min_elevation, alpha=0.7, color='#ff7f0e')
    ax5.plot(times, elevations, color='#ff6b6b', linewidth=4)
    ax5.set_title('Elevation', fontweight='bold', color='#333333')
    ax5.set_ylim(min_elevation, None)
    ax5.grid(True, alpha=0.3, linewidth=0.5)
    ax5.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=plot_tz))
    ax5.set_xlim(min(times), max(times))  # Ограничиваем ось X только временем заезда
    ax5.tick_params(colors='#333333')
    plt.setp(ax5.xaxis.get_majorticklabels(), rotation=45, fontsize=8)
    

    
    plt.tight_layout()
    plt.subplots_adjust(top=0.92, bottom=0.05)
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    
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
