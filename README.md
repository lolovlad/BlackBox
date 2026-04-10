# BlackBox

Регистрация данных с дискретных/аналоговых входов, запись в CSV/JSON, аварийные срезы. Отдельно — **получение данных по Modbus** в пакете `modbus_acquire` (удобно подключать к Flask без всей логики записи).

**Развёртывание на Linux-устройстве по шагам (для оператора):** см. **[`DEPLOY_ON_DEVICE_RU.md`](DEPLOY_ON_DEVICE_RU.md)**. Автозапуск при загрузке отдельно: **[`LINUX_AUTOSTART.md`](LINUX_AUTOSTART.md)**.

## Flask веб-интерфейс (Blueprints + FlaskForm + htmx)

Веб-приложение находится в `src/web_app.py` (entrypoint) и `src/webui/` (основная структура).

- При открытии `/` переход на форму входа `/login` (через `FlaskForm`).
- После входа доступна панель `/dashboard`.
- Страница `/data/` показывает `Analogs`, `Discretes`, `Alarms`.
- Таблицы обновляются через htmx (`/data/tables`), экспорт создается в `src/static/csv/`.
- Прямая ссылка на экспорт CSV: `/data/export.csv`.

### Команды развертывания и запуска (PowerShell, Windows)

1) Установка зависимостей:

```powershell
uv sync
```

2) Пересоздание lock-файла (если менялись зависимости):

```powershell
uv lock
```

3) Создайте файл `.env` в корне проекта: вручную (пример ниже) **или** на Linux — `sh scripts/linux/create_env.sh`.

> Важно: приложение **не запустится**, если файла `.env` нет.

Пример содержимого `.env`:

```powershell
BLACKBOX_DB_PATH=C:/BlackBoxData/blackbox.db
MODBUS_PORT=COM3
MODBUS_SLAVE=1
MODBUS_BAUDRATE=9600
MODBUS_TIMEOUT=0.35
MODBUS_INTERVAL=0.12
MODBUS_ADDRESS_OFFSET=1
RAM_BATCH_SIZE=60
APP_TIMEZONE=Europe/Moscow
DB_CLEANUP_INTERVAL_MINUTES=60
DB_RETENTION_DAYS=30
VIDEO_STORAGE_DIR=D:/Archive/blackbox-videos
VIDEO_GC_INTERVAL_DAYS=10
SECRET_KEY=change-me
HOST=0.0.0.0
PORT=5000
PUBLIC_IP=10.109.114.106
PARSER_SETTINGS_PATH=settings/settings.json
SESSION_COOKIE_SECURE=0
FLASK_INSTANCE_PATH=instance
DISABLE_MODBUS_COLLECTOR=0
SEED_ADMIN_USERNAME=admin
SEED_ADMIN_PASSWORD=admin
SEED_USER_USERNAME=user
SEED_USER_PASSWORD=user
```

`BLACKBOX_DB_PATH` может указывать на любой диск/путь (например приложение на `C:`, БД на `D:`).

4) Настройка Flask CLI для миграций:

```powershell
$env:FLASK_APP="src.web_app:app"
```

5) Инициализация миграций (только один раз на проект):

```powershell
uv run flask db init
```

6) Создание миграции с автогенерацией после изменения моделей:

```powershell
uv run flask db migrate -m "describe changes"
```

7) Применение миграций:

```powershell
uv run flask db upgrade
```

8) Сид начальных ролей и пользователей:

```powershell
$env:DISABLE_MODBUS_COLLECTOR="1"
uv run python -m src.seed
```

По умолчанию будут созданы:
- типы пользователей: `admin`, `user`
- пользователи: `admin/admin` и `user/user`

Можно переопределить:
- `SEED_ADMIN_USERNAME`, `SEED_ADMIN_PASSWORD`
- `SEED_USER_USERNAME`, `SEED_USER_PASSWORD`

9) Запуск сервера (Uvicorn, полный лог в консоль):

```powershell
uv run uvicorn src.web_app:app --host 0.0.0.0 --port 5000 --interface wsgi --log-level debug --access-log
```

10) Открыть в браузере:

```text
http://10.109.114.106:5000/
```

## API: добавление видео

Добавлен endpoint:

- `POST /api/video/add`

Тело запроса (`application/json`):

```json
{
  "path": "D:/Archive/blackbox-videos/camera_1_(2026-04-09_14-22-11).mp4"
}
```

Правила:

- из имени файла извлекаются дата/время (поддерживаются форматы вроде `YYYY-MM-DD_HH-MM-SS`, в том числе в скобках);
- запись сохраняется в таблицу `videos`;
- видео сохраняется только если его `captured_at` попадает в активный интервал в таблице `alarms` (переходы `state=active/inactive`).

Ответ при успехе:

```json
{
  "ok": true,
  "video_id": 1,
  "alarm_id": 12,
  "alarm_name": "Low oil pressure",
  "captured_at": "2026-04-09T14:22:11",
  "file_name": "camera_1_(2026-04-09_14-22-11).mp4"
}
```

## Фоновые задачи

1. Очистка БД от старых записей:
   - запускается каждые `DB_CLEANUP_INTERVAL_MINUTES`;
   - удаляет записи старше `DB_RETENTION_DAYS` из `samples`, `alarms`, `event_logs`.

2. Очистка видеофайлов:
   - запускается каждые `VIDEO_GC_INTERVAL_DAYS` (по умолчанию 10 дней);
   - в `VIDEO_STORAGE_DIR` удаляет файлы, которых нет в таблице `videos`.

### Linux: скрипты и автозапуск

- **`scripts/linux/create_env.sh`** — только создание `.env` (мастер или дефолты без TTY).
- **`scripts/linux/run_blackbox.sh`** — установка зависимостей, миграции, запуск **uvicorn в этом терминале** (остановка Ctrl+C). Не смешивается с «фоном»: для фона используется systemd.
- **`deploy/systemd/blackbox.service`** — unit-файл; `ExecStart` вызывает `run_blackbox.sh`.
- **`scripts/linux/install_systemd_service.sh`** — копия в `/opt/blackbox`, venv, unit; служба по умолчанию от **root** (см. комментарии в `blackbox.service`).

Первый ручной запуск на устройстве: пошагово в **[`DEPLOY_ON_DEVICE_RU.md`](DEPLOY_ON_DEVICE_RU.md)**.

Автозапуск при загрузке: **[`LINUX_AUTOSTART.md`](LINUX_AUTOSTART.md)**.

Настройки службы (порт, Modbus и т.д.) — в **`/etc/default/blackbox`** и в **`/opt/blackbox/.env`** (или в каталоге установки).

### Полезные команды Flask-Migrate

Создать новую миграцию с автогенерацией:

```powershell
uv run flask db migrate -m "describe changes"
```

Откатить миграцию на один шаг:

```powershell
uv run flask db downgrade -1
```

## Зависимости

- **Источник правды для версий пакетов:** `pyproject.toml` (рекомендуется [uv](https://docs.astral.sh/uv/)).
- **Без uv:** можно по-прежнему `pip install -r requirements.txt` (минимальный список).

Внешних ORM сейчас нет: запись ошибок Modbus в БД отключена; при появлении общей модели её можно добавить отдельно.

### uv

В корне проекта: `pyproject.toml`, `.python-version` (Python 3.12), группа зависимостей `dev` (pytest, pytest-cov).

```bash
uv sync
uv run pytest
uv run python deif_modbus_console.py --port /dev/ttyAMA0 --once
```

После изменения зависимостей: `uv lock` (фиксирует версии в `uv.lock`; файл стоит коммитить для воспроизводимых сборок).

Чтобы не ставить dev-зависимости: `uv sync --no-dev`.

---

## Два слоя: «чтение» и «потребление»

| Слой | Пакет / скрипт | Задача | Зависимости |
|------|----------------|--------|-------------|
| **Получение (Modbus)** | `modbus_acquire` | Собрать `Instrument`, прочитать регистры/биты, для DEIF — `poll_raw` → `convert_raw` | только `minimalmodbus` |
| **Потребление** | `blackbox` | Буферы входов, `DataWriter`/`AlarmWriter`, маппинг Modbus → «виртуальные» входы, логи на диск | стандартная библиотека + при необходимости `modbus_acquire` |
| **Склейка DEIF + CSV** | `deif_modbus_csv_logger.py` | Цикл опроса + `HourlySplitCsvWriter` | `modbus_acquire` + `blackbox.hourly_param_csv` |

Для будущего **сайта на Flask** достаточно зависеть от `modbus_acquire`: в обработчике или фоновой задаче вызывайте `build_instrument`, `poll_raw`, `convert_raw` и отдавайте JSON на клиент. Писать CSV и крутить `DataLogger` для этого не обязательно.

---

## Пакет `modbus_acquire`

### `modbus_acquire.instrument`

- **`ModbusReader` / `read_all_data(config)`** — по списку полей из `config["fields"]` (или дефолтный демо-набор) читает регистры по одному, с **ретраями** и масштабированием (`scale`, `u16`/`s32`/`bitfield` и т.д.).
- **`build_instrument(config)`** — создаёт `minimalmodbus.Instrument` с теми же настройками порта, что и у `ModbusReader`. Нужен для **пакетного** чтения (`read_registers`, `read_bits`), которое в универсальном `ModbusReader` не описано.

Поток данных: **serial → Instrument → словарь Python** (числа, списки имён битов для `bitfield`).

### `modbus_acquire.deif`

Профиль **DEIF GEMPAC** (как в `legase/modbus_opt_v3.py`):

1. **`poll_raw(instrument, address_offset=1, on_error=...)`** — одно чтение 90 holding-регистров и 32 coil; при ошибке вызывается `on_error("modbus_holdings"|"modbus_coils", exc)`, частичный словарь всё равно возвращается.
2. **`convert_raw(raw)`** — масштабирование (частота ÷100, и т.д.), сбор `active_alarms` / статусов по таблицам битов.
3. **`analog_discrete_for_csv(processed)`** — два словаря под колонки CSV: аналоги и дискреты (булевы → дальше в CSV пишутся как 0/1).

Константы **`ANALOG_CSV_COLUMNS`**, **`DISCRETE_CSV_COLUMNS`** задают порядок колонок.

---

## Пакет `blackbox`

### Идея

- **`DiscreteInputs` / `AnalogInputs`** — логические входы регистратора (номер → значение).
- **`DataWriter`** — обычная запись по дням (путь с датой), формат CSV/JSON из `DataLoggerConfig`.
- **`AlarmWriter`** — при срабатывании условия выгружает окно до/после события в отдельный файл.
- **`DataLogger`** — потоки опроса, связь с писателями, опционально периодический вызов **`read_all_data`** из `modbus_acquire.instrument` и разбор словаря в аналоги/дискреты по картам в `DataLoggerConfig` (`modbus_to_analog_map`, `modbus_alarm_bits_to_discrete_map`).

То есть BlackBox отвечает за **где и как сохранить** и **как сопоставить каналы**, а не за низкоуровневый Modbus (это `modbus_acquire`).

### Почасовые CSV параметров

Класс **`blackbox.hourly_param_csv.HourlySplitCsvWriter`**:

- каталоги `{base}/analogs/` и `{base}/discretes/`;
- файл на каждый час: `{prefix}_{YYYY-MM-DD}_{HH}.csv`;
- строка: `line_no`, `date`, `time`, затем значения колонок.

Используется в `deif_modbus_csv_logger.py` вместе с `modbus_acquire`.

---

## Скрипт `deif_modbus_csv_logger.py`

1. `build_instrument` — открытие порта.
2. В цикле: `poll_raw` → `convert_raw` → `analog_discrete_for_csv` → `HourlySplitCsvWriter.write_sample`.
3. Ошибки транзакций Modbus только **`logger.warning`** (флаг `--verbose` включает подробный лог).

Запуск:

```bash
python deif_modbus_csv_logger.py --port /dev/ttyAMA0 --output ./logs --prefix unit1
```

---

## Обратная совместимость

- `from modbus_reader import read_all_data` — корневой shim на `modbus_acquire.instrument`.
- `from blackbox.modbus_reader import ...` — реэкспорт из `modbus_acquire.instrument`.
- `from blackbox.deif_gempac import ...` — реэкспорт из `modbus_acquire.deif`.

---

## Структура каталогов (логика)

```
modbus_acquire/          # только чтение Modbus (+ DEIF)
  instrument.py
  deif.py
blackbox/                # регистратор, писатели, входы
  data_logger.py
  data_writer.py
  config.py
  hourly_param_csv.py
  ...
deif_modbus_csv_logger.py
legase/                  # старые скрипты, ориентир по карте регистров
```
