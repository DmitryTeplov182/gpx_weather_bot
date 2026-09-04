# Announce Bot + Weather Bot

Проект запускает два Telegram-бота:

- `announce_bot.py` — бот для создания анонсов поездок;
- `weather_bot.py` — бот для погодного дашборда по маршруту Komoot.

Для скачивания GPX используется [komootgpx](https://github.com/timschneeb/KomootGPX).

## Модули

- `ride_dashboard.py` — единый рендерер картинки заезда. По GPX и JSON-конфигу рисует один PNG 1600×2200: крупная шапка (дата, время, точка старта, темп или диапазон скорости), описание, плитки (дистанция, набор, температура min–max, вероятность дождя min–max), карта на подложке OpenStreetMap с треком, номерами подъёмов и маленькими стрелками ветра, профиль высоты с осью в метрах, список подъёмов (max grade по 100 м), график попутного/встречного ветра по времени и полупрозрачная вотермарка внизу. Прогноз берётся из Open-Meteo; если он недоступен, картинка всё равно строится без погодных блоков.
- `ride_poster.py` — библиотека: разбор GPX, поиск подъёмов, профиль высоты, панели и подгонка текста. Его CLI по-прежнему рисует старый постер.
- `weather_dashboard.py` — выборка точек и запрос прогноза, вспомогательные функции карты (Web Mercator, тайлы OSM). Его CLI (`weather_dashboard.py route.gpx -d ДД.ММ.ГГГГ -t ЧЧ:ММ -s 27`) используется `weather_bot.py` и теперь тоже рендерит единый дашборд.
- `announce_bot.py` — бот полного сценария анонса (дата, маршрут, старт/финиш, темп, комментарий, картинка). Длину и набор маршрута берёт из API Komoot (сглаженные значения), при недоступности — считает по GPX. Дашборд строится через `ride_dashboard.py` как subprocess; при правке даты, времени, названия, старта, темпа или комментария он пересобирается автоматически перед предпросмотром.
- `weather_bot.py` — отдельный упрощённый бот только для сценария погоды (ссылка/GPX -> дашборд).

Ручной рендер для проверки:

```bash
python ride_dashboard.py --gpx routes/valevo.gpx --config routes/valevo_poster.json --out dashboard.png
```

Ключи конфига: `route_name`, `start_iso` (ISO 8601, при отсутствии зоны — таймзона маршрута), `start`, `pace` (1.0–3.0 или `null`), `speed_range` (`[25, 28]` или `null`), `speed_kmh`, `notes`, `distance_km`, `elevation_m`, `weather` (`false` — без прогноза), `watermark`. `--no-weather` отключает запрос прогноза.

`tools/grade_experiment.py` сравнивает варианты сглаживания для максимального градиента на GPX или JSON координат Komoot.


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
- `TIMEZONE` — таймзона (например `Europe/Belgrade`);
- `DASHBOARD_WATERMARK` — текст вотермарки на дашборде (по умолчанию `whatever`);
- `OSM_TILE_USER_AGENT` — User-Agent для тайлов OSM (см. `.env_weather.example`).

`/path/to/project/.env_weather`

- `TELEGRAM_TOKEN` — токен погодного бота;
- `TIMEZONE` — таймзона;
- `OSM_TILE_USER_AGENT` — User-Agent для тайлов OSM.

Тайлы OSM кэшируются в `cache/tiles/`, повторный рендер того же маршрута не ходит за ними в сеть.

## Конфигурация маршрутов и точек старта

Основные конфиги:

- `start_points.json` — точки старта;
- `routes.json` и `ready_routes.json` — маршруты;
- `finish_points.json` — точки финиша.

Базовый формат полей:

- `name` — название для кнопки (оно же попадает в анонс и на дашборд, поэтому названия на сербском или английском);
- `link` — ссылка на маршрут/локацию.

Кнопка «Своя точка» добавляется автоматически и помечена флагом `custom`, в JSON её описывать не нужно. В `ready_routes.json` поле `start_point` должно совпадать с `name` из `start_points.json`.

После изменения JSON-файлов перезапустите контейнеры или процессы.

## Сценарий анонса

Шаг темпа необязателен: можно выбрать луны, написать среднюю скорость (`27` или `25-28`, км/ч) или нажать «Без темпа». Скорость используется для расчёта времени в пути и прогноза; при лунах берётся таблица `PACE_TO_SPEED`, иначе 27 км/ч.

На шаге картинки: «Дашборд заезда» (генерируется 10–30 секунд), своя картинка или пропуск. Своя картинка заменяет дашборд.

## Команды бота

- `/start` — старт диалога анонса;
- `/quick` — анонс по готовому маршруту из `ready_routes.json`;
- `/weather <komoot_link>` — дашборд маршрута с прогнозом на текущее время;
- `/status` — статус бота и кэша;
- `/clear_cache` — очистка GPX-кэша;
- `/help` — краткая справка;
- `/restart` — сброс состояния диалога.


## Лицензия

THE BEER-WARE LICENSE
