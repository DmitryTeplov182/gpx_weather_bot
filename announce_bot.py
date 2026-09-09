import os
import re
import sys
import json
import subprocess
import requests
from telegram import (
    Update,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
)
import logging
import asyncio
import glob
import gpxpy
import gpxpy.gpx
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pytz
from timezonefinder import TimezoneFinder
load_dotenv()

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
ASK_DATE, ASK_TIME, ASK_KOMOOT_LINK, PROCESS_GPX, ASK_ROUTE_NAME, ASK_START_POINT, ASK_START_LINK, ASK_FINISH_POINT, ASK_FINISH_LINK, ASK_PACE, ASK_COMMENT, ASK_IMAGE, PREVIEW_STEP, SELECT_ROUTE, ASK_MANUAL_ROUTE = range(15)

STEP_TO_NAME = {
    ASK_DATE: '📅 Изм. дату',
    ASK_TIME: '⏰ Изм. время',
    ASK_KOMOOT_LINK: '🔗 Изм. ссылку Komoot',
    ASK_ROUTE_NAME: '📝 Изм. название',
    ASK_START_POINT: '📍 Изм. старт',
    ASK_FINISH_POINT: '🏁 Изм. финиш',
    ASK_PACE: '🌙 Изм. темп',
    ASK_COMMENT: '💬 Изм. коммен.',
    ASK_IMAGE: '📷 Изм. картинку',
}

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Belgrade')

# Паттерн для извлечения tour_id из Komoot-ссылки
KOMOOT_LINK_PATTERN = re.compile(r'(https?://)?(www\.)?komoot\.[^/]+/tour/(\d+)')
CACHE_DIR = 'cache'
os.makedirs(CACHE_DIR, exist_ok=True)

KOMOOT_API_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'application/hal+json,application/json',
}

# Текст вотермарки на дашборде (название клуба)
DASHBOARD_WATERMARK = os.getenv('DASHBOARD_WATERMARK', 'whatever')
DASHBOARD_BUTTON = '🖼️ Дашборд заезда'
# Все кнопки, по которым (пере)строится дашборд
DASHBOARD_GENERATE_BUTTONS = (DASHBOARD_BUTTON, '🖼️ Сгенерировать дашборд', '🔄 Обновить дашборд')

# Кнопка "своя точка" в списках старта/финиша: определяется флагом custom
CUSTOM_POINT = {'name': '✏️ Своя точка', 'link': None, 'custom': True}

def fetch_komoot_tour_meta(tour_id):
    """Получает метаданные тура из неофициального API Komoot.

    Komoot отдаёт сглаженные значения длины и набора — они точнее,
    чем расчёт по сырым точкам GPX. Работает только для публичных туров.
    Возвращает dict {name, distance_m, elevation_up} или None при любой ошибке.
    """
    url = f'https://api.komoot.de/v007/tours/{tour_id}'
    try:
        response = requests.get(url, headers=KOMOOT_API_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        return {
            'name': data.get('name'),
            'distance_m': data.get('distance'),
            'elevation_up': data.get('elevation_up'),
        }
    except Exception as e:
        logger.warning(f"Не удалось получить метаданные Komoot для тура {tour_id}: {e}")
        return None

def load_points_from_file(filename, fallback_points=None):
    """Загружает точки из JSON файла

    Args:
        filename (str): Имя файла (start_points.json или finish_points.json)
        fallback_points (list): Точки по умолчанию, если файл не найден

    Returns:
        list: Список точек с полями name и link
    """
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)

            # Новый формат - точки в массиве "points"
            if 'points' in data and isinstance(data['points'], list):
                points = data['points']
            # Старый формат для обратной совместимости
            elif 'start_points' in data:
                points = data['start_points']
            else:
                points = []

            # Автоматически добавляем "Свою точку" в конец списка.
            # Флаг custom, а не имя кнопки, отличает её от обычных точек,
            # поэтому названия точек можно менять свободно.
            points.append(CUSTOM_POINT.copy())

            return points

    except FileNotFoundError:
        logger.warning(f"Файл {filename} не найден, используем точки по умолчанию")
        return fallback_points or get_default_points()

    except json.JSONDecodeError as e:
        logger.error(f"Ошибка при парсинге {filename}: {e}")
        return fallback_points or get_default_points()

    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке точек из {filename}: {e}")
        return fallback_points or get_default_points()

def get_default_points():
    """Точки по умолчанию, если start_points.json недоступен (копия JSON)."""
    default_points = [
        {'name': 'CoffeeRide', 'link': 'https://maps.app.goo.gl/iTBcRqjvhJ9DYvRK7'},
        {'name': 'Flags (Liman)', 'link': 'https://maps.app.goo.gl/j95ME2cuzX8k9hnj7'},
        {'name': 'Železnički park', 'link': 'https://maps.app.goo.gl/hSZ9C4Xue5RVpMea8'},
        {'name': 'Železnička stanica Petrovaradin', 'link': 'https://maps.app.goo.gl/LiQuSUhWCGc9i1Mh9'},
        {'name': 'Bobar Petrol (Bulevar Evrope 120)', 'link': 'https://maps.app.goo.gl/RCn23pSWUyPcze8z9?g_st=ic'},
        {'name': 'Glavna pošta (21101)', 'link': 'https://maps.app.goo.gl/3GMZV5Ze65kDwYLH9'},
        {'name': 'Pekara', 'link': 'https://maps.app.goo.gl/5odriFaKFhEiZnDQ7'},
    ]
    default_points.append(CUSTOM_POINT.copy())
    return default_points

def load_start_points():
    """Загружает точки старта из JSON файла"""
    return load_points_from_file("start_points.json")

def load_finish_points():
    """Загружает точки финиша из JSON файла"""
    return load_points_from_file("finish_points.json")

# Загружаем точки старта и финиша при импорте модуля
START_POINTS = load_start_points()
FINISH_POINTS = load_finish_points()

# Предустановленные точки старта (заполнишь потом)
# START_POINTS = [
#     {'name': 'koferajd', 'link': 'https://maps.app.goo.gl/iTBcRqjvhJ9DYvRK7'},
#     {'name': 'Флаги', 'link': 'https://maps.app.goo.gl/j95ME2cuzX8k9hnj7'},
#     {'name': 'Лидл Лиман', 'link': 'https://maps.app.goo.gl/5JKtAgGBVe48jM9r7'},
#     {'name': 'Железничка Парк', 'link': 'https://maps.app.goo.gl/hSZ9C4Xue5RVpMea8'},
#     {'name': 'Своя точка', 'link': None},
# ]

PACE_OPTIONS = [
    '🌝🌚🌚',
    '🌝🌗🌚',
    '🌝🌝🌚',
    '🌝🌝🌗',
    '🌝🌝🌝',
]

# Маппинг лун на шкалу темпа дашборда (1.0–3.0)
PACE_TO_POSTER = dict(zip(PACE_OPTIONS, [1.0, 1.5, 2.0, 2.5, 3.0]))
# Скорость по умолчанию, если темп не задан. При лунах среднюю скорость считает
# ride_dashboard по профилю маршрута (PACE_MODEL в ride_poster.py).
DEFAULT_SPEED_KMH = 27
SPEED_MIN_KMH, SPEED_MAX_KMH = 10, 60
PACE_SKIP_BUTTON = '⏭️ Без темпа'
# "25-28", "25 – 28 км/ч", "27", "27.5 km/h"
SPEED_RANGE_PATTERN = re.compile(
    r'^\s*(\d{1,2}(?:[.,]\d)?)\s*(?:[-–—]\s*(\d{1,2}(?:[.,]\d)?))?\s*(?:км/ч|km/h|kmh)?\s*$',
    re.IGNORECASE,
)
# RSVP через опрос Telegram: при пересылке опрос остаётся тем же объектом,
# поэтому голоса общие для всех чатов, куда его переслали.
# Начиная с Bot API 9.6 у опроса есть форматируемое описание, а с 10.0 —
# картинка в описании, так что весь анонс живёт внутри одного сообщения-опроса.
RSVP_POLL_QUESTION = '🚴 Едешь?'
RSVP_POLL_OPTIONS = ['✅ Еду', '🤔 Может быть', '❌ Не в этот раз']
# Опрос без описания приходится подписывать вручную (запасной путь, см. ниже)
RSVP_HINT = 'Отмечайтесь в опросе ниже 👇'
# Описание опроса ограничено так же, как подпись к фото
POLL_DESCRIPTION_LIMIT = 1024

PACE_PROMPT = 'Выбери ожидаемый темп (луны), напиши среднюю скорость (например 25-28) или пропусти:'


def pace_keyboard():
    """Клавиатура шага темпа: луны + пропуск (скорость вводится текстом)."""
    rows = [[p] for p in PACE_OPTIONS]
    rows.append([PACE_SKIP_BUTTON])
    return rows


def parse_speed_range(text):
    """Разбирает '25-28' / '27' в (lo, hi) км/ч; None, если это не скорость."""
    match = SPEED_RANGE_PATTERN.match(text or '')
    if not match:
        return None
    lo = float(match.group(1).replace(',', '.'))
    hi = float(match.group(2).replace(',', '.')) if match.group(2) else lo
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def format_speed_range(speed_range, unit='км/ч'):
    lo, hi = speed_range
    if abs(lo - hi) < 0.05:
        return f"{lo:g} {unit}"
    return f"{lo:g}–{hi:g} {unit}"


def planned_speed_kmh(user_data):
    """Явная скорость для дашборда: середина диапазона, иначе None (луны/дефолт считает рендерер)."""
    speed_range = user_data.get('speed_range')
    if speed_range:
        return (float(speed_range[0]) + float(speed_range[1])) / 2.0
    return None


def pace_line(user_data):
    """Строка темпа для анонса или None, если темп не задан."""
    speed_range = user_data.get('speed_range')
    if speed_range:
        return f"Средняя скорость: {format_speed_range(speed_range)}"
    pace = user_data.get('pace')
    if pace:
        return f"Ожидаемый темп: {pace.split(' ')[0]}"
    return None

RU_WEEKDAYS = [
    'Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье'
]

def get_time_of_day(dt: datetime) -> str:
    """Определяет время суток на основе datetime объекта (работает с timezone-aware)"""
    if dt.hour < 12:
        return 'утро'
    elif dt.hour < 18:
        return 'день'
    else:
        return 'вечер'

def extract_route_name_from_gpx(gpx_path: str) -> str:
    """Извлекает название маршрута из GPX файла"""
    try:
        import xml.etree.ElementTree as ET

        tree = ET.parse(gpx_path)
        root = tree.getroot()

        # Поиск названия в metadata
        metadata = root.find('{http://www.topografix.com/GPX/1/1}metadata')
        if metadata is not None:
            name_elem = metadata.find('{http://www.topografix.com/GPX/1/1}name')
            if name_elem is not None and name_elem.text:
                return name_elem.text.strip()

        # Поиск названия в trk (track)
        trk = root.find('{http://www.topografix.com/GPX/1/1}trk')
        if trk is not None:
            name_elem = trk.find('{http://www.topografix.com/GPX/1/1}name')
            if name_elem is not None and name_elem.text:
                return name_elem.text.strip()

        # Если ничего не нашли, возвращаем пустую строку
        return ""

    except Exception as e:
        logger.error(f"Ошибка при извлечении названия из GPX: {e}")
        return ""

def detect_timezone_from_gpx(gpx_path: str) -> str | None:
    """Определяет таймзону по первой точке GPX-трека."""
    try:
        with open(gpx_path, 'r', encoding='utf-8') as f:
            gpx = gpxpy.parse(f)

        for track in gpx.tracks:
            for segment in track.segments:
                for point in segment.points:
                    if point.latitude is None or point.longitude is None:
                        continue
                    tf = TimezoneFinder()
                    tz_name = tf.timezone_at(lat=point.latitude, lng=point.longitude)
                    if tz_name:
                        return tz_name
        return None
    except Exception as e:
        logger.warning(f"Не удалось определить timezone по GPX: {e}")
        return None


def parse_date_time(date_time_str: str, timezone_name: str | None = None) -> tuple[datetime, str]:
    """
    Парсит строку даты и времени, возвращает (datetime, error_message)
    Ожидается формат: 19.07 10:00
    Всегда возвращает datetime с временной зоной timezone_name (или TIMEZONE)
    """
    try:
        # Парсим дату без timezone
        dt_naive = datetime.strptime(date_time_str, '%d.%m %H:%M')
        # Подставляем текущий год
        dt_naive = dt_naive.replace(year=datetime.now().year)

        # Получаем временную зону из настроек
        try:
            tz = pytz.timezone(timezone_name or TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            # Если указана неизвестная timezone, используем UTC
            logger.warning(f"Неизвестная временная зона: {timezone_name or TIMEZONE}, используем UTC")
            tz = pytz.UTC

        # Создаем timezone-aware datetime
        dt = tz.localize(dt_naive)

        # Получаем текущее время в той же timezone для сравнения
        now = datetime.now(tz)

        # Проверяем, что дата не в прошлом
        if dt.date() < now.date():
            return None, "❌ <b>Указанная дата уже прошла!</b> Укажи будущую дату."
        elif dt.date() == now.date() and dt.time() <= now.time():
            return None, "❌ <b>Указанное время уже прошло!</b> Укажи время в будущем."

        # Проверяем, что дата не слишком далеко в будущем (больше года)
        if dt > now + timedelta(days=365):
            return None, "❌ <b>Дата слишком далеко в будущем!</b> Укажи дату в пределах года."

        return dt, None

    except ValueError:
        return None, "❌ <b>Неверный формат даты!</b> Используй формат: ДД.ММ ЧЧ:ММ (например: 19.07 10:00)"
    except Exception as e:
        logger.error(f"Ошибка при обработке даты: {str(e)}", exc_info=True)
        return None, f"❌ <b>Ошибка при обработке даты:</b> {str(e)}"

def load_ready_routes():
    """Загружает готовые маршруты из JSON файла"""
    try:
        with open('ready_routes.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            routes = data.get('ready_routes', [])
            logger.info(f"Загружено готовых маршрутов: {len(routes)}")
            for i, route in enumerate(routes):
                logger.info(f"Маршрут {i+1}: {route['name']} -> {route['start_point']}")
            return routes
    except FileNotFoundError:
        logger.warning("Файл ready_routes.json не найден")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка при парсинге ready_routes.json: {e}")
        return []
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке готовых маршрутов: {e}")
        return []

# Загружаем готовые маршруты при импорте модуля
READY_ROUTES = load_ready_routes()

def load_route_comments():
    """Загружает готовые ссылки на маршруты из JSON файла"""
    try:
        with open('routes.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            routes = data.get('routes', [])
            logger.info(f"Загружено готовых ссылок на маршруты: {len(routes)}")
            return routes
    except FileNotFoundError:
        logger.warning("Файл routes.json не найден")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка при парсинге routes.json: {e}")
        return []
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке готовых ссылок: {e}")
        return []

# Загружаем готовые ссылки на маршруты при импорте модуля
ROUTE_COMMENTS = load_route_comments()

async def quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для быстрого создания анонса из готового маршрута"""
    # Очищаем все данные пользователя перед началом новой сессии
    context.user_data.clear()
    
    logger.info(f"quick_command вызван, READY_ROUTES: {len(READY_ROUTES)}")
    
    if not READY_ROUTES:
        logger.warning("READY_ROUTES пуст")
        await update.message.reply_text(
            "❌ Готовые маршруты не найдены. Обратитесь к администратору."
        )
        return ConversationHandler.END
    
    # Создаем клавиатуру с готовыми маршрутами
    keyboard = []
    for i, route in enumerate(READY_ROUTES):
        keyboard.append([f"{i+1}. {route['name']}"])
        logger.info(f"Добавлена кнопка: {i+1}. {route['name']}")
    
    keyboard.append(["❌ Отмена"])
    
    await update.message.reply_text(
        "🚴‍♂️ <b>Выбери готовый маршрут:</b>\n\n"
        "Просто выбери маршрут, и тебе нужно будет указать только дату, время и темп!",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    
    # Сохраняем состояние для обработки выбора маршрута
    context.user_data['quick_mode'] = True
    logger.info("quick_command завершен, возвращаем SELECT_ROUTE")
    return SELECT_ROUTE

async def handle_route_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор готового маршрута"""
    text = update.message.text.strip()
    logger.info(f"handle_route_selection вызван с текстом: '{text}'")
    
    # Проверяем отмену
    if text == "❌ Отмена":
        logger.info("Пользователь отменил выбор маршрута")
        await update.message.reply_text(
            "❌ Выбор маршрута отменен.\n\n"
            "Используй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    # Проверяем, что это выбор маршрута
    # Кнопка вида "6. Название": номер не ограничен пятью маршрутами
    if re.match(r'^\d+\.', text):
        try:
            route_index = int(text.split('.')[0]) - 1
            logger.info(f"Выбран маршрут с индексом: {route_index}")
            if 0 <= route_index < len(READY_ROUTES):
                route = READY_ROUTES[route_index]
                logger.info(f"Загружен маршрут: {route['name']}")
                context.user_data.update({
                    'komoot_link': route['komoot_link'],
                    'route_name': route['name'],
                    'start_point_name': route['start_point'],
                    'start_point_link': route['start_point_link'],
                    'comment': route['comment'],
                    'quick_mode': True
                })
                
                await update.message.reply_text(
                    f"🚴‍♂️ <b>Выбран готовый маршрут:</b>\n\n"
                    f"<b>{route['name']}</b>\n"
                    f"📍 Старт: {route['start_point']}\n"
                    f"💬 {route['comment']}\n\n"
                    f"Теперь укажи дату и время старта (например: <code>26.08 10:00</code>)",
                    parse_mode='HTML',
                    reply_markup=ReplyKeyboardRemove()
                )
                return ASK_DATE
            else:
                logger.warning(f"Индекс маршрута {route_index} вне диапазона")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка при обработке выбора маршрута: {e}")
            pass
    
    # Если это не выбор маршрута, возвращаемся к выбору
    logger.info("Неверный выбор маршрута, показываем список снова")
    keyboard = []
    for i, route in enumerate(READY_ROUTES):
        keyboard.append([f"{i+1}. {route['name']}"])
    keyboard.append(["❌ Отмена"])
    
    await update.message.reply_text(
        "Пожалуйста, выбери маршрут из списка выше.",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return SELECT_ROUTE

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Очищаем все данные пользователя перед началом новой сессии
    context.user_data.clear()
    
    # Проверяем, есть ли готовый маршрут в команде
    command_args = update.message.text.split()
    if len(command_args) > 1 and command_args[1].isdigit():
        # Если передан номер маршрута, загружаем его
        route_index = int(command_args[1]) - 1
        if 0 <= route_index < len(READY_ROUTES):
            route = READY_ROUTES[route_index]
            context.user_data.update({
                'komoot_link': route['komoot_link'],
                'route_name': route['name'],
                'start_point_name': route['start_point'],
                'start_point_link': route['start_point_link'],
                'comment': route['comment'],
                'quick_mode': True
            })
            await update.message.reply_text(
                f"🚴‍♂️ <b>Выбран готовый маршрут:</b>\n\n"
                f"<b>{route['name']}</b>\n"
                f"📍 Старт: {route['start_point']}\n"
                f"💬 {route['comment']}\n\n"
                f"Теперь укажи дату и время старта (например: <code>26.08 10:00</code>)",
                parse_mode='HTML'
            )
            return ASK_DATE
    
    # Обычный старт
    # Первое сообщение - приветствие и команды
    await update.message.reply_text(
        f'🚴‍♂️ <b>Привет! Я бот для создания анонсов велопоездок</b>\n\n'
        f'Создам красивый анонс с маршрутом, точкой старта и всеми деталями.\n\n'
        f'<b>Основные команды:</b>\n'
        f'• /start - создать новый анонс\n'
        f'• /weather - дашборд погоды по ссылке Komoot\n'
        f'• /help - показать справку\n'
        f'• /restart - сбросить состояние',
        parse_mode='HTML'
    )

    # Начинаем с выбора даты
    return await ask_date(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда помощи"""
    help_text = (
        "🚴‍♂️ <b>Справка по боту</b>\n\n"
        "<b>Как создать анонс:</b>\n"
        "1. Укажи дату и время старта (формат: ДД.ММ ЧЧ:ММ)\n"
        "2. Пришли ссылку на маршрут Komoot\n"
        "3. Введи название маршрута\n"
        "4. Выбери точку старта\n"
        "5. Укажи темп: луны, средняя скорость (например 25-28) или пропусти\n"
        "6. Добавь комментарий\n"
        "7. Проверь и отправь анонс\n\n"
        "<b>Основные команды:</b>\n"
        "• /start - создать новый анонс\n"
        "• /weather <komoot_link> - получить дашборд погоды без диалога\n"
        "• /help - эта справка\n"
        "• /restart - сбросить состояние\n\n"
        "<b>Точки старта:</b>\n"
        f"• {', '.join(p['name'] for p in START_POINTS if not p.get('custom'))}\n"
        "• Или укажи свою точку\n\n"
        "<b>Формат даты:</b> ДД.ММ ЧЧ:ММ (например: 19.07 10:00)"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрая команда: генерирует дашборд погоды только по ссылке Komoot."""
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "<code>/weather https://www.komoot.com/tour/123456789</code>",
            parse_mode='HTML'
        )
        return

    komoot_link = " ".join(context.args).strip()
    match = KOMOOT_LINK_PATTERN.search(komoot_link)
    if not match:
        await update.message.reply_text(
            "❌ Неверный формат ссылки.\n"
            "Пришли публичную ссылку Komoot в формате:\n"
            "<code>/weather https://www.komoot.com/tour/123456789</code>",
            parse_mode='HTML'
        )
        return

    tour_id = match.group(3)

    await update.message.reply_text(
        "🖼️ Генерирую дашборд по маршруту...\n"
        "Обычно это занимает 10–30 секунд ⏳"
    )

    # Переиспользуем кэшированный GPX, если есть
    gpx_files = glob.glob(f"{CACHE_DIR}/*-{tour_id}.gpx")
    gpx_path = gpx_files[0] if gpx_files else None

    if not gpx_path:
        try:
            process = await asyncio.create_subprocess_exec(
                'komootgpx',
                '-d', tour_id,
                '-o', CACHE_DIR,
                '-e',
                '-n',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
            except asyncio.TimeoutError:
                process.kill()
                await update.message.reply_text("❌ Таймаут при скачивании GPX из Komoot.")
                return

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Неизвестная ошибка"
                await update.message.reply_text(f"❌ Ошибка при скачивании GPX: {error_msg}")
                return
        except Exception as e:
            logger.error(f"Ошибка в /weather при скачивании GPX: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка при скачивании GPX: {str(e)}")
            return

        gpx_files = glob.glob(f"{CACHE_DIR}/*-{tour_id}.gpx")
        if not gpx_files:
            await update.message.reply_text("❌ GPX-файл не найден после скачивания.")
            return
        gpx_path = gpx_files[0]

    # Время старта для прогноза: текущее время в timezone маршрута (если удалось определить)
    route_timezone = detect_timezone_from_gpx(gpx_path) or TIMEZONE
    try:
        tz = pytz.timezone(route_timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.UTC
    start_dt = datetime.now(tz)

    dashboard_path = f"dashboard_weather_{tour_id}_{start_dt.strftime('%Y%m%d_%H%M%S')}.png"
    success = generate_weather_dashboard(gpx_path, start_dt, dashboard_path)

    if not success or not os.path.exists(dashboard_path):
        await update.message.reply_text(
            "❌ Не удалось сгенерировать дашборд.\n"
            "Попробуй позже."
        )
        return

    route_name = extract_route_name_from_gpx(gpx_path) or f"Маршрут {tour_id}"
    caption = (
        f"🖼️ <b>Дашборд маршрута</b>\n"
        f"Маршрут: <b>{route_name}</b>\n"
        f"Время прогноза: {start_dt.strftime('%d.%m.%Y %H:%M')} ({route_timezone})\n"
        f"<a href=\"{komoot_link}\">Открыть маршрут в Komoot</a>"
    )

    with open(dashboard_path, 'rb') as photo:
        await update.message.reply_photo(
            photo=photo,
            caption=caption,
            parse_mode='HTML'
        )

async def ask_date_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сохраняем дату и время в user_data
    date_time_str = update.message.text.strip()

    # Валидируем дату
    dt, error_msg = parse_date_time(date_time_str)
    if error_msg:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return ASK_DATE

    context.user_data['date_time'] = date_time_str
    context.user_data['parsed_datetime'] = dt  # Сохраняем распарсенную дату

    # Проверяем быстрый режим
    if context.user_data.get('quick_mode'):
        # В быстром режиме после даты спрашиваем темп
        context.user_data['quick_mode'] = False
        keyboard = pace_keyboard()
        await update.message.reply_text(
            '✅ Дата и время приняты!\n\n'
            'Теперь выбери ожидаемый темп (луны), напиши среднюю скорость (например 25-28) или пропусти:',
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

    if context.user_data.get('edit_mode'):
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    # Создаем клавиатуру с готовыми ссылками
    keyboard = []
    keyboard.append(["🚫 Без трека"])
    for route in ROUTE_COMMENTS:
        keyboard.append([f"🔗 {route['name']}"])

    keyboard.append(["❌ Отмена"])

    await update.message.reply_text(
        '✅ Дата и время приняты!\n\n'
        'Теперь пришли <b>публичную</b> ссылку на маршрут Komoot\n\n'
        'Или выбери готовый маршрут:\n'
        '• 🚫 <b>Без трека</b> - если плана маршрута нет',
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_KOMOOT_LINK

async def ask_manual_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод описания маршрута вручную"""
    text = update.message.text.strip()
    
    if not text or len(text) < 1:
        await update.message.reply_text(
            "❌ <b>Описание не может быть пустым!</b>\n\n"
            "Пожалуйста, опиши маршрут:\n"
            "• Куда планируете ехать\n"
            "• Примерное расстояние\n"
            "• Набор высоты\n"
            "• Особенности маршрута",
            parse_mode='HTML'
        )
        return ASK_MANUAL_ROUTE
    
    # Сохраняем описание маршрута
    context.user_data['manual_route_description'] = text
    context.user_data['route_name'] = "Маршрут без трека"
    
    await update.message.reply_text(
        f"✅ <b>Описание маршрута принято:</b>\n\n"
        f"<i>{text}</i>\n\n"
        f"Теперь выбери точку старта:",
        parse_mode='HTML'
    )
    
    # Переходим к выбору точки старта
    keyboard = [[p['name']] for p in START_POINTS]
    await update.message.reply_text(
        "Выбери точку старта:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_START_POINT

async def ask_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает выбор даты"""
    # Создаем кнопки с датами: сегодня, завтра, послезавтра
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.UTC
    now = datetime.now(tz)
    dates = []

    for i in range(3):
        date = now + timedelta(days=i)
        if i == 0:
            date_text = f"📅 Сегодня ({date.strftime('%d.%m')})"
        elif i == 1:
            date_text = f"📅 Завтра ({date.strftime('%d.%m')})"
        else:
            date_text = f"📅 Послезавтра ({date.strftime('%d.%m')})"
        dates.append([date_text])

    dates.append(["❌ Отмена"])

    await update.message.reply_text(
        '🚴‍♂️ <b>Выбери дату старта</b>\n\n'
        '• Выбери дату из списка ниже\n'
        '• Или введи дату в формате: <code>ДД.ММ</code> (например: <code>25.12</code>)',
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(dates, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_DATE

async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает выбор времени"""
    # Создаем кнопки с временем
    times = [
        ["🌅 06:00"],
        ["🌅 06:30"],
        ["🌅 07:00"],
        ["🌅 07:30"],
        ["☀️ 08:00"],
        ["☀️ 08:30"],
        ["☀️ 09:00"],
        ["☀️ 09:30"],
        ["☀️ 10:00"],
        ["☀️ 10:30"],
        ["🌞 11:00"],
        ["🌞 11:30"],
        ["🌞 12:00"],
        ["🌆 18:00"],
        ["🌆 18:30"],
        ["🌆 19:00"],
        ["🌆 19:30"],
        ["🌙 20:00"],
        ["❌ Отмена"]
    ]

    await update.message.reply_text(
        '🚴‍♂️ <b>Выбери время старта</b>\n\n'
        '• Выбери время из списка ниже\n'
        '• Или введи время в формате: <code>ЧЧ:ММ</code> (например: <code>08:30</code>)',
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(times, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_TIME

async def handle_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор даты"""
    text = update.message.text.strip()

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Выбор даты отменен.\n\nИспользуй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Извлекаем дату из текста кнопки
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.UTC
    now = datetime.now(tz)
    selected_date = None

    # Проверяем, является ли текст датой в формате ДД.ММ
    date_match = re.match(r'^(\d{1,2})\.(\d{1,2})$', text)
    if date_match:
        try:
            day, month = map(int, date_match.groups())
            # Создаем дату с текущим годом
            selected_date = datetime(now.year, month, day).date()

            # Проверяем, что дата не в прошлом (если это текущий год)
            if selected_date < now.date() and selected_date.year == now.year:
                await update.message.reply_text(
                    "❌ <b>Указанная дата уже прошла!</b> Выбери будущую дату.",
                    parse_mode='HTML'
                )
                return ASK_DATE

        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат даты. Используй формат ДД.ММ (например: 25.12)",
                reply_markup=ReplyKeyboardRemove()
            )
            return ASK_DATE
    else:
        # Проверяем кнопки
        if "Сегодня" in text:
            selected_date = now.date()
        elif "Завтра" in text:
            selected_date = (now + timedelta(days=1)).date()
        elif "Послезавтра" in text:
            selected_date = (now + timedelta(days=2)).date()
        else:
            await update.message.reply_text(
                "❌ Неизвестная дата. Выбери из списка или введи в формате ДД.ММ",
                reply_markup=ReplyKeyboardRemove()
            )
            return ASK_DATE

    # Сохраняем выбранную дату
    context.user_data['selected_date'] = selected_date

    # Переходим к выбору времени
    return await ask_time(update, context)

async def handle_time_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор времени"""
    text = update.message.text.strip()

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Выбор времени отменен.\n\nИспользуй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Сначала проверяем, является ли текст временем в формате ЧЧ:ММ
    time_match = re.match(r'^(\d{1,2}):(\d{2})$', text)
    selected_time = None

    if time_match:
        try:
            hour, minute = map(int, time_match.groups())
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                selected_time = datetime.strptime(text, '%H:%M').time()
            else:
                await update.message.reply_text(
                    "❌ Неверное время. Часы должны быть от 00 до 23, минуты от 00 до 59.",
                    reply_markup=ReplyKeyboardRemove()
                )
                return ASK_TIME
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат времени. Используй формат ЧЧ:ММ (например: 08:30)",
                reply_markup=ReplyKeyboardRemove()
            )
            return ASK_TIME
    else:
        # Проверяем кнопки с временем
        time_buttons = {
            "🌅 06:00": "06:00",
            "🌅 06:30": "06:30",
            "🌅 07:00": "07:00",
            "🌅 07:30": "07:30",
            "☀️ 08:00": "08:00",
            "☀️ 08:30": "08:30",
            "☀️ 09:00": "09:00",
            "☀️ 09:30": "09:30",
            "☀️ 10:00": "10:00",
            "☀️ 10:30": "10:30",
            "🌞 11:00": "11:00",
            "🌞 11:30": "11:30",
            "🌞 12:00": "12:00",
            "🌆 18:00": "18:00",
            "🌆 18:30": "18:30",
            "🌆 19:00": "19:00",
            "🌆 19:30": "19:30",
            "🌙 20:00": "20:00"
        }

        if text in time_buttons:
            selected_time_str = time_buttons[text]
            selected_time = datetime.strptime(selected_time_str, '%H:%M').time()
        else:
            await update.message.reply_text(
                "❌ Неизвестное время. Выбери из списка или введи в формате ЧЧ:ММ",
                reply_markup=ReplyKeyboardRemove()
            )
            return ASK_TIME
    selected_date = context.user_data.get('selected_date')

    if not selected_date:
        await update.message.reply_text(
            "❌ Дата не найдена. Начни заново.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Создаем полный datetime
    selected_datetime_naive = datetime.combine(selected_date, selected_time)

    # Получаем временную зону и создаем timezone-aware datetime
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.UTC
    selected_datetime = tz.localize(selected_datetime_naive)

    # Валидируем дату и время (не в прошлом)
    now = datetime.now(tz)
    if selected_datetime.date() < now.date():
        await update.message.reply_text(
            "❌ <b>Указанная дата уже прошла!</b> Выбери будущую дату.",
            parse_mode='HTML'
        )
        return ASK_DATE
    elif selected_datetime.date() == now.date() and selected_datetime.time() <= now.time():
        await update.message.reply_text(
            "❌ <b>Указанное время уже прошло!</b> Выбери время в будущем.",
            parse_mode='HTML'
        )
        return ASK_TIME

    # Сохраняем дату и время
    date_time_str = selected_datetime.strftime('%d.%m %H:%M')
    context.user_data['date_time'] = date_time_str
    context.user_data['parsed_datetime'] = selected_datetime

    # Проверяем быстрый режим
    if context.user_data.get('quick_mode'):
        # В быстром режиме после даты спрашиваем темп
        context.user_data['quick_mode'] = False
        keyboard = pace_keyboard()
        await update.message.reply_text(
            f'✅ Дата и время приняты: <b>{date_time_str}</b>\n\n'
            'Теперь выбери ожидаемый темп (луны), напиши среднюю скорость (например 25-28) или пропусти:',
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

    if context.user_data.get('edit_mode'):
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    # Создаем клавиатуру с готовыми ссылками
    keyboard = []
    keyboard.append(["🚫 Без трека"])
    for route in ROUTE_COMMENTS:
        keyboard.append([f"🔗 {route['name']}"])

    keyboard.append(["❌ Отмена"])

    await update.message.reply_text(
        f'✅ Дата и время приняты: <b>{date_time_str}</b>\n\n'
        'Теперь пришли <b>публичную</b> ссылку на маршрут Komoot\n\n'
        'Или выбери готовый маршрут:\n'
        '• 🚫 <b>Без трека</b> - если плана маршрута нет',
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_KOMOOT_LINK

async def ask_komoot_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info(f"ask_komoot_link вызван с текстом: '{text}'")
    logger.info(f"ROUTE_COMMENTS загружено: {len(ROUTE_COMMENTS)}")
    
    # Проверяем, не выбрана ли готовая ссылка
    if text.startswith("🔗 "):
        route_name = text[2:]  # Убираем эмодзи
        logger.info(f"Выбрана готовая ссылка: '{route_name}'")
        # Ищем маршрут по названию
        selected_route = None
        for route in ROUTE_COMMENTS:
            if route['name'] == route_name:
                selected_route = route
                break
        
        if selected_route:
            # Автоматически вставляем готовую ссылку
            text = selected_route['link']
            logger.info(f"Найден маршрут: {selected_route['name']} -> {selected_route['link']}")
            await update.message.reply_text(
                f"✅ Выбран готовый маршрут: <b>{selected_route['name']}</b>\n\n"
                f"Ссылка: {selected_route['link']}",
                parse_mode='HTML',
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            logger.warning(f"Маршрут не найден: '{route_name}'")
            await update.message.reply_text(
                "❌ Маршрут не найден. Попробуй еще раз.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ASK_KOMOOT_LINK
    
    # Проверяем, выбрана ли опция "Без трека"
    if text == "🚫 Без трека":
        logger.info("Пользователь выбрал 'Без трека'")
        context.user_data['no_track'] = True
        context.user_data['komoot_link'] = None
        context.user_data['tour_id'] = None
        context.user_data['gpx_path'] = None
        context.user_data['length_km'] = None
        context.user_data['uphill'] = None
        
        await update.message.reply_text(
            "🚫 <b>Создаем анонс без трека</b>\n\n"
            "Опиши маршрут в свободной форме:\n"
            "• Куда планируете ехать\n"
            "• Примерное расстояние\n"
            "• Набор высоты\n"
            "• Особенности маршрута\n\n"
            "Например: <i>Едем в сторону Нового Сада, примерно 50 км, набор 200м, по асфальту</i>",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )
        return ASK_MANUAL_ROUTE
    
    # Проверяем отмену
    if text == "❌ Отмена":
        logger.info("Пользователь отменил ввод ссылки")
        await update.message.reply_text(
            "❌ Ввод ссылки отменен.\n\n"
            "Используй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    match = KOMOOT_LINK_PATTERN.search(text)
    logger.info(f"Результат поиска ссылки: {match}")
    
    if not match:
        # Если ссылка некорректная, просто просим ввести правильную
        await update.message.reply_text(
            '❌ Неверный формат ссылки!\n\n'
            'Пожалуйста, пришли корректную публичную ссылку на маршрут Komoot\n\n'
            'Или используй кнопки выше для выбора готового маршрута.'
        )
        return ASK_KOMOOT_LINK
    
    context.user_data['komoot_link'] = text
    context.user_data['tour_id'] = match.group(3)

    # Всегда скачиваем GPX при изменении ссылки, независимо от режима редактирования
    if context.user_data.get('edit_mode'):
        # Если мы в режиме редактирования, после скачивания GPX вернемся к предпросмотру
        context.user_data['edit_mode'] = False
        context.user_data['after_gpx_edit'] = True

    # Переходим к обработке GPX
    return await process_gpx(update, context)

async def process_gpx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tour_id = context.user_data['tour_id']
    logger.info(f"Начинаю скачивание GPX для tour_id: {tour_id}")
    
    try:
        # Используем асинхронный subprocess
        process = await asyncio.create_subprocess_exec(
            'komootgpx',
            '-d', tour_id,
            '-o', CACHE_DIR,
            '-e',
            '-n',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        logger.info(f"Процесс komootgpx запущен с PID: {process.pid}")
        
        # Ждем завершения с таймаутом
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
            logger.info(f"Процесс komootgpx завершен с кодом: {process.returncode}")
        except asyncio.TimeoutError:
            # Если процесс завис, убиваем его
            logger.warning(f"Процесс komootgpx завис, убиваю PID: {process.pid}")
            process.kill()
            await update.message.reply_text('Превышено время ожидания при скачивании GPX. Попробуй другую ссылку на маршрут Komoot:')
            return ASK_KOMOOT_LINK
            
        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Неизвестная ошибка"
            logger.error(f"Ошибка komootgpx: {error_msg}")
            await update.message.reply_text(f'Ошибка при скачивании GPX: {error_msg}. Попробуй другую ссылку на маршрут Komoot:')
            return ASK_KOMOOT_LINK
            
    except Exception as e:
        logger.error(f"Исключение при скачивании GPX: {str(e)}", exc_info=True)
        await update.message.reply_text(f'Ошибка при скачивании GPX: {str(e)}. Попробуй другую ссылку на маршрут Komoot:')
        return ASK_KOMOOT_LINK
        
    # Проверяем, что файл действительно скачался
    gpx_files = glob.glob(f"{CACHE_DIR}/*-{tour_id}.gpx")
    if not gpx_files:
        logger.warning(f"GPX файл не найден для tour_id: {tour_id}")
        await update.message.reply_text('GPX-файл не найден. Попробуй другую ссылку на маршрут Komoot:')
        return ASK_KOMOOT_LINK
        
    gpx_path = gpx_files[0]
    logger.info(f"GPX файл найден: {gpx_path}")
    context.user_data['gpx_path'] = gpx_path
    route_timezone = detect_timezone_from_gpx(gpx_path)
    if route_timezone:
        context.user_data['route_timezone'] = route_timezone
        logger.info(f"Определена timezone по GPX: {route_timezone}")
        if context.user_data.get('date_time'):
            reparsed_dt, reparsed_error = parse_date_time(
                context.user_data['date_time'],
                route_timezone
            )
            if not reparsed_error:
                context.user_data['parsed_datetime'] = reparsed_dt
    
    try:
        with open(gpx_path, 'r') as f:
            gpx = gpxpy.parse(f)
        length_km = gpx.length_2d() / 1000
        uphill = gpx.get_uphill_downhill()[0]
        context.user_data['length_km'] = round(length_km)
        context.user_data['uphill'] = round(uphill)
        logger.info(f"GPX обработан: длина {length_km} км, набор {uphill} м")

        # Автоматически извлекаем название из GPX
        extracted_name = extract_route_name_from_gpx(gpx_path)

        # Пробуем уточнить длину/набор по данным Komoot (сглаженные значения точнее GPX)
        tour_meta = await asyncio.to_thread(fetch_komoot_tour_meta, tour_id)
        if tour_meta:
            if tour_meta.get('distance_m') is not None:
                context.user_data['length_km'] = round(tour_meta['distance_m'] / 1000)
            if tour_meta.get('elevation_up') is not None:
                context.user_data['uphill'] = round(tour_meta['elevation_up'])
            if not extracted_name and tour_meta.get('name'):
                extracted_name = tour_meta['name']
            logger.info(
                f"Метаданные Komoot: длина {context.user_data['length_km']} км, "
                f"набор {context.user_data['uphill']} м"
            )

        if extracted_name:
            context.user_data['extracted_name'] = extracted_name
            logger.info(f"Извлечено название из GPX: {extracted_name}")
        else:
            context.user_data['extracted_name'] = None
            logger.info("Название в GPX файле не найдено")

    except Exception as e:
        logger.error(f"Ошибка при обработке GPX файла: {str(e)}", exc_info=True)
        await update.message.reply_text('Ошибка при обработке GPX-файла. Попробуй другую ссылку на маршрут Komoot:')
        return ASK_KOMOOT_LINK

    # Создаем клавиатуру для выбора названия
    extracted_name = context.user_data.get('extracted_name')

    if extracted_name:
        keyboard = [
            ["✅ Оставить извлеченное"],
            ["✏️ Ввести другое"],
            ["❌ Отмена"]
        ]
        message_text = (
            f'🚴‍♂️ <b>Название маршрута из GPX:</b> <code>{extracted_name}</code>\n\n'
            f'Выбери действие:'
        )
    else:
        keyboard = [
            ["✏️ Ввести название"],
            ["❌ Отмена"]
        ]
        message_text = 'Введи название маршрута (например: Шайкаш - Чуруг - Србобран - Темерин):'

    # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
    if context.user_data.get('after_gpx_edit'):
        context.user_data['after_gpx_edit'] = False
        # После изменения GPX возвращаемся к предпросмотру
        return await preview_step(update, context)

    await update.message.reply_text(
        message_text,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_ROUTE_NAME

async def ask_route_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Проверяем отмену
    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Ввод названия отменен.\n\nИспользуй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Проверяем, выбрано ли извлеченное название
    extracted_name = context.user_data.get('extracted_name')
    if extracted_name and text == "✅ Оставить извлеченное":
        context.user_data['route_name'] = extracted_name
        await update.message.reply_text(
            f"✅ Название маршрута: <b>{extracted_name}</b>",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )

        # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
        if context.user_data.get('after_gpx_edit'):
            context.user_data['after_gpx_edit'] = False
            # После изменения GPX возвращаемся к предпросмотру
            return await preview_step(update, context)

        # Переходим к выбору точки старта
        keyboard = [[p['name']] for p in START_POINTS]
        await update.message.reply_text(
            f"Маршрут: {context.user_data['length_km']} км, набор: {context.user_data['uphill']} м\n\nВыбери точку старта:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_START_POINT

    # Проверяем, выбрано ли "Ввести другое" или "Ввести название"
    if text in ["✏️ Ввести другое", "✏️ Ввести название"]:
        if extracted_name:
            await update.message.reply_text(
                f'Текущее название из GPX: <code>{extracted_name}</code>\n\n'
                f'Введи новое название маршрута:',
                parse_mode='HTML',
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                'Введи название маршрута (например: Шайкаш - Чуруг - Србобран - Темерин):',
                reply_markup=ReplyKeyboardRemove()
            )
        return ASK_ROUTE_NAME

    # Обычный ввод названия
    context.user_data['route_name'] = text
    await update.message.reply_text(
        f"✅ Название маршрута: <b>{text}</b>",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )

    # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
    if context.user_data.get('after_gpx_edit'):
        context.user_data['after_gpx_edit'] = False
        # После изменения GPX возвращаемся к предпросмотру
        return await preview_step(update, context)

    if context.user_data.get('edit_mode'):
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    # Кнопки для выбора точки старта
    keyboard = [[p['name']] for p in START_POINTS]
    await update.message.reply_text(
        f"Маршрут: {context.user_data['length_km']} км, набор: {context.user_data['uphill']} м\n\nВыбери точку старта:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_START_POINT

async def ask_start_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    point_name = update.message.text.strip()
    point = next((p for p in START_POINTS if p['name'] == point_name), None)
    if not point:
        await update.message.reply_text('Пожалуйста, выбери точку старта из списка.')
        return ASK_START_POINT
    if point.get('custom'):
        context.user_data['start_point_name'] = None
        context.user_data['start_point_link'] = None
        await update.message.reply_text('Введи название своей точки старта:')
        return ASK_START_LINK
    else:
        context.user_data['start_point_name'] = point['name']
        context.user_data['start_point_link'] = point['link']

        # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
        if context.user_data.get('after_gpx_edit'):
            context.user_data['after_gpx_edit'] = False
            # После изменения GPX возвращаемся к предпросмотру
            return await preview_step(update, context)

        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)

        # Переходим к выбору точки финиша
        keyboard = [[p['name']] for p in FINISH_POINTS]
        keyboard.insert(0, ["🏁 Не нужно"])  # Добавляем опцию "Не нужно" в начало
        await update.message.reply_text(
            f"Маршрут: {context.user_data['length_km']} км, набор: {context.user_data['uphill']} м\n\n"
            f"Укажи точку финиша или выбери '🏁 Не нужно':",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_FINISH_POINT

async def ask_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если не было имени, значит сейчас ждём имя, иначе ждём ссылку
    if not context.user_data.get('start_point_name'):
        context.user_data['start_point_name'] = update.message.text.strip()
        await update.message.reply_text('Введи ссылку на Google Maps для своей точки старта:')
        return ASK_START_LINK
    else:
        link = update.message.text.strip()
        if not (link.startswith('http://') or link.startswith('https://')):
            await update.message.reply_text('Пожалуйста, пришли корректную ссылку на Google Maps (начинается с http...)')
            return ASK_START_LINK
        context.user_data['start_point_link'] = link

        # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
        if context.user_data.get('after_gpx_edit'):
            context.user_data['after_gpx_edit'] = False
            # После изменения GPX возвращаемся к предпросмотру
            return await preview_step(update, context)

        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)

        # Переходим к выбору точки финиша
        keyboard = [[p['name']] for p in FINISH_POINTS]
        keyboard.insert(0, ["🏁 Не нужно"])  # Добавляем опцию "Не нужно" в начало
        await update.message.reply_text(
            f"Маршрут: {context.user_data['length_km']} км, набор: {context.user_data['uphill']} м\n\n"
            f"Укажи точку финиша или выбери '🏁 Не нужно':",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_FINISH_POINT

async def ask_finish_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    point_name = update.message.text.strip()

    # Обработка опции "Не нужно"
    if point_name == "🏁 Не нужно":
        context.user_data['finish_point_name'] = None
        context.user_data['finish_point_link'] = None

        # Проверяем режим редактирования
        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)

        # Переходим к выбору темпа
        keyboard = pace_keyboard()
        await update.message.reply_text(
            PACE_PROMPT,
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

    # Поиск точки в списке FINISH_POINTS
    point = next((p for p in FINISH_POINTS if p['name'] == point_name), None)
    if not point:
        await update.message.reply_text('Пожалуйста, выбери точку финиша из списка.')
        return ASK_FINISH_POINT

    if point.get('custom'):
        context.user_data['finish_point_name'] = None
        context.user_data['finish_point_link'] = None
        await update.message.reply_text('Введи название своей точки финиша:')
        return ASK_FINISH_LINK
    else:
        context.user_data['finish_point_name'] = point['name']
        context.user_data['finish_point_link'] = point['link']

        # Проверяем режим редактирования
        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)

        # Переходим к выбору темпа
        keyboard = pace_keyboard()
        await update.message.reply_text(
            PACE_PROMPT,
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

async def ask_finish_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если не было имени, значит сейчас ждём имя, иначе ждём ссылку
    if not context.user_data.get('finish_point_name'):
        context.user_data['finish_point_name'] = update.message.text.strip()
        await update.message.reply_text('Введи ссылку на Google Maps для своей точки финиша:')
        return ASK_FINISH_LINK
    else:
        link = update.message.text.strip()
        if not (link.startswith('http://') or link.startswith('https://')):
            await update.message.reply_text('Пожалуйста, пришли корректную ссылку на Google Maps (начинается с http...)')
            return ASK_FINISH_LINK
        context.user_data['finish_point_link'] = link

        # Проверяем режим редактирования
        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)

        # Переходим к выбору темпа
        keyboard = pace_keyboard()
        await update.message.reply_text(
            PACE_PROMPT,
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

async def ask_pace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in PACE_OPTIONS:
        context.user_data['pace'] = text
        context.user_data['speed_range'] = None
    elif text == PACE_SKIP_BUTTON:
        context.user_data['pace'] = None
        context.user_data['speed_range'] = None
    else:
        speed_range = parse_speed_range(text)
        if not speed_range:
            await update.message.reply_text(
                'Не понял темп. Выбери луны из кнопок, напиши скорость '
                f'(например 25-28 или 27) или нажми «{PACE_SKIP_BUTTON}».',
                reply_markup=ReplyKeyboardMarkup(pace_keyboard(), one_time_keyboard=True, resize_keyboard=True)
            )
            return ASK_PACE
        if speed_range[0] < SPEED_MIN_KMH or speed_range[1] > SPEED_MAX_KMH:
            await update.message.reply_text(
                f'Скорость должна быть в пределах {SPEED_MIN_KMH}–{SPEED_MAX_KMH} км/ч.',
                reply_markup=ReplyKeyboardMarkup(pace_keyboard(), one_time_keyboard=True, resize_keyboard=True)
            )
            return ASK_PACE
        context.user_data['pace'] = None
        context.user_data['speed_range'] = speed_range

    # Проверяем быстрый режим
    if context.user_data.get('quick_mode'):
        # В быстром режиме после темпа сразу к предпросмотру
        context.user_data['quick_mode'] = False
        # Комментарий уже есть из готового маршрута
        return await preview_step(update, context)

    # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
    if context.user_data.get('after_gpx_edit'):
        context.user_data['after_gpx_edit'] = False
        # После изменения GPX возвращаемся к предпросмотру
        return await preview_step(update, context)

    if context.user_data.get('edit_mode'):
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)
    await update.message.reply_text(
        'Теперь напиши комментарий к анонсу (можно несколько строк):',
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_COMMENT

async def ask_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    context.user_data['comment'] = comment

    # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
    if context.user_data.get('after_gpx_edit'):
        context.user_data['after_gpx_edit'] = False
        # После изменения GPX возвращаемся к предпросмотру
        return await preview_step(update, context)

    if context.user_data.get('edit_mode'):
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    # В обычном потоке после комментария предлагаем картинку или дашборд
    no_track = context.user_data.get('no_track', False)

    if no_track:
        # Для маршрутов без трека предлагаем только картинку
        await update.message.reply_text(
            "📷 <b>Добавить картинку к анонсу?</b>\n\n"
            "• Пришли свою картинку\n"
            "• Или пропусти этот шаг",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["📷 Прислать картинку"],
                ["⏭️ Пропустить"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    else:
        await update.message.reply_text(
            "📷 <b>Добавить картинку к анонсу?</b>\n\n"
            "• <b>Дашборд заезда</b>: карта, профиль, подъёмы и прогноз погоды одной картинкой\n"
            "• Или пришли свою картинку\n"
            "• Или пропусти этот шаг",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                [DASHBOARD_BUTTON],
                ["📷 Прислать картинку"],
                ["⏭️ Пропустить"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    return ASK_IMAGE

async def generate_dashboard_for_announce(update: Update, context: ContextTypes.DEFAULT_TYPE, intro=None) -> bool:
    """Строит дашборд заезда и сохраняет путь в user_data. Возвращает успех."""
    gpx_path = context.user_data.get('gpx_path')
    parsed_datetime = context.user_data.get('parsed_datetime')

    if not gpx_path or not parsed_datetime:
        await update.message.reply_text(
            "❌ <b>Ошибка:</b> Не найден GPX файл или время старта",
            parse_mode='HTML'
        )
        return False

    await update.message.reply_text(
        intro or "🖼️ <b>Генерирую дашборд заезда...</b> ⏳\n\nОбычно это занимает 10–30 секунд.",
        parse_mode='HTML'
    )

    # Имя файла включает id пользователя: два чата с одним туром не должны
    # перезаписывать/удалять файлы друг друга
    user_id = update.effective_user.id if update.effective_user else 'anon'
    dashboard_path = f"dashboard_{user_id}_{context.user_data.get('tour_id', 'temp')}.png"
    success = await asyncio.to_thread(
        generate_ride_dashboard, gpx_path, dict(context.user_data), dashboard_path
    )

    if success:
        context.user_data['dashboard_path'] = dashboard_path
        context.user_data['dashboard_stale'] = False
        context.user_data['wants_dashboard'] = True
        await update.message.reply_text(
            "✅ <b>Дашборд готов.</b>",
            parse_mode='HTML'
        )
    else:
        kept = "Оставляем предыдущий дашборд." if current_dashboard_path(context) else "Продолжаем без дашборда."
        await update.message.reply_text(
            "❌ <b>Не удалось сгенерировать дашборд</b>\n\n"
            "Возможно, проблемы с интернетом или данными.\n"
            f"{kept}",
            parse_mode='HTML'
        )
    return success

async def ask_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор или изменение картинки для анонса"""
    # Пришло фото: своя картинка заменяет сгенерированный дашборд
    if update.message.photo:
        photo = update.message.photo[-1]
        context.user_data['announce_image'] = photo.file_id
        discard_dashboard(context)
        context.user_data['wants_dashboard'] = False

        await update.message.reply_text(
            "✅ <b>Картинка добавлена к анонсу!</b>",
            parse_mode='HTML'
        )
        context.user_data['after_gpx_edit'] = False
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    text = update.message.text.strip()

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Изменение картинки отменено.\n\nИспользуй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    if text in DASHBOARD_GENERATE_BUTTONS:
        context.user_data['edit_mode'] = False
        context.user_data['after_gpx_edit'] = False
        await generate_dashboard_for_announce(update, context)
        return await preview_step(update, context)

    if text in ("📷 Прислать картинку", "📷 Изменить картинку", "📷 Заменить картинкой"):
        keyboard = []
        if context.user_data.get('announce_image'):
            keyboard.append(["🗑️ Удалить картинку"])
        keyboard.append(["⏭️ Оставить как есть"])
        keyboard.append(["❌ Отмена"])
        await update.message.reply_text(
            "📷 <b>Пришли картинку для анонса</b>\n\n"
            "Или выбери действие:",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_IMAGE

    if text in ("⏭️ Пропустить", "⏭️ Оставить как есть", "⏭️ Оставить дашборд"):
        context.user_data['edit_mode'] = False
        context.user_data['after_gpx_edit'] = False
        return await preview_step(update, context)

    if text == "🗑️ Удалить картинку":
        context.user_data['announce_image'] = None
        await update.message.reply_text(
            "✅ <b>Картинка удалена из анонса!</b>",
            parse_mode='HTML'
        )
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    if text == "🗑️ Удалить дашборд":
        discard_dashboard(context)
        context.user_data['wants_dashboard'] = False
        await update.message.reply_text(
            "✅ <b>Дашборд удалён из анонса.</b>",
            parse_mode='HTML'
        )
        context.user_data['edit_mode'] = False
        return await preview_step(update, context)

    # Неизвестный текст: показываем доступные действия
    await update.message.reply_text(
        "❌ Пришли картинку или выбери действие из кнопок ниже:",
        reply_markup=ReplyKeyboardMarkup(image_step_keyboard(context), one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_IMAGE

def image_step_keyboard(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Кнопки шага картинки с учётом того, что уже есть в анонсе."""
    keyboard = []
    if context.user_data.get('gpx_path'):
        keyboard.append(["🔄 Обновить дашборд" if current_dashboard_path(context) else DASHBOARD_BUTTON])
    keyboard.append(["📷 Прислать картинку"])
    if context.user_data.get('announce_image'):
        keyboard.append(["🗑️ Удалить картинку"])
    if current_dashboard_path(context):
        keyboard.append(["🗑️ Удалить дашборд"])
    keyboard.append(["⏭️ Оставить как есть"])
    keyboard.append(["❌ Отмена"])
    return keyboard

def current_dashboard_path(context: ContextTypes.DEFAULT_TYPE):
    """Путь к дашборду анонса, если файл существует."""
    path = context.user_data.get('dashboard_path')
    return path if path and os.path.exists(path) else None

def discard_dashboard(context: ContextTypes.DEFAULT_TYPE):
    """Удаляет файл дашборда (и его конфиг) и ссылку на него в user_data."""
    path = context.user_data.get('dashboard_path')
    if path:
        remove_file_quietly(path)
        remove_file_quietly(os.path.join(CACHE_DIR, os.path.basename(path)))
        stem = os.path.splitext(os.path.basename(path))[0]
        remove_file_quietly(os.path.join(CACHE_DIR, f"{stem}_config.json"))
    context.user_data['dashboard_path'] = None
    context.user_data['dashboard_stale'] = False

def mark_dashboard_stale(context: ContextTypes.DEFAULT_TYPE):
    """Данные, запечённые в дашборд, изменились: перестроим его перед предпросмотром.

    Раньше при правке анонса картинка просто удалялась, и пользователь
    терял её; теперь она помечается устаревшей и пересобирается сама.
    """
    if context.user_data.get('wants_dashboard'):
        context.user_data['dashboard_stale'] = True

async def refresh_dashboard_if_stale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересобирает дашборд, если пользователь его выбирал, а данные с тех пор менялись."""
    if not context.user_data.get('wants_dashboard'):
        return
    if context.user_data.get('announce_image') or not context.user_data.get('gpx_path'):
        return
    if context.user_data.get('dashboard_stale') or not current_dashboard_path(context):
        await generate_dashboard_for_announce(
            update, context,
            intro="🔄 <b>Данные анонса изменились, обновляю дашборд...</b> ⏳"
        )

def remove_file_quietly(path):
    """Удаляет файл, логируя ошибку вместо падения."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning(f"Не удалось удалить файл {path}: {e}")

# Лимит Telegram на подпись к фото/альбому (в UTF-16 code units)
TELEGRAM_CAPTION_LIMIT = 1024

def utf16_len(text: str) -> int:
    """Длина строки так, как её считает Telegram (UTF-16 code units)."""
    return len(text.encode('utf-16-le')) // 2

async def send_announce_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   announce: str, reply_markup=None, confirm_text=None) -> None:
    """Отправляет анонс с картинкой (своя картинка приоритетнее дашборда) или текстом."""
    announce_image = context.user_data.get('announce_image')
    dashboard_path = current_dashboard_path(context)
    caption = announce + ('\n\n' + confirm_text if confirm_text else '')
    # Подпись к фото ограничена 1024 UTF-16 юнитами (текст сообщения — 4096),
    # поэтому длинный анонс отправляем отдельным сообщением после картинки
    caption_fits = utf16_len(caption) <= TELEGRAM_CAPTION_LIMIT

    if announce_image or dashboard_path:
        photo_source = announce_image if announce_image else open(dashboard_path, 'rb')
        try:
            if caption_fits:
                await update.message.reply_photo(
                    photo=photo_source,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_photo(photo=photo_source)
                await update.message.reply_text(
                    caption,
                    parse_mode='HTML',
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
        finally:
            if not announce_image:
                photo_source.close()
    else:
        await update.message.reply_text(
            caption,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

async def send_rsvp_poll(update: Update, description=None, media=None) -> None:
    """Публичный опрос-отметка. Описание и картинка делают его самим анонсом."""
    await update.message.reply_poll(
        question=RSVP_POLL_QUESTION,
        options=RSVP_POLL_OPTIONS,
        description=description,
        description_parse_mode='HTML' if description else None,
        media=media,
        is_anonymous=False,          # видно поимённо, кто едет
        allows_multiple_answers=False,
        allows_revoting=True,        # можно передумать и переголосовать
        allow_adding_options=False,  # свои варианты не добавляют
    )


async def send_announce_as_poll(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                announce: str) -> None:
    """Публикует анонс. Обычно это одно сообщение-опрос: картинка и текст живут
    в описании опроса, отметки — в нём же, поэтому пересылать нужно ровно одно
    сообщение, и голоса остаются общими для всех чатов.
    Слишком длинный анонс в описание не влезает — тогда шлём его отдельно."""
    announce_image = context.user_data.get('announce_image')
    dashboard_path = current_dashboard_path(context)

    if utf16_len(announce) > POLL_DESCRIPTION_LIMIT:
        await send_announce_with_media(update, context, announce + '\n\n' + RSVP_HINT)
        await send_rsvp_poll(update)
        return

    if announce_image:
        await send_rsvp_poll(update, description=announce,
                             media=InputMediaPhoto(announce_image))
    elif dashboard_path:
        with open(dashboard_path, 'rb') as photo:
            await send_rsvp_poll(update, description=announce,
                                 media=InputMediaPhoto(photo))
    else:
        await send_rsvp_poll(update, description=announce)

def build_announce_text(user_data) -> tuple:
    """Собирает текст анонса. Возвращает (text, None) или (None, error_html)."""
    date_time_str = user_data.get('date_time', '-')
    dt, error_msg = parse_date_time(date_time_str, user_data.get('route_timezone'))
    if not dt:
        return None, error_msg or "❌ <b>Дата не задана.</b>"

    weekday = RU_WEEKDAYS[dt.weekday()]
    time_of_day = get_time_of_day(dt)
    date_part = dt.strftime('%d.%m')
    time_part = dt.strftime('%H:%M')
    komoot_link = user_data.get('komoot_link', '-')
    route_name = user_data.get('route_name', '-')
    start_point_name = user_data.get('start_point_name', '-')
    start_point_link = user_data.get('start_point_link', '-')
    finish_point_name = user_data.get('finish_point_name')
    finish_point_link = user_data.get('finish_point_link')
    length_km = user_data.get('length_km', '-')
    uphill = user_data.get('uphill', '-')
    comment = user_data.get('comment', '-')

    if user_data.get('no_track', False):
        manual_description = user_data.get('manual_route_description', 'Маршрут без трека')
        announce_lines = [
            f"<b>{weekday}, {date_part}, {time_of_day} ({time_part})</b>",
            f"Маршрут: {manual_description}",
            "",
            f"Старт: <a href=\"{start_point_link}\">{start_point_name}</a>, выезд в {time_part}"
        ]
    else:
        announce_lines = [
            f"<b>{weekday}, {date_part}, {time_of_day} ({time_part})</b>",
            f"Маршрут: {route_name} ↔️ {length_km} км ⛰ {uphill} м (<a href=\"{komoot_link}\">комут</a>)",
            "",
            f"Старт: <a href=\"{start_point_link}\">{start_point_name}</a>, выезд в {time_part}"
        ]

    if finish_point_name and finish_point_link:
        announce_lines.append(f"Финиш: <a href=\"{finish_point_link}\">{finish_point_name}</a>")
    elif finish_point_name:
        announce_lines.append(f"Финиш: {finish_point_name}")

    # Темп необязателен: луны, диапазон скорости или ничего
    pace_text = pace_line(user_data)
    if pace_text:
        announce_lines.append(pace_text)

    announce_lines.extend([
        "",
        comment
    ])
    return "\n".join(announce_lines), None

async def preview_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если данные анонса менялись, дашборд пересобирается здесь, а не теряется
    await refresh_dashboard_if_stale(update, context)

    announce, error_msg = build_announce_text(context.user_data)
    if announce is None:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return ASK_DATE  # Вернуться к запросу даты

    # Кнопки предпросмотра
    buttons = [["✅ Отправить"]]

    announce_image = context.user_data.get('announce_image')
    no_track = context.user_data.get('no_track', False)

    if no_track:
        # Для маршрутов без трека показываем только картинку
        if announce_image:
            buttons.append(["🗑️ Удалить картинку"])
        else:
            buttons.append(["📷 Добавить картинку"])
    else:
        has_dashboard = bool(current_dashboard_path(context))
        # Без скачанного GPX (например, /quick) генерация невозможна — кнопки не предлагаем
        has_gpx = bool(context.user_data.get('gpx_path'))

        if announce_image:
            buttons.append(["🗑️ Удалить картинку"])
        elif has_dashboard:
            buttons.append(["🔄 Обновить дашборд"])
            buttons.append(["🗑️ Удалить дашборд"])
            buttons.append(["📷 Заменить картинкой"])
        else:
            if has_gpx:
                buttons.append(["🖼️ Сгенерировать дашборд"])
            buttons.append(["📷 Добавить картинку"])

    for step, name in STEP_TO_NAME.items():
        buttons.append([name])

    await send_announce_with_media(
        update, context, announce,
        reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True),
        confirm_text='🗳️ Отметки «Едешь?» добавятся опросом прямо в это сообщение.\n\nВсё верно?'
    )
    return PREVIEW_STEP

async def preview_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Отправить — публикуем анонс и GPX
    if text == '✅ Отправить':
        announce, error_msg = build_announce_text(context.user_data)
        if announce is None:
            await update.message.reply_text(error_msg, parse_mode='HTML')
            return ASK_DATE
        gpx_path = context.user_data.get('gpx_path')
        no_track = context.user_data.get('no_track', False)

        # Анонс уходит одним сообщением-опросом (или двумя, если текст длинный)
        await send_announce_as_poll(update, context, announce)

        # Отправляем GPX файл только если есть трек
        if gpx_path and not no_track:
            with open(gpx_path, 'rb') as f:
                await update.message.reply_document(f, filename=os.path.basename(gpx_path))

        # Отправляем финальное сообщение
        await update.message.reply_text(
            "✅ <b>Анонс создан, можешь переслать его друзьям.</b>\n\n"
            "🗳️ <b>Отметки — опросом внутри самого анонса.</b> При пересылке это тот же "
            "опрос, так что голоса общие для всех чатов, куда он попал.\n\n"
            "🚴‍♂️ <b>Хорошей покатушки!</b>\n\n"
            "Используй /start для создания нового анонса.",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END

    # Обработка кнопок управления картинкой и дашбордом
    if text in ("📷 Добавить картинку", "📷 Заменить картинкой"):
        await update.message.reply_text(
            "📷 <b>Пришли картинку для анонса</b>\n\n"
            "Или выбери действие:",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["⏭️ Оставить как есть"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_IMAGE

    if text == "🗑️ Удалить картинку":
        context.user_data['announce_image'] = None
        await update.message.reply_text(
            "✅ <b>Картинка удалена из анонса!</b>",
            parse_mode='HTML'
        )
        return await preview_step(update, context)

    if text in DASHBOARD_GENERATE_BUTTONS:
        await generate_dashboard_for_announce(update, context)
        return await preview_step(update, context)

    if text == "🗑️ Удалить дашборд":
        discard_dashboard(context)
        context.user_data['wants_dashboard'] = False
        await update.message.reply_text(
            "✅ <b>Дашборд удалён из анонса.</b>",
            parse_mode='HTML'
        )
        return await preview_step(update, context)

    # Если выбрана кнопка редактирования — возвращаем на нужный этап
    for step, name in STEP_TO_NAME.items():
        if text == name:
            context.user_data['edit_mode'] = True
            # Всё, что запечено в дашборд (дата, время, название, старт, темп,
            # комментарий), помечает его устаревшим: preview_step пересоберёт
            if step in (ASK_DATE, ASK_TIME, ASK_ROUTE_NAME, ASK_START_POINT, ASK_PACE, ASK_COMMENT):
                mark_dashboard_stale(context)
            if step == ASK_DATE:
                return await ask_date(update, context)
            elif step == ASK_TIME:
                return await ask_time(update, context)
            elif step == ASK_KOMOOT_LINK:
                # Для изменения ссылки Komoot - сбрасываем GPX данные и просим новую ссылку
                context.user_data['gpx_path'] = None
                context.user_data['length_km'] = None
                context.user_data['uphill'] = None
                context.user_data['extracted_name'] = None
                # Дашборд относится к старому маршруту: удаляем файл, но помним,
                # что пользователь его хотел, и пересоберём для нового трека
                discard_dashboard(context)
                mark_dashboard_stale(context)
                keyboard = []
                for route in ROUTE_COMMENTS:
                    keyboard.append([f"🔗 {route['name']}"])
                keyboard.append(["❌ Отмена"])
                await update.message.reply_text(
                    'Пришли <b>публичную</b> ссылку на маршрут Komoot\n\n'
                    'Или выбери готовый маршрут:',
                    parse_mode='HTML',
                    reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
                )
                return ASK_KOMOOT_LINK
            elif step == ASK_ROUTE_NAME:
                await update.message.reply_text('Введи название маршрута:', reply_markup=ReplyKeyboardRemove())
                return ASK_ROUTE_NAME
            elif step == ASK_START_POINT:
                keyboard = [[p['name']] for p in START_POINTS]
                await update.message.reply_text('Выбери точку старта:', reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
                return ASK_START_POINT
            elif step == ASK_FINISH_POINT:
                keyboard = [[p['name']] for p in FINISH_POINTS]
                keyboard.insert(0, ["🏁 Не нужно"])  # Добавляем опцию "Не нужно" в начало
                await update.message.reply_text(
                    f"Маршрут: {context.user_data['length_km']} км, набор: {context.user_data['uphill']} м\n\n"
                    f"Укажи точку финиша или выбери '🏁 Не нужно':",
                    reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
                )
                return ASK_FINISH_POINT
            elif step == ASK_PACE:
                await update.message.reply_text(PACE_PROMPT, reply_markup=ReplyKeyboardMarkup(pace_keyboard(), one_time_keyboard=True, resize_keyboard=True))
                return ASK_PACE
            elif step == ASK_COMMENT:
                await update.message.reply_text('Введи комментарий:', reply_markup=ReplyKeyboardRemove())
                return ASK_COMMENT
            elif step == ASK_IMAGE:
                await update.message.reply_text(
                    "📷 <b>Пришли картинку для анонса</b>\n\n"
                    "Или выбери действие:",
                    parse_mode='HTML',
                    reply_markup=ReplyKeyboardMarkup(image_step_keyboard(context), one_time_keyboard=True, resize_keyboard=True)
                )
                return ASK_IMAGE
    # Если что-то другое — повторяем предпросмотр
    return await preview_step(update, context)

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для сброса состояния и начала заново"""
    # Очищаем все данные пользователя
    context.user_data.clear()
    await update.message.reply_text(
        "✅ <b>Состояние сброшено!</b> Начинаем заново.\n\n"
        "Используй /start для создания нового анонса.",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для проверки статуса бота"""
    cache_files = glob.glob(f"{CACHE_DIR}/*.gpx")
    cache_size = len(cache_files)
    
    # Проверяем размер кэша
    total_size = 0
    if cache_files:
        total_size = sum(os.path.getsize(f) for f in cache_files)
    
    status_text = f"🤖 <b>Статус бота</b>\n\n"
    status_text += f"📁 Файлов в кэше: {cache_size}\n"
    status_text += f"💾 Размер кэша: {total_size / 1024:.1f} KB\n"
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.UTC
    now = datetime.now(tz)
    status_text += f"⏰ Время ({TIMEZONE}): {now.strftime('%H:%M:%S')}\n"
    status_text += f"📅 Дата ({TIMEZONE}): {now.strftime('%d.%m.%Y')}\n"
    
    if cache_size > 10:
        status_text += "\n⚠️ Много файлов в кэше! Используй /clear_cache"
    elif cache_size == 0:
        status_text += "\n✅ Кэш пуст"
    else:
        status_text += f"\n✅ Кэш в порядке ({cache_size} файлов)"
    
    status_text += "\n🔄 Кэш автоматически очищается раз в 180 дней"
    
    await update.message.reply_text(status_text, parse_mode='HTML')

def cleanup_old_gpx_files():
    """Автоматически очищает GPX файлы старше 180 дней"""
    try:
        try:
            tz = pytz.timezone(TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.UTC
        current_time = datetime.now(tz)
        cache_files = glob.glob(f"{CACHE_DIR}/*.gpx")
        deleted_count = 0

        for file_path in cache_files:
            try:
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                # Создаем timezone-aware datetime для файла
                file_time_tz = tz.localize(file_time.replace(tzinfo=None))
                if (current_time - file_time_tz).days > 180:
                    os.remove(file_path)
                    logger.info(f"Автоматически удален старый файл: {file_path}")
                    deleted_count += 1
            except Exception as e:
                logger.error(f"Ошибка при проверке файла {file_path}: {e}")

        if deleted_count > 0:
            logger.info(f"Автоматически очищено {deleted_count} старых GPX файлов")

    except Exception as e:
        logger.error(f"Ошибка при автоматической очистке: {e}")

def cleanup_old_dashboards():
    """Автоматически очищает дашборды и постеры старше 180 дней"""
    try:
        try:
            tz = pytz.timezone(TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.UTC
        current_time = datetime.now(tz)
        dashboard_files = (
            glob.glob("dashboard_*.png")
            + glob.glob("poster_*.png")
            + glob.glob(f"{CACHE_DIR}/dashboard_*.png")
            + glob.glob(f"{CACHE_DIR}/dashboard_*_config.json")
            + glob.glob(f"{CACHE_DIR}/poster_*.png")
            + glob.glob(f"{CACHE_DIR}/poster_*_config.json")
        )
        deleted_count = 0

        for file_path in dashboard_files:
            try:
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                # Создаем timezone-aware datetime для файла
                file_time_tz = tz.localize(file_time.replace(tzinfo=None))
                if (current_time - file_time_tz).days > 180:
                    os.remove(file_path)
                    logger.info(f"Автоматически удален старый дашборд: {file_path}")
                    deleted_count += 1
            except Exception as e:
                logger.error(f"Ошибка при проверке дашборда {file_path}: {e}")

        if deleted_count > 0:
            logger.info(f"Автоматически очищено {deleted_count} старых дашбордов")

    except Exception as e:
        logger.error(f"Ошибка при автоматической очистке дашбордов: {e}")

async def preload_ready_routes():
    """Предварительно загружает все готовые маршруты в кеш"""
    logger.info("Начинаю предварительную загрузку готовых маршрутов в кеш...")
    
    for route in ROUTE_COMMENTS:
        try:
            # Извлекаем tour_id из ссылки
            match = KOMOOT_LINK_PATTERN.search(route['link'])
            if not match:
                logger.warning(f"Не удалось извлечь tour_id из ссылки: {route['link']}")
                continue
                
            tour_id = match.group(3)
            route_name = route['name']
            
            # Проверяем, есть ли уже файл в кеше
            gpx_files = glob.glob(f"{CACHE_DIR}/*-{tour_id}.gpx")
            if gpx_files:
                logger.info(f"Маршрут '{route_name}' уже в кеше, пропускаю")
                continue
            
            logger.info(f"Загружаю маршрут '{route_name}' (tour_id: {tour_id})")
            
            # Скачиваем GPX
            process = await asyncio.create_subprocess_exec(
                'komootgpx',
                '-d', tour_id,
                '-o', CACHE_DIR,
                '-e',
                '-n',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
                if process.returncode == 0:
                    logger.info(f"✅ Маршрут '{route_name}' успешно загружен в кеш")
                else:
                    error_msg = stderr.decode() if stderr else "Неизвестная ошибка"
                    logger.error(f"❌ Ошибка при загрузке маршрута '{route_name}': {error_msg}")
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Таймаут при загрузке маршрута '{route_name}', убиваю процесс")
                process.kill()
                
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка при загрузке маршрута '{route.get('name', 'Unknown')}': {e}")
    
    logger.info("Предварительная загрузка готовых маршрутов завершена")

def preload_ready_routes_sync():
    """Синхронная версия предзагрузки готовых маршрутов для запуска при старте"""
    logger.info("Начинаю синхронную предзагрузку готовых маршрутов в кеш...")

    for route in ROUTE_COMMENTS:
        try:
            # Извлекаем tour_id из ссылки
            match = KOMOOT_LINK_PATTERN.search(route['link'])
            if not match:
                logger.warning(f"Не удалось извлечь tour_id из ссылки: {route['link']}")
                continue

            tour_id = match.group(3)
            route_name = route['name']

            # Проверяем, есть ли уже файл в кеше
            gpx_files = glob.glob(f"{CACHE_DIR}/*-{tour_id}.gpx")
            if gpx_files:
                logger.info(f"Маршрут '{route_name}' уже в кеше, пропускаю")
                continue

            logger.info(f"Загружаю маршрут '{route_name}' (tour_id: {tour_id})")

            # Скачиваем GPX синхронно
            import subprocess
            try:
                result = subprocess.run(
                    ['komootgpx', '-d', tour_id, '-o', CACHE_DIR, '-e', '-n'],
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                if result.returncode == 0:
                    logger.info(f"✅ Маршрут '{route_name}' успешно загружен в кеш")
                else:
                    logger.error(f"❌ Ошибка при загрузке маршрута '{route_name}': {result.stderr}")

            except subprocess.TimeoutExpired:
                logger.warning(f"⏰ Таймаут при загрузке маршрута '{route_name}'")
            except FileNotFoundError:
                logger.error(f"❌ komootgpx не найден в системе")
                break

        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка при загрузке маршрута '{route.get('name', 'Unknown')}': {e}")

    logger.info("Синхронная предзагрузка готовых маршрутов завершена")

# Функции для генерации дашборда погоды

def build_dashboard_config(user_data, kicker=None):
    """Конфиг для ride_dashboard.py из данных анонса."""
    dt = user_data.get('parsed_datetime')
    speed_range = user_data.get('speed_range')
    route_name = user_data.get('route_name') or user_data.get('extracted_name') or ''
    config = {
        'kicker': kicker or (f"ROAD RIDE · {dt.strftime('%A')}" if dt else 'ROAD RIDE'),
        'route_name': route_name,
        'start_iso': dt.isoformat() if dt else None,
        'timezone': user_data.get('route_timezone'),
        'start': user_data.get('start_point_name') or '',
        'pace': PACE_TO_POSTER.get(user_data.get('pace')),
        'speed_range': [float(speed_range[0]), float(speed_range[1])] if speed_range else None,
        'speed_kmh': planned_speed_kmh(user_data),
        'notes': user_data.get('comment') or '',
        'distance_km': user_data.get('length_km'),
        'elevation_m': user_data.get('uphill'),
        'weather': True,
        'watermark': DASHBOARD_WATERMARK,
    }
    # Шрифт дашборда не умеет эмодзи — вычищаем их из пользовательского текста
    for key in ('route_name', 'start', 'notes'):
        config[key] = strip_unrenderable(config[key])
    return config

def generate_ride_dashboard(gpx_path, user_data, output_path="ride_dashboard.png", kicker=None):
    """Строит единый дашборд заезда через ride_dashboard.py (subprocess).

    Погода запрашивается внутри модуля; при её недоступности картинка
    всё равно строится (без погодных блоков).
    """
    try:
        cache_output_path = os.path.join(CACHE_DIR, output_path)
        config = build_dashboard_config(user_data, kicker=kicker)

        config_stem = os.path.splitext(os.path.basename(output_path))[0]
        config_path = os.path.join(CACHE_DIR, f"{config_stem}_config.json")
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False)

        cmd = [
            sys.executable, 'ride_dashboard.py',
            '--gpx', gpx_path,
            '--config', config_path,
            '--out', cache_output_path,
        ]
        logger.info(f"Вызываем ride_dashboard: {' '.join(cmd)}")

        # Таймаут — чтобы зависший процесс (сеть, тайлы) не заблокировал бота
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd(), timeout=300, env=env)

        if result.returncode == 0 and os.path.exists(cache_output_path):
            import shutil
            # Копия в корне — для совместимости с путями в user_data
            shutil.copy2(cache_output_path, output_path)
            logger.info(f"Дашборд создан: {cache_output_path}")
            return True

        logger.error(
            f"ride_dashboard завершился с кодом {result.returncode}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        return False

    except Exception as e:
        logger.error(f"Ошибка при вызове ride_dashboard: {e}", exc_info=True)
        return False

def generate_weather_dashboard(gpx_path, start_datetime, output_path="weather_dashboard.png", speed_kmh=DEFAULT_SPEED_KMH):
    """Дашборд для /weather: тот же ride_dashboard без данных анонса."""
    user_data = {
        'parsed_datetime': start_datetime,
        'route_name': extract_route_name_from_gpx(gpx_path),
        'speed_range': (speed_kmh, speed_kmh),
    }
    return generate_ride_dashboard(gpx_path, user_data, output_path, kicker='WEATHER CHECK')

# Эмодзи и прочие символы, которых нет в шрифте постера (DejaVu Sans):
# non-BMP (все современные эмодзи), misc symbols/dingbats, VS16, ZWJ
UNRENDERABLE_PATTERN = re.compile(
    '[{}-{}{}-{}{}-{}{}{}]'.format(
        chr(0x10000), chr(0x10FFFF),  # всё выше BMP (основная масса эмодзи)
        chr(0x2600), chr(0x27BF),     # misc symbols + dingbats
        chr(0x2B00), chr(0x2BFF),     # misc symbols and arrows
        chr(0xFE0F),                  # variation selector-16
        chr(0x200D),                  # zero-width joiner
    )
)

def strip_unrenderable(text):
    """Убирает символы, которые matplotlib отрисует как '?' на постере."""
    if not text:
        return text
    cleaned = UNRENDERABLE_PATTERN.sub('', str(text))
    return re.sub(r' {2,}', ' ', cleaned).strip()

async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для очистки кэша"""
    try:
        # Сначала очищаем старые файлы
        cleanup_old_gpx_files()

        cache_files = (
            glob.glob(f"{CACHE_DIR}/*.gpx")
            + glob.glob(f"{CACHE_DIR}/dashboard_*.png")
            + glob.glob(f"{CACHE_DIR}/dashboard_*_config.json")
            + glob.glob(f"{CACHE_DIR}/poster_*.png")
            + glob.glob(f"{CACHE_DIR}/poster_*_config.json")
        )
        deleted_count = 0
        
        for file_path in cache_files:
            try:
                os.remove(file_path)
                logger.info(f"Удален файл кэша: {file_path}")
                deleted_count += 1
            except Exception as e:
                logger.error(f"Ошибка при удалении {file_path}: {e}")
        
        if deleted_count == 0:
            await update.message.reply_text("🗑️ Кэш уже пуст!")
        else:
            await update.message.reply_text(f"🗑️ Кэш очищен! Удалено файлов: {deleted_count}")
        
    except Exception as e:
        logger.error(f"Ошибка при очистке кэша: {e}")
        await update.message.reply_text(f"❌ Ошибка при очистке кэша: {str(e)}")

if __name__ == '__main__':
    # Логируем информацию о временной зоне
    try:
        tz = pytz.timezone(TIMEZONE)
        logger.info(f"Используемая временная зона: {TIMEZONE}")
        logger.info(f"Текущее время: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    except pytz.exceptions.UnknownTimeZoneError:
        logger.warning(f"Неизвестная временная зона: {TIMEZONE}, используем UTC")
        TIMEZONE = 'UTC'

    # Автоматически очищаем старые GPX файлы и дашборды при запуске
    cleanup_old_gpx_files()
    cleanup_old_dashboards()

    # Предварительно загружаем все готовые маршруты в кеш
    preload_ready_routes_sync()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем команды статуса и очистки кэша
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('clear_cache', clear_cache_command))
    app.add_handler(CommandHandler('weather', weather_command))
    app.add_handler(CommandHandler('help', help_command))


    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('quick', quick_command)
        ],
        states={
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_selection)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_selection)],
            ASK_KOMOOT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_komoot_link)],
            ASK_MANUAL_ROUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_manual_route)],
            ASK_ROUTE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_route_name)],
            ASK_START_POINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_start_point)],
            ASK_START_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_start_link)],
            ASK_FINISH_POINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_finish_point)],
            ASK_FINISH_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_finish_link)],
            ASK_PACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pace)],
            ASK_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_comment)],
            ASK_IMAGE: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, ask_image)],
            PREVIEW_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, preview_handler)],
            SELECT_ROUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_route_selection)],
        },
        fallbacks=[CommandHandler('restart', restart_command)],
    )
    app.add_handler(conv_handler)
    print('Bot started...')
    app.run_polling() 
