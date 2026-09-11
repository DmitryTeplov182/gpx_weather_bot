"""Разведочный скрипт: RideWithGPS-ссылка -> метаданные + GPX.

Использование:
    python tools/rwgps_probe.py https://ridewithgps.com/routes/45000000 out.gpx

Проверяет, что легаси-эндпоинт /{routes,trips}/{id}.json доступен без авторизации
и что из track_points собирается GPX, совместимый с ride_dashboard.py.
Рядом с GPX пишется сайдкар out.gpx.meta.json с ETag и updated_at; при повторном
запуске скрипт делает условный запрос и показывает 304, если маршрут не менялся —
это тот самый механизм ревалидации кэша из плана.
Подробности: docs/plans/2026-09-11-ridewithgps-integration.md

Только stdlib, чтобы запускалось без venv. В боте вместо urllib будет requests,
а сборку GPX можно оставить как есть (текстовый шаблон) — это ровно то, что
komootgpx отдаёт для Komoot.
"""
import json
import re
import urllib.request
import urllib.error
from xml.sax.saxutils import escape

RWGPS_LINK_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?ridewithgps\.com/(routes|trips)/(\d+)',
    re.IGNORECASE,
)
RWGPS_EMBED_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?ridewithgps\.com/embeds\?[^ ]*\btype=(route|trip)\b[^ ]*\bid=(\d+)',
    re.IGNORECASE,
)
PRIVACY_CODE_PATTERN = re.compile(r'[?&]privacy_code=([A-Za-z0-9_-]+)')

HEADERS = {
    'User-Agent': 'gpx-weather-bot/1.0 (+telegram announce bot)',
    'Accept': 'application/json',
}


def parse_rwgps_link(text):
    """Возвращает (kind, id, privacy_code) или None. kind = 'routes' | 'trips'."""
    m = RWGPS_LINK_PATTERN.search(text)
    if m:
        kind, rid = m.group(1).lower(), m.group(2)
    else:
        m = RWGPS_EMBED_PATTERN.search(text)
        if not m:
            return None
        kind = 'routes' if m.group(1).lower() == 'route' else 'trips'
        rid = m.group(2)
    code = PRIVACY_CODE_PATTERN.search(text)
    return kind, rid, (code.group(1) if code else None)


def fetch_rwgps(kind, rid, privacy_code=None, timeout=30, etag=None):
    """Возвращает (data, etag). При 304 data is None — кэш актуален."""
    url = f'https://ridewithgps.com/{kind}/{rid}.json'
    if privacy_code:
        url += f'?privacy_code={privacy_code}'
    headers = dict(HEADERS)
    if etag:
        headers['If-None-Match'] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8')), r.headers.get('ETag')
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        body = e.read().decode('utf-8', 'replace')[:300]
        raise RuntimeError(f'RideWithGPS {e.code}: {body}') from e


def rwgps_to_gpx(data, kind='routes'):
    """Строит GPX 1.1 из track_points. Для trips пишет время точек."""
    name = data.get('name') or f"RideWithGPS {data.get('id')}"
    desc = data.get('description') or ''
    pts = data.get('track_points') or []
    if not pts:
        raise RuntimeError('в ответе нет track_points')

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="gpx-weather-bot (RideWithGPS)" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f'  <metadata><name>{escape(name)}</name>'
        + (f'<desc>{escape(desc)}</desc>' if desc else '')
        + '</metadata>',
        f'  <trk><name>{escape(name)}</name><type>cycling</type><trkseg>',
    ]
    import datetime as _dt
    for p in pts:
        lat, lon = p.get('y'), p.get('x')
        if lat is None or lon is None:
            continue
        row = f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}">'
        if p.get('e') is not None:
            row += f'<ele>{p["e"]:.1f}</ele>'
        if p.get('t') is not None and kind == 'trips':
            ts = _dt.datetime.fromtimestamp(p['t'], _dt.timezone.utc)
            row += f'<time>{ts.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>'
        row += '</trkpt>'
        out.append(row)
    out += ['  </trkseg></trk>', '</gpx>']
    return '\n'.join(out)


def rwgps_meta(data):
    """Метаданные в том же виде, что fetch_komoot_tour_meta()."""
    return {
        'name': data.get('name'),
        'distance_m': data.get('distance'),
        'elevation_up': data.get('elevation_gain'),
        # бонус, которого нет у Komoot:
        'elevation_down': data.get('elevation_loss'),
        'locality': data.get('locality'),
        'country_code': data.get('country_code'),
        'surface': data.get('surface'),
        'unpaved_pct': data.get('unpaved_pct'),
        'track_type': data.get('track_type'),
        'difficulty': data.get('difficulty'),
        'visibility': data.get('visibility'),
        'n_points': len(data.get('track_points') or []),
        'n_cues': len(data.get('course_points') or []),
    }


if __name__ == '__main__':
    import os
    import sys

    link = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else 'out.gpx'
    meta_path = out_path + '.meta.json'

    parsed = parse_rwgps_link(link)
    print('parsed:', parsed)
    kind, rid, code = parsed

    # Сайдкар от прошлого запуска: ETag + updated_at, как задумано для кэша бота
    cached = None
    if os.path.exists(meta_path) and os.path.exists(out_path):
        with open(meta_path, encoding='utf-8') as f:
            cached = json.load(f)
        print('сайдкар:', cached.get('etag'), 'updated_at =', cached.get('updated_at'))

    data, etag = fetch_rwgps(kind, rid, code, etag=(cached or {}).get('etag'))

    if data is None:
        print('304 Not Modified — кэш актуален, качать нечего')
        sys.exit(0)

    meta = rwgps_meta(data)
    print('meta:', json.dumps(meta, ensure_ascii=False, indent=2))

    if cached and cached.get('updated_at') == data.get('updated_at'):
        print('ETag сменился, но updated_at тот же — GPX не изменился')

    gpx = rwgps_to_gpx(data, kind)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(gpx)
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump({'etag': etag, 'updated_at': data.get('updated_at'), **meta}, f,
                  ensure_ascii=False, indent=2)
    print('gpx bytes:', len(gpx.encode('utf-8')), '->', out_path)
    print('сайдкар ->', meta_path)
