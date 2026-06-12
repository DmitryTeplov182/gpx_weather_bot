import os
import re
import sys
import json
import subprocess
import requests
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputMediaPhoto,
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

            # Автоматически добавляем "Свою точку" в конец списка
            points.append({
                'name': 'Своя точка',
                'link': None
            })

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
    """Возвращает точки по умолчанию"""
    default_points = [
        {'name': 'koferajd', 'link': 'https://maps.app.goo.gl/iTBcRqjvhJ9DYvRK7'},
        {'name': 'Флаги', 'link': 'https://maps.app.goo.gl/j95ME2cuzX8k9hnj7'},
        {'name': 'Лидл Лиман', 'link': 'https://maps.app.goo.gl/5JKtAgGBVe48jM9r7'},
        {'name': 'Железничка Парк', 'link': 'https://maps.app.goo.gl/hSZ9C4Xue5RVpMea8'},
        {'name': 'Макси у Бульвара Европы', 'link': 'https://maps.app.goo.gl/3ZbDZM9VVRBDDddp8?g_st=ipc'},
    ]

    # Добавляем "Свою точку"
    default_points.append({
        'name': 'Своя точка',
        'link': None
    })

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

# Маппинг лун на шкалу темпа постера (1.0–3.0)
PACE_TO_POSTER = dict(zip(PACE_OPTIONS, [1.0, 1.5, 2.0, 2.5, 3.0]))

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
    if text.startswith(('1.', '2.', '3.', '4.', '5.')):
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
        "5. Укажи ожидаемый темп (количество лун)\n"
        "6. Добавь комментарий\n"
        "7. Проверь и отправь анонс\n\n"
        "<b>Основные команды:</b>\n"
        "• /start - создать новый анонс\n"
        "• /weather <komoot_link> - получить дашборд погоды без диалога\n"
        "• /help - эта справка\n"
        "• /restart - сбросить состояние\n\n"
        "<b>Точки старта:</b>\n"
        "• koferajd, Флаги, Лидл Лиман, Железничка Парк\n"
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
        "🌤️ Генерирую дашборд погоды по маршруту...\n"
        "Это может занять 1-2 минуты ⏳"
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
            "❌ Не удалось сгенерировать дашборд погоды.\n"
            "Попробуй позже."
        )
        return

    route_name = extract_route_name_from_gpx(gpx_path) or f"Маршрут {tour_id}"
    caption = (
        f"🌤️ <b>Погодный дашборд</b>\n"
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
        keyboard = [[p] for p in PACE_OPTIONS]
        await update.message.reply_text(
            '✅ Дата и время приняты!\n\n'
            'Теперь выбери ожидаемый темп (количество лун):',
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
        keyboard = [[p] for p in PACE_OPTIONS]
        await update.message.reply_text(
            f'✅ Дата и время приняты: <b>{date_time_str}</b>\n\n'
            'Теперь выбери ожидаемый темп (количество лун):',
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
    if point['name'] == 'Своя точка':
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
        keyboard = [[p] for p in PACE_OPTIONS]
        await update.message.reply_text(
            'Выбери ожидаемый темп (количество лун):',
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

    # Поиск точки в списке FINISH_POINTS
    point = next((p for p in FINISH_POINTS if p['name'] == point_name), None)
    if not point:
        await update.message.reply_text('Пожалуйста, выбери точку финиша из списка.')
        return ASK_FINISH_POINT

    if point['name'] == 'Своя точка':
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
        keyboard = [[p] for p in PACE_OPTIONS]
        await update.message.reply_text(
            'Выбери ожидаемый темп (количество лун):',
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
        keyboard = [[p] for p in PACE_OPTIONS]
        await update.message.reply_text(
            'Выбери ожидаемый темп (количество лун):',
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_PACE

async def ask_pace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pace = update.message.text.strip()
    if pace not in PACE_OPTIONS:
        await update.message.reply_text('Пожалуйста, выбери темп из предложенных вариантов.')
        return ASK_PACE
    context.user_data['pace'] = pace

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

    # В обычном потоке после комментариев предлагаем добавить картинку или дашборд
    no_track = context.user_data.get('no_track', False)
    
    if no_track:
        # Для маршрутов без трека предлагаем только картинку
        await update.message.reply_text(
            "📷 <b>Хотите добавить картинку к анонсу?</b>\n\n"
            "Вы можете:\n"
            "• Прислать свою картинку\n"
            "• Или пропустить этот шаг",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["📷 Прислать картинку"],
                ["⏭️ Пропустить"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    else:
        # Для маршрутов с треком предлагаем все опции
        await update.message.reply_text(
            "📷 <b>Хотите добавить картинку к анонсу?</b>\n\n"
            "Вы можете:\n"
            "• Сгенерировать <b>дашборд погоды</b> для маршрута\n"
            "• Сгенерировать <b>постер заезда</b>\n"
            "• Сгенерировать <b>оба</b> изображения\n"
            "• Прислать свою картинку\n"
            "• Или пропустить этот шаг",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["🌤️ Дашборд погоды"],
                ["🖼️ Постер заезда"],
                ["🌤️🖼️ Оба"],
                ["📷 Прислать картинку"],
                ["⏭️ Пропустить"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    return ASK_IMAGE

async def generate_dashboard_for_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Генерирует дашборд погоды и сохраняет путь в user_data. Возвращает успех."""
    gpx_path = context.user_data.get('gpx_path')
    parsed_datetime = context.user_data.get('parsed_datetime')

    if not gpx_path or not parsed_datetime:
        await update.message.reply_text(
            "❌ <b>Ошибка:</b> Не найден GPX файл или время старта",
            parse_mode='HTML'
        )
        return False

    await update.message.reply_text(
        "🌤️ <b>Генерирую дашборд погоды...</b>\n\n"
        "Это может занять 1-2 минуты ⏳",
        parse_mode='HTML'
    )

    dashboard_path = f"dashboard_{context.user_data.get('tour_id', 'temp')}.png"
    success = await asyncio.to_thread(
        generate_weather_dashboard, gpx_path, parsed_datetime, dashboard_path
    )

    if success:
        context.user_data['dashboard_path'] = dashboard_path
        await update.message.reply_text(
            "✅ <b>Дашборд погоды успешно сгенерирован!</b>",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "❌ <b>Не удалось сгенерировать дашборд погоды</b>\n\n"
            "Возможно, проблемы с интернетом или данными.\n"
            "Продолжаем без дашборда.",
            parse_mode='HTML'
        )
    return success

async def generate_poster_for_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Генерирует постер заезда и сохраняет путь в user_data. Возвращает успех."""
    gpx_path = context.user_data.get('gpx_path')

    if not gpx_path:
        await update.message.reply_text(
            "❌ <b>Ошибка:</b> Не найден GPX файл",
            parse_mode='HTML'
        )
        return False

    await update.message.reply_text(
        "🖼️ <b>Генерирую постер заезда...</b> ⏳",
        parse_mode='HTML'
    )

    poster_path = f"poster_{context.user_data.get('tour_id', 'temp')}.png"
    success = await asyncio.to_thread(
        generate_ride_poster, gpx_path, dict(context.user_data), poster_path
    )

    if success:
        context.user_data['poster_path'] = poster_path
        await update.message.reply_text(
            "✅ <b>Постер заезда успешно сгенерирован!</b>",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "❌ <b>Не удалось сгенерировать постер заезда</b>\n\n"
            "Продолжаем без постера.",
            parse_mode='HTML'
        )
    return success

async def ask_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор или изменение картинки для анонса"""
    # Проверяем, пришло ли фото
    if update.message.photo:
        # Получаем самое большое фото
        photo = update.message.photo[-1]
        context.user_data['announce_image'] = photo.file_id

        await update.message.reply_text(
            "✅ <b>Картинка добавлена к анонсу!</b>",
            parse_mode='HTML'
        )

        # Проверяем, находимся ли мы в режиме редактирования после изменения GPX
        if context.user_data.get('after_gpx_edit'):
            context.user_data['after_gpx_edit'] = False
            # После изменения возвращаемся к предпросмотру
            return await preview_step(update, context)

        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)

        # Если это не режим редактирования, возвращаемся к предпросмотру
        return await preview_step(update, context)

    # Проверяем текстовые команды
    text = update.message.text.strip()

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Изменение картинки отменено.\n\nИспользуй /start для создания нового анонса.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    if text in ("🌤️ Дашборд погоды", "🖼️ Постер заезда", "🌤️🖼️ Оба",
                "🌤️ Сгенерировать дашборд погоды (BETA)"):
        if text in ("🖼️ Постер заезда", "🌤️🖼️ Оба"):
            await generate_poster_for_announce(update, context)
        if text in ("🌤️ Дашборд погоды", "🌤️🖼️ Оба", "🌤️ Сгенерировать дашборд погоды (BETA)"):
            await generate_dashboard_for_announce(update, context)
        return await preview_step(update, context)

    if text == "📷 Прислать картинку":
        await update.message.reply_text(
            "📷 <b>Пришлите картинку для анонса</b>\n\n"
            "Или отправьте команду:",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["⏭️ Пропустить"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_IMAGE

    if text == "⏭️ Пропустить":
        # Пропускаем добавление картинки, переходим к предпросмотру
        return await preview_step(update, context)

    if text == "🗑️ Удалить картинку":
        context.user_data['announce_image'] = None
        await update.message.reply_text(
            "✅ <b>Картинка удалена из анонса!</b>",
            parse_mode='HTML'
        )

        if context.user_data.get('edit_mode'):
            context.user_data['edit_mode'] = False
            return await preview_step(update, context)
        return await preview_step(update, context)

    if text == "📷 Изменить картинку":
        await update.message.reply_text(
            "📷 <b>Пришлите картинку для анонса</b>\n\n"
            "Или отправьте команду:",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["🗑️ Удалить картинку"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_IMAGE

    if text in ("⏭️ Оставить дашборд", "⏭️ Оставить как есть"):
        # Возвращаемся к предпросмотру, оставляя сгенерированные изображения
        return await preview_step(update, context)

    # Если пришел неизвестный текст
    # Проверяем режим редактирования
    if context.user_data.get('edit_mode'):
        await update.message.reply_text(
            "❌ Пожалуйста, пришлите картинку или выберите действие из кнопок ниже:",
            reply_markup=ReplyKeyboardMarkup([
                ["🗑️ Удалить картинку"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    elif context.user_data.get('dashboard_path') or context.user_data.get('poster_path'):
        # Если есть сгенерированные изображения, показываем опции для замены
        await update.message.reply_text(
            "❌ Пожалуйста, пришлите картинку или выберите действие из кнопок ниже:",
            reply_markup=ReplyKeyboardMarkup([
                ["⏭️ Оставить как есть"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    else:
        await update.message.reply_text(
            "❌ Пожалуйста, пришлите картинку или выберите действие из кнопок ниже:",
            reply_markup=ReplyKeyboardMarkup([
                ["⏭️ Пропустить"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
    return ASK_IMAGE

def get_announce_attachments(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Возвращает пути к сгенерированным изображениям анонса (постер, дашборд)."""
    paths = []
    for key in ('poster_path', 'dashboard_path'):
        path = context.user_data.get(key)
        if path and os.path.exists(path):
            paths.append(path)
    return paths

async def send_announce_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   announce: str, reply_markup=None, confirm_text=None) -> None:
    """Отправляет анонс с картинкой/сгенерированными изображениями или текстом.

    Своя картинка имеет приоритет; иначе постер и/или дашборд.
    Два изображения уходят альбомом — caption на первом, а confirm_text
    с клавиатурой отдельным сообщением (к media group их прикрепить нельзя).
    """
    announce_image = context.user_data.get('announce_image')
    attachments = get_announce_attachments(context)
    caption = announce + ('\n\n' + confirm_text if confirm_text else '')

    if announce_image:
        await update.message.reply_photo(
            photo=announce_image,
            caption=caption,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
    elif len(attachments) == 1:
        with open(attachments[0], 'rb') as image:
            await update.message.reply_photo(
                photo=image,
                caption=caption,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
    elif len(attachments) >= 2:
        media = []
        opened_files = []
        try:
            for i, path in enumerate(attachments):
                f = open(path, 'rb')
                opened_files.append(f)
                if i == 0:
                    media.append(InputMediaPhoto(f, caption=announce, parse_mode='HTML'))
                else:
                    media.append(InputMediaPhoto(f))
            await update.message.reply_media_group(media=media)
        finally:
            for f in opened_files:
                f.close()
        if confirm_text or reply_markup:
            await update.message.reply_text(
                confirm_text or 'Анонс выше 👆',
                reply_markup=reply_markup
            )
    else:
        await update.message.reply_text(
            caption,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

async def preview_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Формируем анонс
    date_time_str = context.user_data.get('date_time', '-')
    dt, error_msg = parse_date_time(date_time_str, context.user_data.get('route_timezone'))
    if dt:
        weekday = RU_WEEKDAYS[dt.weekday()]
        time_of_day = get_time_of_day(dt)
        date_part = dt.strftime('%d.%m')
        time_part = dt.strftime('%H:%M')
    else:
        weekday = 'День недели?'
        time_of_day = 'время суток?'
        date_part = date_time_str
        time_part = ''
        if error_msg:
            await update.message.reply_text(error_msg, parse_mode='HTML')
            return ASK_DATE # Вернуться к запросу даты
    komoot_link = context.user_data.get('komoot_link', '-')
    route_name = context.user_data.get('route_name', '-')
    start_point_name = context.user_data.get('start_point_name', '-')
    start_point_link = context.user_data.get('start_point_link', '-')
    finish_point_name = context.user_data.get('finish_point_name')
    finish_point_link = context.user_data.get('finish_point_link')
    length_km = context.user_data.get('length_km', '-')
    uphill = context.user_data.get('uphill', '-')
    pace = context.user_data.get('pace', '-')
    comment = context.user_data.get('comment', '-')
    gpx_path = context.user_data.get('gpx_path', None)
    pace_emoji = pace.split(' ')[0] if pace else '-'
    # Формируем текст анонса
    no_track = context.user_data.get('no_track', False)
    
    if no_track:
        # Для маршрутов без трека используем ручное описание
        manual_description = context.user_data.get('manual_route_description', 'Маршрут без трека')
        announce_lines = [
            f"<b>{weekday}, {date_part}, {time_of_day} ({time_part})</b>",
            f"Маршрут: {manual_description}",
            "",
            f"Старт: <a href=\"{start_point_link}\">{start_point_name}</a>, выезд в {time_part}"
        ]
    else:
        # Для маршрутов с треком используем обычную логику
        announce_lines = [
            f"<b>{weekday}, {date_part}, {time_of_day} ({time_part})</b>",
            f"Маршрут: {route_name} ↔️ {length_km} км ⛰ {uphill} м (<a href=\"{komoot_link}\">комут</a>)",
            "",
            f"Старт: <a href=\"{start_point_link}\">{start_point_name}</a>, выезд в {time_part}"
        ]

    # Добавляем финиш, если он указан
    if finish_point_name and finish_point_link:
        announce_lines.append(f"Финиш: <a href=\"{finish_point_link}\">{finish_point_name}</a>")
    elif finish_point_name:
        announce_lines.append(f"Финиш: {finish_point_name}")

    announce_lines.extend([
        f"Ожидаемый темп: {pace_emoji}",
        "",
        comment,
        "",
        "Ставьте реакцию если собираетесь поехать"
    ])

    announce = "\n".join(announce_lines)
    # Кнопки предпросмотра
    buttons = [["✅ Отправить"]]

    # Добавляем кнопки для управления картинкой или дашбордом (только если есть трек)
    announce_image = context.user_data.get('announce_image')
    dashboard_path = context.user_data.get('dashboard_path')
    no_track = context.user_data.get('no_track', False)

    if no_track:
        # Для маршрутов без трека показываем только картинку
        if announce_image:
            buttons.append(["🗑️ Удалить картинку"])
        else:
            buttons.append(["📷 Добавить картинку"])
    else:
        # Для маршрутов с треком показываем все опции
        has_dashboard = dashboard_path and os.path.exists(dashboard_path)
        poster_path = context.user_data.get('poster_path')
        has_poster = poster_path and os.path.exists(poster_path)

        if announce_image:
            buttons.append(["🗑️ Удалить картинку"])
        else:
            if has_dashboard:
                buttons.append(["🗑️ Удалить дашборд"])
            else:
                buttons.append(["🌤️ Сгенерировать дашборд"])
            if has_poster:
                buttons.append(["🗑️ Удалить постер"])
            else:
                buttons.append(["🖼️ Сгенерировать постер"])
            if has_dashboard or has_poster:
                buttons.append(["📷 Заменить картинкой"])
            else:
                buttons.append(["📷 Добавить картинку"])

    for step, name in STEP_TO_NAME.items():
        buttons.append([name])

    await send_announce_with_media(
        update, context, announce,
        reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True),
        confirm_text='Всё верно?'
    )
    return PREVIEW_STEP

async def preview_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Отправить — публикуем анонс и GPX
    if text == '✅ Отправить':
        # Формируем анонс (тот же код, что в preview_step)
        date_time_str = context.user_data.get('date_time', '-')
        dt, error_msg = parse_date_time(date_time_str, context.user_data.get('route_timezone'))
        if dt:
            weekday = RU_WEEKDAYS[dt.weekday()]
            time_of_day = get_time_of_day(dt)
            date_part = dt.strftime('%d.%m')
            time_part = dt.strftime('%H:%M')
        else:
            weekday = 'День недели?'
            time_of_day = 'время суток?'
            date_part = date_time_str
            time_part = ''
            if error_msg:
                await update.message.reply_text(error_msg, parse_mode='HTML')
                return ASK_DATE # Вернуться к запросу даты
        komoot_link = context.user_data.get('komoot_link', '-')
        route_name = context.user_data.get('route_name', '-')
        start_point_name = context.user_data.get('start_point_name', '-')
        start_point_link = context.user_data.get('start_point_link', '-')
        finish_point_name = context.user_data.get('finish_point_name')
        finish_point_link = context.user_data.get('finish_point_link')
        length_km = context.user_data.get('length_km', '-')
        uphill = context.user_data.get('uphill', '-')
        pace = context.user_data.get('pace', '-')
        comment = context.user_data.get('comment', '-')
        gpx_path = context.user_data.get('gpx_path', None)
        pace_emoji = pace.split(' ')[0] if pace else '-'

        # Формируем текст анонса
        no_track = context.user_data.get('no_track', False)
        
        if no_track:
            # Для маршрутов без трека используем ручное описание
            manual_description = context.user_data.get('manual_route_description', 'Маршрут без трека')
            announce_lines = [
                f"<b>{weekday}, {date_part}, {time_of_day} ({time_part})</b>",
                f"Маршрут: {manual_description}",
                "",
                f"Старт: <a href=\"{start_point_link}\">{start_point_name}</a>, выезд в {time_part}"
            ]
        else:
            # Для маршрутов с треком используем обычную логику
            announce_lines = [
                f"<b>{weekday}, {date_part}, {time_of_day} ({time_part})</b>",
                f"Маршрут: {route_name} ↔️ {length_km} км ⛰ {uphill} м (<a href=\"{komoot_link}\">комут</a>)",
                "",
                f"Старт: <a href=\"{start_point_link}\">{start_point_name}</a>, выезд в {time_part}"
            ]

        # Добавляем финиш, если он указан
        if finish_point_name and finish_point_link:
            announce_lines.append(f"Финиш: <a href=\"{finish_point_link}\">{finish_point_name}</a>")
        elif finish_point_name:
            announce_lines.append(f"Финиш: {finish_point_name}")

        announce_lines.extend([
            f"Ожидаемый темп: {pace_emoji}",
            "",
            comment,
            "",
            "Ставьте реакцию если собираетесь поехать"
        ])

        announce = "\n".join(announce_lines)

        # Отправляем анонс с картинкой/постером/дашбордом или текстом
        await send_announce_with_media(update, context, announce)
        
        # Отправляем GPX файл только если есть трек
        if gpx_path and not no_track:
            with open(gpx_path, 'rb') as f:
                await update.message.reply_document(f, filename=os.path.basename(gpx_path))
        
        # Отправляем финальное сообщение
        await update.message.reply_text(
            "✅ <b>Анонс создан, можешь переслать его друзьям.</b>\n\n"
            "🚴‍♂️ <b>Хорошей покатушки!</b>\n\n"
            "Используй /start для создания нового анонса.",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )
        
        return ConversationHandler.END

    # Обработка кнопок управления картинкой и дашбордом
    if text == "📷 Добавить картинку":
        await update.message.reply_text(
            "📷 <b>Пришлите картинку для анонса</b>\n\n"
            "Или отправьте команду:",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["🗑️ Удалить картинку"],
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

    if text in ("🌤️ Сгенерировать дашборд", "🌤️ Сгенерировать дашборд (BETA)"):
        await generate_dashboard_for_announce(update, context)
        return await preview_step(update, context)

    if text == "🖼️ Сгенерировать постер":
        await generate_poster_for_announce(update, context)
        return await preview_step(update, context)

    if text == "🗑️ Удалить дашборд":
        dashboard_path = context.user_data.get('dashboard_path')
        if dashboard_path and os.path.exists(dashboard_path):
            try:
                os.remove(dashboard_path)
            except:
                pass
        context.user_data['dashboard_path'] = None
        await update.message.reply_text(
            "✅ <b>Дашборд удален из анонса!</b>",
            parse_mode='HTML'
        )
        return await preview_step(update, context)

    if text == "🗑️ Удалить постер":
        poster_path = context.user_data.get('poster_path')
        if poster_path and os.path.exists(poster_path):
            try:
                os.remove(poster_path)
            except:
                pass
        context.user_data['poster_path'] = None
        await update.message.reply_text(
            "✅ <b>Постер удален из анонса!</b>",
            parse_mode='HTML'
        )
        return await preview_step(update, context)

    if text == "📷 Заменить картинкой":
        await update.message.reply_text(
            "📷 <b>Пришлите картинку для замены сгенерированных изображений</b>\n\n"
            "Или отправьте команду:",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup([
                ["⏭️ Оставить как есть"],
                ["❌ Отмена"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_IMAGE

    # Если выбрана кнопка редактирования — возвращаем на нужный этап
    for step, name in STEP_TO_NAME.items():
        if text == name:
            context.user_data['edit_mode'] = True
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
                # Сгенерированные изображения относятся к старому маршруту
                context.user_data['dashboard_path'] = None
                context.user_data['poster_path'] = None
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
                keyboard = [[p] for p in PACE_OPTIONS]
                await update.message.reply_text('Выбери ожидаемый темп (количество лун):', reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
                return ASK_PACE
            elif step == ASK_COMMENT:
                await update.message.reply_text('Введи комментарий:', reply_markup=ReplyKeyboardRemove())
                return ASK_COMMENT
            elif step == ASK_IMAGE:
                await update.message.reply_text(
                    "📷 <b>Пришлите картинку для анонса</b>\n\n"
                    "Или отправьте команду:",
                    parse_mode='HTML',
                    reply_markup=ReplyKeyboardMarkup([
                        ["🗑️ Удалить картинку"],
                        ["❌ Отмена"]
                    ], one_time_keyboard=True, resize_keyboard=True)
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
    """Автоматически очищает дашборды старше 180 дней"""
    try:
        try:
            tz = pytz.timezone(TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.UTC
        current_time = datetime.now(tz)
        dashboard_files = glob.glob("dashboard_*.png")
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

def generate_weather_dashboard(gpx_path, start_datetime, output_path="weather_dashboard.png", speed_kmh=27):
    """Генерирует дашборд погоды для маршрута через внешний модуль"""
    try:
        # Формируем путь к выходному файлу в папке cache
        cache_output_path = os.path.join("cache", output_path)
        
        # Форматируем дату и время для внешнего модуля
        date_str = start_datetime.strftime('%d.%m.%Y')
        time_str = start_datetime.strftime('%H:%M')
        
        # Вызываем внешний модуль weather_dashboard.py
        cmd = [
            'python3', 'weather_dashboard.py',
            gpx_path,
            '-o', cache_output_path,
            '-s', str(speed_kmh),
            '-d', date_str,
            '-t', time_str
        ]
        
        print(f"🌤️ Вызываем внешний модуль: {' '.join(cmd)}")
        
        # Выполняем команду
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
        
        if result.returncode == 0:
            print(f"✅ Дашборд успешно создан: {cache_output_path}")
            # Копируем файл из cache в корневую папку для совместимости
            if os.path.exists(cache_output_path):
                import shutil
                shutil.copy2(cache_output_path, output_path)
                return True
            else:
                print(f"❌ Файл не найден: {cache_output_path}")
                return False
        else:
            print(f"❌ Ошибка выполнения внешнего модуля:")
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")
            return False

    except Exception as e:
        print(f"❌ Ошибка при вызове внешнего модуля: {e}")
        return False

def generate_ride_poster(gpx_path, user_data, output_path="ride_poster.png"):
    """Генерирует постер заезда через внешний модуль ride_poster.py

    Собирает config из данных анонса (название, дата, время, старт, темп,
    длина, набор) и вызывает ride_poster.py как subprocess.
    """
    try:
        cache_output_path = os.path.join(CACHE_DIR, output_path)

        # Все текстовые поля задаём явно, чтобы не всплыли примеры из default_config()
        config = {
            'title': 'GROUP ROAD RIDE',
            'subtitle': '',
            'route_name': '',
            'date': '—',
            'time': '—',
            'start_label': 'START',
            'start': '—',
            'notes': '',
            'climb_names': ['Climb 1', 'Climb 2', 'Climb 3', 'Climb 4'],
        }

        dt = user_data.get('parsed_datetime')
        if dt:
            # Формат как в постере: "Sat 18 Apr 2026" (%a/%b в C-локали — английские)
            config['date'] = dt.strftime('%a %d %b %Y')
            config['time'] = dt.strftime('%H:%M')

        route_name = user_data.get('route_name') or user_data.get('extracted_name')
        if route_name:
            config['subtitle'] = route_name
            config['route_name'] = route_name

        start_point = user_data.get('start_point_name')
        if start_point:
            config['start'] = start_point

        pace = user_data.get('pace')
        if pace in PACE_TO_POSTER:
            config['pace'] = PACE_TO_POSTER[pace]

        # Длина и набор из анонса (Komoot/GPX), чтобы цифры совпадали с текстом
        if user_data.get('length_km') is not None:
            config['distance_km'] = user_data['length_km']
        if user_data.get('uphill') is not None:
            config['elevation_m'] = user_data['uphill']

        comment = user_data.get('comment')
        if comment:
            config['notes'] = comment

        config_path = os.path.join(CACHE_DIR, f"poster_config_{user_data.get('tour_id', 'temp')}.json")
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False)

        cmd = [
            sys.executable, 'ride_poster.py',
            '--gpx', gpx_path,
            '--config', config_path,
            '--out', cache_output_path,
        ]

        print(f"🖼️ Вызываем внешний модуль: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())

        if result.returncode == 0:
            print(f"✅ Постер успешно создан: {cache_output_path}")
            # Копируем файл из cache в корневую папку для совместимости
            if os.path.exists(cache_output_path):
                import shutil
                shutil.copy2(cache_output_path, output_path)
                return True
            else:
                print(f"❌ Файл не найден: {cache_output_path}")
                return False
        else:
            print(f"❌ Ошибка выполнения внешнего модуля:")
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")
            return False

    except Exception as e:
        print(f"❌ Ошибка при вызове внешнего модуля: {e}")
        return False

async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для очистки кэша"""
    try:
        # Сначала очищаем старые файлы
        cleanup_old_gpx_files()
        
        cache_files = glob.glob(f"{CACHE_DIR}/*.gpx")
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
