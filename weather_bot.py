import asyncio
import glob
import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta

import pytz
import gpxpy
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from weather_dashboard import detect_timezone_from_gpx, get_timezone

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ASK_KOMOOT_LINK, ASK_DATE, ASK_TIME, ASK_SPEED = range(4)
RESTART_BUTTON = "Restart"

# Для отдельного погодного бота можно задать отдельный токен:
# WEATHER_TELEGRAM_TOKEN=...
# Если не задан - используем TELEGRAM_TOKEN для обратной совместимости.
TELEGRAM_TOKEN = os.getenv("WEATHER_TELEGRAM_TOKEN") or os.getenv(
    "TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN"
)
KOMOOT_LINK_PATTERN = re.compile(r"(https?://)?(www\.)?komoot\.[^/]+/tour/(\d+)")

CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
MAX_GPX_SIZE_BYTES = 5 * 1024 * 1024  # 5MB

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🌤️ <b>Hi! I am a weather bot</b>\n\n"
        "Send a public Komoot route link\n"
        "or upload a GPX route file.\n"
        "I will generate a weather dashboard.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[RESTART_BUTTON]], resize_keyboard=True),
    )
    return ASK_KOMOOT_LINK


async def ask_komoot_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == RESTART_BUTTON:
        return await start(update, context)

    if update.message.document:
        document = update.message.document
        filename = (document.file_name or "").lower()
        mime_type = (document.mime_type or "").lower()

        if document.file_size and document.file_size > MAX_GPX_SIZE_BYTES:
            await update.message.reply_text("❌ File is too large. Maximum size is 5MB.")
            return ASK_KOMOOT_LINK

        allowed_mimes = {"application/gpx+xml", "application/octet-stream", "text/xml"}
        if not filename.endswith(".gpx") and mime_type not in allowed_mimes:
            await update.message.reply_text("❌ Please upload a GPX file (.gpx).")
            return ASK_KOMOOT_LINK

        safe_name = f"uploaded_{int(time.time())}_{uuid.uuid4().hex[:8]}.gpx"
        gpx_path = os.path.join(CACHE_DIR, safe_name)
        try:
            tg_file = await context.bot.get_file(document.file_id)
            await tg_file.download_to_drive(custom_path=gpx_path)
        except Exception as e:
            logger.error("Failed to download GPX file: %s", e, exc_info=True)
            await update.message.reply_text("❌ Failed to download GPX file.")
            return ASK_KOMOOT_LINK

        # GPX content validation
        try:
            with open(gpx_path, "r", encoding="utf-8") as f:
                gpx = gpxpy.parse(f)
            has_points = any(
                segment.points
                for track in gpx.tracks
                for segment in track.segments
            )
            if not has_points:
                os.remove(gpx_path)
                await update.message.reply_text("❌ GPX file contains no route points.")
                return ASK_KOMOOT_LINK
        except Exception:
            if os.path.exists(gpx_path):
                os.remove(gpx_path)
            await update.message.reply_text("❌ File is corrupted or not a valid GPX.")
            return ASK_KOMOOT_LINK

        context.user_data["gpx_path"] = gpx_path
        context.user_data["tour_id"] = f"upload_{uuid.uuid4().hex[:8]}"
        context.user_data["komoot_link"] = None

        dates = [
            ["📅 Today", "📅 Tomorrow"],
            ["📅 Day After Tomorrow", "📅 In 3 Days"],
            ["❌ Cancel"],
            [RESTART_BUTTON],
        ]
        await update.message.reply_text(
            "✅ GPX file accepted.\nChoose ride date:",
            reply_markup=ReplyKeyboardMarkup(dates, one_time_keyboard=True, resize_keyboard=True),
        )
        return ASK_DATE

    if text == "❌ Cancel":
        await update.message.reply_text(
            "❌ Cancelled.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    match = KOMOOT_LINK_PATTERN.search(text)
    if not match:
        await update.message.reply_text(
            "❌ Invalid link format.\n"
            "Example: <code>https://www.komoot.com/tour/123456789</code>",
            parse_mode="HTML",
        )
        return ASK_KOMOOT_LINK

    context.user_data["komoot_link"] = text
    context.user_data["tour_id"] = match.group(3)

    dates = [
        ["📅 Today", "📅 Tomorrow"],
        ["📅 Day After Tomorrow", "📅 In 3 Days"],
        ["❌ Cancel"],
        [RESTART_BUTTON],
    ]
    await update.message.reply_text(
        "Choose ride date:",
        reply_markup=ReplyKeyboardMarkup(dates, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_DATE


async def ask_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == RESTART_BUTTON:
        return await start(update, context)
    if text == "❌ Cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    tz = get_timezone()
    now = datetime.now(tz)
    selected_date = None

    if text == "📅 Today":
        selected_date = now.date()
    elif text == "📅 Tomorrow":
        selected_date = (now + timedelta(days=1)).date()
    elif text == "📅 Day After Tomorrow":
        selected_date = (now + timedelta(days=2)).date()
    elif text == "📅 In 3 Days":
        selected_date = (now + timedelta(days=3)).date()
    else:
        m = re.match(r"^(\d{1,2})\.(\d{1,2})$", text)
        if not m:
            await update.message.reply_text("Enter date in DD.MM format")
            return ASK_DATE
        day, month = map(int, m.groups())
        try:
            selected_date = datetime(now.year, month, day).date()
        except ValueError:
            await update.message.reply_text("Invalid date, please try again.")
            return ASK_DATE

    if selected_date < now.date():
        await update.message.reply_text("Date is in the past. Choose a future date.")
        return ASK_DATE

    context.user_data["selected_date"] = selected_date
    times = [
        ["🌅 06:00", "🌅 07:00"],
        ["☀️ 08:00", "☀️ 09:00"],
        ["☀️ 10:00", "🌞 11:00"],
        ["🌞 12:00", "🌆 13:00"],
        ["🌆 14:00", "🌙 15:00"],
        ["❌ Cancel"],
        [RESTART_BUTTON],
    ]
    await update.message.reply_text(
        "Choose start time:",
        reply_markup=ReplyKeyboardMarkup(times, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_TIME


async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == RESTART_BUTTON:
        return await start(update, context)
    if text == "❌ Cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    map_btn = {
        "🌅 06:00": "06:00",
        "🌅 07:00": "07:00",
        "☀️ 08:00": "08:00",
        "☀️ 09:00": "09:00",
        "☀️ 10:00": "10:00",
        "🌞 11:00": "11:00",
        "🌞 12:00": "12:00",
        "🌆 13:00": "13:00",
        "🌆 14:00": "14:00",
        "🌙 15:00": "15:00",
    }
    raw = map_btn.get(text, text)
    try:
        time_obj = datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("Enter time in HH:MM format")
        return ASK_TIME

    tz = get_timezone()
    selected_date = context.user_data["selected_date"]
    context.user_data["selected_datetime"] = tz.localize(datetime.combine(selected_date, time_obj))

    speeds = [
        ["🚴 15 km/h", "🚴 20 km/h"],
        ["🚴 25 km/h", "🚴 30 km/h"],
        ["🚴 35 km/h", "🚴 40 km/h"],
        ["❌ Cancel"],
        [RESTART_BUTTON],
    ]
    await update.message.reply_text(
        "Choose riding speed:",
        reply_markup=ReplyKeyboardMarkup(speeds, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_SPEED


async def ask_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == RESTART_BUTTON:
        return await start(update, context)
    if text == "❌ Cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    speed_btn = {
        "🚴 15 km/h": 15,
        "🚴 20 km/h": 20,
        "🚴 25 km/h": 25,
        "🚴 30 km/h": 30,
        "🚴 35 km/h": 35,
        "🚴 40 km/h": 40,
    }
    speed = speed_btn.get(text)
    if speed is None:
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:km/h)?$", text, re.IGNORECASE)
        if not m:
            await update.message.reply_text("Enter speed as a number, e.g. 25")
            return ASK_SPEED
        speed = float(m.group(1))

    context.user_data["speed"] = speed
    return await process_gpx(update, context)


async def process_gpx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tour_id = context.user_data["tour_id"]
    selected_datetime = context.user_data["selected_datetime"]
    speed = context.user_data["speed"]
    gpx_path = context.user_data.get("gpx_path")
    if not gpx_path:
        process = await asyncio.create_subprocess_exec(
            "komootgpx",
            "-d",
            tour_id,
            "-o",
            CACHE_DIR,
            "-e",
            "-n",
        )

        try:
            await asyncio.wait_for(process.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            process.kill()
            await update.message.reply_text("❌ GPX download timeout.")
            return ConversationHandler.END

        if process.returncode != 0:
            await update.message.reply_text("❌ Failed to download Komoot route.")
            return ConversationHandler.END

        gpx_files = glob.glob(f"{CACHE_DIR}/*-{tour_id}.gpx")
        if not gpx_files:
            await update.message.reply_text("❌ GPX was not found after download.")
            return ConversationHandler.END
        gpx_path = gpx_files[0]
        context.user_data["gpx_path"] = gpx_path
    elif not os.path.exists(gpx_path):
        await update.message.reply_text("❌ Uploaded GPX file was not found.")
        return ConversationHandler.END

    route_timezone_name = detect_timezone_from_gpx(gpx_path)
    route_tz = get_timezone(route_timezone_name)
    timezone_label = getattr(route_tz, "zone", str(route_tz))

    # Ensure selected_datetime is represented in route timezone.
    if selected_datetime.tzinfo is None:
        selected_datetime = route_tz.localize(selected_datetime)
    else:
        selected_datetime = selected_datetime.astimezone(route_tz)
    context.user_data["selected_datetime"] = selected_datetime

    await update.message.reply_text(
        f"🌤️ Generating weather dashboard (Time Zone {timezone_label})...",
        reply_markup=ReplyKeyboardRemove(),
    )

    output_path = os.path.join(CACHE_DIR, f"dashboard_{tour_id}_{int(selected_datetime.timestamp())}.png")

    cmd = [
        "python3",
        "weather_dashboard.py",
        gpx_path,
        "-o",
        output_path,
        "-s",
        str(speed),
        "-d",
        selected_datetime.strftime("%d.%m.%Y"),
        "-t",
        selected_datetime.strftime("%H:%M"),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        logger.error("Dashboard generation failed: %s", exc.stderr)
        await update.message.reply_text("❌ Failed to generate dashboard.")
        return ConversationHandler.END

    context.user_data["dashboard_path"] = output_path
    return await show_dashboard(update, context)


async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dashboard_path = context.user_data.get("dashboard_path")
    selected_datetime = context.user_data.get("selected_datetime")
    speed = context.user_data.get("speed")

    if not dashboard_path or not os.path.exists(dashboard_path):
        await update.message.reply_text("❌ Dashboard not found.")
        return ConversationHandler.END

    with open(dashboard_path, "rb") as image:
        await update.message.reply_photo(photo=image)

    await update.message.reply_text(
        "🌤️ <b>Weather dashboard is ready</b>\n\n"
        f"📅 Date: {selected_datetime.strftime('%d.%m.%Y')}\n"
        f"⏰ Time: {selected_datetime.strftime('%H:%M')}\n"
        f"🚴 Speed: {speed} km/h",
        parse_mode="HTML",
    )
    context.user_data.clear()
    await update.message.reply_text(
        "🌤️ <b>Ready for another route?</b>\n\n"
        "Send a public Komoot link or upload a GPX file:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[RESTART_BUTTON]], resize_keyboard=True),
    )
    return ASK_KOMOOT_LINK


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/start - restart and begin a new dashboard flow\n"
        "/help - show help\n"
        "/cancel - cancel current flow\n\n"
        "You can send:\n"
        "• public Komoot route link\n"
        "• GPX file (up to 5MB)"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def main():
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("❌ Error: set WEATHER_TELEGRAM_TOKEN (or TELEGRAM_TOKEN) in env")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_KOMOOT_LINK: [MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, ask_komoot_link)],
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_date)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_time)],
            ASK_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_speed)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command), CommandHandler("start", start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    print("Weather bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
