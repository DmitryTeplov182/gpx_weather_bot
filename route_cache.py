"""Кэш GPX-треков: сам трек плюс сайдкар с ключами свежести.

Одна запись — это два файла:

    cache/komoot-2526993761.gpx
    cache/komoot-2526993761.meta.json

Имя детерминированное (`{provider}-{id}`), поэтому переименование маршрута в
Komoot/RideWithGPS перезаписывает файл, а не плодит рядом второй с тем же id.
Сайдкар хранит `etag` и `remote_updated_at` — по ним `route_sources` понимает,
менялся ли маршрут, не скачивая трек целиком.

Запись атомарная: сначала GPX, потом сайдкар, оба через временный файл и
`os.replace`. Если процесс убили посередине, останется трек без сайдкара —
такая запись считается отсутствующей и будет перекачана.
"""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CACHE_DIR = 'cache'
META_SUFFIX = '.meta.json'

# Файлы, которые komootgpx писал до перехода на сайдкары: "{title}-{tour_id}.gpx"
LEGACY_GPX_PATTERN = re.compile(r'.*-\d+\.gpx$')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def entry_stem(provider: str, route_id: str) -> str:
    return f"{provider}-{route_id}"


def cache_paths(provider: str, route_id: str, cache_dir: str = CACHE_DIR):
    """Возвращает (путь к GPX, путь к сайдкару)."""
    stem = os.path.join(cache_dir, entry_stem(provider, route_id))
    return f"{stem}.gpx", f"{stem}{META_SUFFIX}"


def read_meta(provider: str, route_id: str, cache_dir: str = CACHE_DIR):
    """Сайдкар, если запись цела. Трек без сайдкара (и наоборот) — это не кэш."""
    gpx_path, meta_path = cache_paths(provider, route_id, cache_dir)
    if not (os.path.exists(gpx_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Повреждённый сайдкар {meta_path}: {e}")
        return None
    if not isinstance(meta, dict):
        return None
    meta['gpx_path'] = gpx_path
    return meta


def _write_atomic(path: str, data, mode: str):
    # Суффикс уникален на вызов, а не на процесс: два пользователя могут
    # запросить один и тот же маршрут одновременно, и общий временный файл
    # они бы писали друг поверх друга.
    tmp_path = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, mode, **({} if 'b' in mode else {'encoding': 'utf-8'})) as f:
            f.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_entry(provider: str, route_id: str, gpx_data, meta: dict,
                cache_dir: str = CACHE_DIR) -> str:
    """Кладёт трек и сайдкар. gpx_data — str или bytes. Возвращает путь к GPX."""
    os.makedirs(cache_dir, exist_ok=True)
    gpx_path, meta_path = cache_paths(provider, route_id, cache_dir)

    if isinstance(gpx_data, bytes):
        _write_atomic(gpx_path, gpx_data, 'wb')
    else:
        _write_atomic(gpx_path, gpx_data, 'w')

    payload = dict(meta)
    payload.update({
        'provider': provider,
        'id': str(route_id),
        'fetched_at': now_iso(),
        'last_used_at': now_iso(),
    })
    _write_atomic(meta_path, json.dumps(payload, ensure_ascii=False, indent=2), 'w')
    logger.info(f"Кэш обновлён: {gpx_path}")
    return gpx_path


def update_meta(provider: str, route_id: str, changes: dict,
                cache_dir: str = CACHE_DIR) -> None:
    """Правит поля сайдкара, не трогая трек (например, протухший ETag)."""
    meta = read_meta(provider, route_id, cache_dir)
    if meta is None:
        return
    meta.pop('gpx_path', None)
    meta.update(changes)
    _, meta_path = cache_paths(provider, route_id, cache_dir)
    try:
        _write_atomic(meta_path, json.dumps(meta, ensure_ascii=False, indent=2), 'w')
    except OSError as e:
        logger.warning(f"Не удалось обновить сайдкар {meta_path}: {e}")


def touch_used(provider: str, route_id: str, cache_dir: str = CACHE_DIR) -> None:
    """Отмечает запись использованной — по этому полю работает вытеснение."""
    update_meta(provider, route_id, {'last_used_at': now_iso()}, cache_dir)


def _age_days(iso_or_none: str | None, fallback_path: str) -> float:
    """Возраст в днях по ISO-метке, а при её отсутствии — по mtime файла."""
    if iso_or_none:
        try:
            dt = datetime.fromisoformat(iso_or_none)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except ValueError:
            pass
    try:
        return (time.time() - os.path.getmtime(fallback_path)) / 86400
    except OSError:
        return 0.0


def purge_unused(days: int, cache_dir: str = CACHE_DIR) -> int:
    """Вытеснение по времени последнего использования.

    Для записей с сайдкаром смотрим `last_used_at` — то есть реально возраст
    кэша. Для одиночных GPX без сайдкара (загруженные пользователем файлы,
    временные копии избранного) остаётся старое поведение по mtime.

    Раньше здесь был mtime для всего подряд, а komootgpx выставляет треку
    mtime, равный дате правки маршрута, — из-за чего давно нарисованный и
    потому заведомо актуальный маршрут вычищался на каждой уборке.
    """
    deleted = 0
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return 0

    for name in names:
        if not name.endswith('.gpx'):
            continue
        gpx_path = os.path.join(cache_dir, name)
        meta_path = f"{os.path.splitext(gpx_path)[0]}{META_SUFFIX}"
        last_used = None
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    last_used = json.load(f).get('last_used_at')
            except (OSError, json.JSONDecodeError):
                last_used = None
        try:
            if _age_days(last_used, gpx_path) <= days:
                continue
            os.remove(gpx_path)
            deleted += 1
            if os.path.exists(meta_path):
                os.remove(meta_path)
            logger.info(f"Вытеснена неиспользуемая запись кэша: {gpx_path}")
        except OSError as e:
            logger.error(f"Ошибка при вытеснении {gpx_path}: {e}")
    return deleted


def purge_legacy(cache_dir: str = CACHE_DIR) -> int:
    """Сносит треки старого формата "{title}-{id}.gpx" без сайдкара.

    Именно они и порождали двойников: после переименования маршрута рядом
    оставался файл со старым названием и тем же id, а выбирался он или новый —
    зависело от порядка обхода каталога.
    """
    deleted = 0
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return 0

    for name in names:
        if not LEGACY_GPX_PATTERN.match(name):
            continue
        gpx_path = os.path.join(cache_dir, name)
        meta_path = f"{os.path.splitext(gpx_path)[0]}{META_SUFFIX}"
        if os.path.exists(meta_path):
            continue  # запись нового формата, её трогать нельзя
        try:
            os.remove(gpx_path)
            deleted += 1
            logger.info(f"Удалён трек старого формата: {gpx_path}")
        except OSError as e:
            logger.error(f"Ошибка при удалении {gpx_path}: {e}")
    return deleted


def stats(cache_dir: str = CACHE_DIR) -> dict:
    """Сводка для /status: сколько записей, сколько с сайдкаром, общий размер."""
    total = tracked = size = 0
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return {'total': 0, 'tracked': 0, 'size_bytes': 0}

    for name in names:
        if not name.endswith('.gpx'):
            continue
        gpx_path = os.path.join(cache_dir, name)
        total += 1
        try:
            size += os.path.getsize(gpx_path)
        except OSError:
            pass
        if os.path.exists(f"{os.path.splitext(gpx_path)[0]}{META_SUFFIX}"):
            tracked += 1
    return {'total': total, 'tracked': tracked, 'size_bytes': size}
