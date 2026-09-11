"""Источники маршрутов: Komoot и RideWithGPS за одним интерфейсом.

Боты не знают, откуда взялся трек. Они зовут `parse_route_link` на присланном
тексте и `ensure_route` на результате, а дальше получают `RouteData` с путём к
GPX и метаданными.

Свежесть проверяется условным запросом (`If-None-Match`), поэтому повторное
обращение к неизменившемуся маршруту стоит 0.2–0.6 с и ноль байт трафика.
Если маршрут поменялся, в `RouteData.changed` приходит True, а в `previous` —
метаданные до обновления, чтобы бот мог сказать «было 51 км, стало 54».

RideWithGPS: основной путь — `https://ridewithgps.com/{routes|trips}/{id}.json`,
он работает без ключей и отдаёт точки трека вместе с метаданными (сам `.gpx`
там закрыт авторизацией, поэтому GPX собирается из точек). Фоллбэк — официальный
API v1 с парой RWGPS_API_KEY + RWGPS_AUTH_TOKEN, он включается только когда
основной путь отвечает 5xx или не отвечает вовсе.
"""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests

import route_cache

logger = logging.getLogger(__name__)

KOMOOT_LINK_PATTERN = re.compile(r'(?:https?://)?(?:www\.)?komoot\.[^/\s]+/tour/(\d+)')
RWGPS_LINK_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?ridewithgps\.com/(routes|trips)/(\d+)', re.IGNORECASE)
RWGPS_EMBED_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?ridewithgps\.com/embeds\?\S*\btype=(route|trip)\b\S*\bid=(\d+)',
    re.IGNORECASE)
PRIVACY_CODE_PATTERN = re.compile(r'[?&]privacy_code=([A-Za-z0-9_-]+)')

# Подпись ссылки в тексте анонса
PROVIDER_LINK_LABELS = {'komoot': 'комут', 'rwgps': 'rwgps'}
# Название сервиса в сообщениях бота
PROVIDER_NAMES = {'komoot': 'Komoot', 'rwgps': 'RideWithGPS'}

KOMOOT_API_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'application/hal+json,application/json',
}
RWGPS_HEADERS = {
    'User-Agent': 'gpx-weather-bot (+https://github.com/DmitryTeplov182/gpx_weather_bot)',
    'Accept': 'application/json',
}

KOMOOT_TIMEOUT = 15
RWGPS_TIMEOUT = 30
KOMOOTGPX_TIMEOUT = 60


class RouteError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


@dataclass
class RouteRef:
    provider: str            # komoot | rwgps
    route_id: str
    url: str
    privacy_code: str | None = None
    kind: str = 'routes'     # для rwgps: routes | trips

    @property
    def cache_provider(self) -> str:
        """Ключ кэша. Маршрут и заезд с одинаковым id — разные вещи."""
        if self.provider == 'rwgps' and self.kind == 'trips':
            return 'rwgps_trip'
        return self.provider

    @property
    def service_name(self) -> str:
        return PROVIDER_NAMES.get(self.provider, self.provider)


@dataclass
class RouteData:
    ref: RouteRef
    gpx_path: str
    name: str | None = None
    distance_m: float | None = None
    elevation_up: float | None = None
    elevation_down: float | None = None
    changed: bool = False           # маршрут изменился с прошлого раза
    stale: bool = False             # не смогли проверить актуальность, отдали кэш
    previous: dict | None = None    # метаданные до обновления
    extra: dict = field(default_factory=dict)

    @property
    def distance_km(self):
        return None if self.distance_m is None else round(self.distance_m / 1000)

    @property
    def elevation_up_m(self):
        return None if self.elevation_up is None else round(self.elevation_up)


def provider_link_label(provider: str | None) -> str:
    return PROVIDER_LINK_LABELS.get(provider or '', 'маршрут')


def _conditional(headers: dict, etag: str | None) -> dict:
    """Добавляет If-None-Match, если ETag пригоден для HTTP-заголовка.

    Заголовки латиницей: испорченный сайдкар с нелатинским ETag не должен
    ронять запрос — лучше сходить без условия и просто перекачать.
    """
    if etag:
        try:
            etag.encode('latin-1')
            headers['If-None-Match'] = etag
        except UnicodeEncodeError:
            logger.warning(f'Непригодный ETag в кэше, игнорирую: {etag!r}')
    return headers


def supported_services_hint() -> str:
    return 'Komoot или RideWithGPS'


# --------------------------------------------------------------------------
# Разбор ссылок
# --------------------------------------------------------------------------

def parse_route_link(text: str) -> RouteRef | None:
    """Находит в тексте ссылку на поддерживаемый маршрут."""
    if not text:
        return None

    match = KOMOOT_LINK_PATTERN.search(text)
    if match:
        return RouteRef(provider='komoot', route_id=match.group(1), url=_clean_url(text))

    match = RWGPS_LINK_PATTERN.search(text)
    if match:
        kind, route_id = match.group(1).lower(), match.group(2)
    else:
        match = RWGPS_EMBED_PATTERN.search(text)
        if not match:
            return None
        kind = 'routes' if match.group(1).lower() == 'route' else 'trips'
        route_id = match.group(2)

    code = PRIVACY_CODE_PATTERN.search(text)
    return RouteRef(
        provider='rwgps',
        route_id=route_id,
        url=_clean_url(text),
        privacy_code=code.group(1) if code else None,
        kind=kind,
    )


def _clean_url(text: str) -> str:
    """Вытаскивает саму ссылку, если её прислали внутри предложения."""
    for token in text.split():
        if 'komoot.' in token or 'ridewithgps.com' in token:
            return token.strip().rstrip('.,;)')
    return text.strip()


# --------------------------------------------------------------------------
# Общий вход
# --------------------------------------------------------------------------

def ensure_route(ref: RouteRef, cache_dir: str = route_cache.CACHE_DIR) -> RouteData:
    """Отдаёт актуальный GPX маршрута, перекачивая его только при изменении."""
    if ref.provider == 'komoot':
        return _ensure_komoot(ref, cache_dir)
    if ref.provider == 'rwgps':
        return _ensure_rwgps(ref, cache_dir)
    raise RouteError(f'Неизвестный источник маршрута: {ref.provider}')


def _from_cache(ref: RouteRef, cached: dict, *, stale: bool = False) -> RouteData:
    return RouteData(
        ref=ref,
        gpx_path=cached['gpx_path'],
        name=cached.get('name'),
        distance_m=cached.get('distance_m'),
        elevation_up=cached.get('elevation_up'),
        elevation_down=cached.get('elevation_down'),
        changed=False,
        stale=stale,
        extra=cached.get('extra') or {},
    )


# --------------------------------------------------------------------------
# Komoot
# --------------------------------------------------------------------------

def _komoot_fetch_meta(tour_id: str, etag: str | None):
    """(данные тура или None при 304, ETag).

    Komoot отдаёт сглаженные длину и набор — они точнее расчёта по сырым точкам
    GPX. Здесь же берётся `changed_at`, по которому видно, менялся ли маршрут.
    """
    url = f'https://api.komoot.de/v007/tours/{tour_id}'
    headers = _conditional(dict(KOMOOT_API_HEADERS), etag)
    response = requests.get(url, headers=headers, timeout=KOMOOT_TIMEOUT)
    if response.status_code == 304:
        return None, etag
    if response.status_code == 404:
        raise RouteError('Маршрут не найден в Komoot. Проверь ссылку.')
    if response.status_code in (401, 403):
        # Приватный тур — частая ошибка пользователя, её не надо показывать
        # как «сервис не отвечает»
        raise RouteError('Маршрут в Komoot не публичный. Открой к нему доступ и пришли ссылку снова.')
    response.raise_for_status()
    return response.json(), response.headers.get('ETag')


def _komoot_download_gpx(tour_id: str, cache_dir: str) -> str:
    """Качает GPX через komootgpx во временный каталог и возвращает его текст.

    Каталог временный намеренно: komootgpx называет файл по текущему имени тура,
    и раньше переименование маршрута оставляло в кэше второй файл с тем же id.
    """
    os.makedirs(cache_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix=f'komoot-{tour_id}-', dir=cache_dir)
    try:
        try:
            result = subprocess.run(
                ['komootgpx', '-d', tour_id, '-o', tmp_dir, '-e', '-n'],
                capture_output=True, text=True, timeout=KOMOOTGPX_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise RouteError('Превышено время ожидания при скачивании GPX из Komoot.')
        except FileNotFoundError:
            raise RouteError('komootgpx не найден в системе — GPX скачать нечем.')

        if result.returncode != 0:
            error_msg = (result.stderr or '').strip() or 'неизвестная ошибка'
            raise RouteError(f'Komoot не отдал GPX: {error_msg}')

        files = glob.glob(os.path.join(tmp_dir, '*.gpx'))
        if not files:
            raise RouteError('Komoot не отдал GPX-файл.')
        with open(files[0], 'r', encoding='utf-8') as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _komoot_meta(data: dict, url: str, etag: str | None) -> dict:
    return {
        'url': url,
        'etag': etag,
        'remote_updated_at': data.get('changed_at'),
        'name': data.get('name'),
        'distance_m': data.get('distance'),
        'elevation_up': data.get('elevation_up'),
        'elevation_down': data.get('elevation_down'),
        'extra': {
            'sport': data.get('sport'),
            'difficulty': (data.get('difficulty') or {}).get('grade'),
        },
    }


def _ensure_komoot(ref: RouteRef, cache_dir: str) -> RouteData:
    cached = route_cache.read_meta(ref.cache_provider, ref.route_id, cache_dir)

    try:
        data, etag = _komoot_fetch_meta(ref.route_id, cached.get('etag') if cached else None)
    except RouteError:
        raise
    except requests.RequestException as e:
        if cached:
            logger.warning(f"Komoot недоступен ({e}), отдаю маршрут {ref.route_id} из кэша")
            route_cache.touch_used(ref.cache_provider, ref.route_id, cache_dir)
            return _from_cache(ref, cached, stale=True)
        raise RouteError(f'Komoot не отвечает: {e}')

    if data is None:  # 304 — маршрут не менялся
        route_cache.touch_used(ref.cache_provider, ref.route_id, cache_dir)
        logger.info(f"Komoot {ref.route_id}: 304, кэш актуален")
        return _from_cache(ref, cached)

    if cached and cached.get('remote_updated_at') == data.get('changed_at'):
        # ETag сменился по техническим причинам, сам маршрут тот же
        route_cache.update_meta(ref.cache_provider, ref.route_id,
                                {'etag': etag, 'last_used_at': route_cache.now_iso()},
                                cache_dir)
        return _from_cache(ref, cached)

    gpx_text = _komoot_download_gpx(ref.route_id, cache_dir)
    meta = _komoot_meta(data, ref.url, etag)
    gpx_path = route_cache.write_entry(ref.cache_provider, ref.route_id, gpx_text, meta, cache_dir)

    return RouteData(
        ref=ref, gpx_path=gpx_path, name=meta['name'],
        distance_m=meta['distance_m'], elevation_up=meta['elevation_up'],
        elevation_down=meta['elevation_down'],
        changed=cached is not None, previous=cached, extra=meta['extra'],
    )


# --------------------------------------------------------------------------
# RideWithGPS
# --------------------------------------------------------------------------

def _rwgps_unwrap(payload: dict) -> dict:
    """Легаси отдаёт объект в корне, API v1 — обёрнутым в {"route": ...}."""
    if not isinstance(payload, dict):
        raise RouteError('RideWithGPS вернул неожиданный ответ.')
    for key in ('route', 'trip'):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return payload


def _rwgps_fetch_json(ref: RouteRef, etag: str | None):
    """Основной путь: бесключевой эндпоинт. (данные или None при 304, ETag)."""
    url = f'https://ridewithgps.com/{ref.kind}/{ref.route_id}.json'
    headers = _conditional(dict(RWGPS_HEADERS), etag)
    params = {'privacy_code': ref.privacy_code} if ref.privacy_code else None

    response = requests.get(url, headers=headers, params=params, timeout=RWGPS_TIMEOUT)
    if response.status_code == 304:
        return None, etag
    if response.status_code == 403:
        raise RouteError(
            'Маршрут в RideWithGPS приватный. Сделай его публичным '
            'или пришли ссылку с privacy_code.'
        )
    if response.status_code == 404:
        raise RouteError('Маршрут не найден в RideWithGPS. Проверь ссылку.')
    response.raise_for_status()
    return _rwgps_unwrap(response.json()), response.headers.get('ETag')


def _rwgps_api_credentials():
    key, token = os.getenv('RWGPS_API_KEY'), os.getenv('RWGPS_AUTH_TOKEN')
    return (key, token) if key and token else (None, None)


def _rwgps_api_fetch(ref: RouteRef):
    """Фоллбэк: официальный API v1 по ключу. (данные, gpx_bytes) или None.

    Ключи бессрочные (в схеме AuthToken нет поля срока жизни), так что 401 здесь
    означает испорченную конфигурацию, а не истёкший токен.
    """
    key, token = _rwgps_api_credentials()
    if not key:
        return None

    headers = dict(RWGPS_HEADERS)
    headers.update({'x-rwgps-api-key': key, 'x-rwgps-auth-token': token})
    params = {'privacy_code': ref.privacy_code} if ref.privacy_code else None
    base = f'https://ridewithgps.com/api/v1/{ref.kind}/{ref.route_id}'

    try:
        gpx_response = requests.get(f'{base}.gpx', headers=headers, params=params,
                                    timeout=RWGPS_TIMEOUT)
        if gpx_response.status_code == 401:
            logger.error('RideWithGPS отверг RWGPS_API_KEY/RWGPS_AUTH_TOKEN (401)')
            return None
        gpx_response.raise_for_status()

        meta_response = requests.get(f'{base}.json', headers=headers, params=params,
                                     timeout=RWGPS_TIMEOUT)
        meta_response.raise_for_status()
        data = _rwgps_unwrap(meta_response.json())
    except requests.RequestException as e:
        logger.error(f'Фоллбэк RideWithGPS тоже не сработал: {e}')
        return None

    logger.info(f'RideWithGPS {ref.route_id}: сработал фоллбэк через API v1')
    return data, gpx_response.content


def rwgps_to_gpx(data: dict, kind: str = 'routes') -> str:
    """Собирает GPX 1.1 из track_points: x/y — координаты, e — высота, t — время."""
    name = data.get('name') or f"RideWithGPS {data.get('id')}"
    description = data.get('description') or ''
    points = data.get('track_points') or []
    if not points:
        raise RouteError('В ответе RideWithGPS нет точек трека.')

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="gpx-weather-bot (RideWithGPS)" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f'  <metadata><name>{escape(name)}</name>'
        + (f'<desc>{escape(description)}</desc>' if description else '')
        + '</metadata>',
        f'  <trk><name>{escape(name)}</name><type>cycling</type><trkseg>',
    ]
    for point in points:
        lat, lon = point.get('y'), point.get('x')
        if lat is None or lon is None:
            continue
        row = f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}">'
        if point.get('e') is not None:
            row += f'<ele>{point["e"]:.1f}</ele>'
        if kind == 'trips' and point.get('t') is not None:
            stamp = datetime.fromtimestamp(point['t'], timezone.utc)
            row += f'<time>{stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>'
        lines.append(row + '</trkpt>')
    lines += ['  </trkseg></trk>', '</gpx>']
    return '\n'.join(lines)


def _rwgps_meta(data: dict, url: str, etag: str | None) -> dict:
    return {
        'url': url,
        'etag': etag,
        'remote_updated_at': str(data.get('updated_at')),
        'name': data.get('name'),
        'distance_m': data.get('distance'),
        'elevation_up': data.get('elevation_gain'),
        'elevation_down': data.get('elevation_loss'),
        'extra': {
            'surface': data.get('surface'),
            'unpaved_pct': data.get('unpaved_pct'),
            'track_type': data.get('track_type'),
            'difficulty': data.get('difficulty'),
            'locality': data.get('locality'),
            'n_cues': len(data.get('course_points') or []),
        },
    }


def _ensure_rwgps(ref: RouteRef, cache_dir: str) -> RouteData:
    cached = route_cache.read_meta(ref.cache_provider, ref.route_id, cache_dir)
    gpx_bytes = None

    try:
        data, etag = _rwgps_fetch_json(ref, cached.get('etag') if cached else None)
    except RouteError:
        raise  # 403/404 — в фоллбэке будет то же самое
    except (requests.RequestException, ValueError) as e:
        logger.warning(f'Основной путь RideWithGPS не сработал ({e}), пробую API v1')
        fallback = _rwgps_api_fetch(ref)
        if fallback is None:
            if cached:
                logger.warning(f'Отдаю маршрут {ref.route_id} из кэша')
                route_cache.touch_used(ref.cache_provider, ref.route_id, cache_dir)
                return _from_cache(ref, cached, stale=True)
            raise RouteError(f'RideWithGPS не отвечает: {e}')
        data, gpx_bytes = fallback
        etag = None

    if data is None:  # 304 — маршрут не менялся
        route_cache.touch_used(ref.cache_provider, ref.route_id, cache_dir)
        logger.info(f'RideWithGPS {ref.route_id}: 304, кэш актуален')
        return _from_cache(ref, cached)

    if cached and gpx_bytes is None and cached.get('remote_updated_at') == str(data.get('updated_at')):
        route_cache.update_meta(ref.cache_provider, ref.route_id,
                                {'etag': etag, 'last_used_at': route_cache.now_iso()},
                                cache_dir)
        return _from_cache(ref, cached)

    gpx_data = gpx_bytes if gpx_bytes is not None else rwgps_to_gpx(data, ref.kind)
    meta = _rwgps_meta(data, ref.url, etag)
    gpx_path = route_cache.write_entry(ref.cache_provider, ref.route_id, gpx_data, meta, cache_dir)

    return RouteData(
        ref=ref, gpx_path=gpx_path, name=meta['name'],
        distance_m=meta['distance_m'], elevation_up=meta['elevation_up'],
        elevation_down=meta['elevation_down'],
        changed=cached is not None, previous=cached, extra=meta['extra'],
    )


# --------------------------------------------------------------------------
# Сообщения об обновлении
# --------------------------------------------------------------------------

def describe_change(route: RouteData) -> str | None:
    """Текст «маршрут обновился: было … стало …», если есть что сказать."""
    if not route.changed or not route.previous:
        return None

    def km(value):
        return None if value is None else round(value / 1000)

    def m(value):
        return None if value is None else round(value)

    was_km, now_km = km(route.previous.get('distance_m')), km(route.distance_m)
    was_up, now_up = m(route.previous.get('elevation_up')), m(route.elevation_up)
    service = route.ref.service_name

    if (was_km, was_up) != (now_km, now_up) and None not in (was_km, now_km):
        return (f'🔄 Маршрут обновился в {service}: было {was_km} км / {was_up} м, '
                f'стало {now_km} км / {now_up} м.')

    was_name, now_name = route.previous.get('name'), route.name
    if was_name and now_name and was_name != now_name:
        return f'🔄 Маршрут переименован в {service}: «{was_name}» → «{now_name}».'

    return f'🔄 Маршрут обновился в {service}, трек перекачан.'


def describe_stale(route: RouteData) -> str | None:
    """Текст про то, что актуальность проверить не удалось."""
    if not route.stale:
        return None
    meta_path = f"{os.path.splitext(route.gpx_path)[0]}{route_cache.META_SUFFIX}"
    fetched = ''
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            stamp = json.load(f).get('fetched_at')
        if stamp:
            fetched = f" от {datetime.fromisoformat(stamp).strftime('%d.%m %H:%M')}"
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return (f'⚠️ Не смог проверить актуальность маршрута ({route.ref.service_name} '
            f'не ответил). Использую сохранённую версию{fetched}.')
