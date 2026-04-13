# Announce Bot + Weather Bot

Проект запускает два Telegram-бота:

- `announce_bot.py` — бот для создания анонсов поездок;
- `weather_bot.py` — бот для погодного дашборда по маршруту Komoot.

Для скачивания GPX используется [komootgpx](https://github.com/timschneeb/KomootGPX).

## Зачем нужны `weather_dashboard.py` и два бота

- `weather_dashboard.py` — это общий модуль, который строит погодную картинку: читает GPX, запрашивает прогноз и генерирует PNG-дашборд.
- `announce_bot.py` — бот для полноценного сценария анонса поездки (дата, маршрут, старт/финиш, темп, комментарий).
- `weather_bot.py` — отдельный упрощенный бот только для сценария погоды (ссылка/GPX -> дашборд).

Такое разделение убирает дублирование и упрощает поддержку: логику генерации дашборда можно менять в одном месте, а два бота остаются независимыми по UX и релизам.


## Требования

- Python 3.11+ (рекомендовано);
- Docker и Docker Compose (для контейнерного запуска);
- токены Telegram-ботов;
- доступный `komootgpx` внутри окружения/контейнера.

## Быстрый старт (локально)

1. Клонируйте репозиторий и перейдите в директорию проекта:

```bash
git clone <repository-url>
cd announce_bot
```

2. Создайте виртуальное окружение и установите зависимости:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Создайте файлы окружения:

```bash
cat > .env_announce <<'EOF'
TELEGRAM_TOKEN=your_announce_bot_token_here
TIMEZONE=Europe/Belgrade
EOF

cat > .env_weather <<'EOF'
TELEGRAM_TOKEN=your_weather_bot_token_here
TIMEZONE=Europe/Belgrade
EOF
```

4. Запуск:

```bash
python3 announce_bot.py
```

или

```bash
python3 weather_bot.py
```

## Запуск через Docker Compose

В проекте используется `compose.yml` с двумя сервисами: `announce-bot` и `weather-bot`.

```bash
docker compose up -d --build
```

Остановка:

```bash
docker compose down
```

## Переменные окружения

`/path/to/project/.env_announce`

- `TELEGRAM_TOKEN` — токен бота анонсов;
- `TIMEZONE` — таймзона (например `Europe/Belgrade`).

`/path/to/project/.env_weather`

- `TELEGRAM_TOKEN` — токен погодного бота;
- `TIMEZONE` — таймзона.

## Конфигурация маршрутов и точек старта

Основные конфиги:

- `start_points.json` — точки старта;
- `routes.json` и `ready_routes.json` — маршруты;
- `finish_points.json` — точки финиша.

Базовый формат полей:

- `name` — название для кнопки;
- `link` — ссылка на маршрут/локацию.

После изменения JSON-файлов перезапустите контейнеры или процессы.

## Команды бота

- `/start` — старт диалога анонса;
- `/weather <komoot_link>` — получить погодный дашборд по ссылке;
- `/status` — статус бота и кэша;
- `/clear_cache` — очистка GPX-кэша;
- `/help` — краткая справка;
- `/restart` — сброс состояния диалога.


## Лицензия

THE BEER-WARE LICENSE
