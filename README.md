# BlackBox

Регистрация данных с дискретных/аналоговых входов, запись в CSV/JSON, аварийные срезы. Отдельно — **получение данных по Modbus** в пакете `modbus_acquire` (удобно подключать к Flask без всей логики записи).

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

3) Настройка переменных окружения (пример):

```powershell
$env:BLACKBOX_DB_PATH="/home/agk/app/BlackBox/instance/blackbox.db"
$env:MODBUS_PORT="/dev/ttyAMA0"
$env:MODBUS_SLAVE="1"
$env:MODBUS_BAUDRATE="9600"
$env:MODBUS_TIMEOUT="0.35"
$env:MODBUS_INTERVAL="0.12"
$env:MODBUS_ADDRESS_OFFSET="1"
$env:RAM_BATCH_SIZE="60"
$env:APP_USERNAME="admin"
$env:APP_PASSWORD="admin"
$env:SECRET_KEY="change-me"
$env:HOST="0.0.0.0"
$env:PORT="5000"
$env:PUBLIC_IP="10.109.114.106"
```

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
uv run uvicorn src.web_app:app --host 0.0.0.0 --port 5000 --log-level debug --access-log
```

10) Открыть в браузере:

```text
http://10.109.114.106:5000/
```

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
