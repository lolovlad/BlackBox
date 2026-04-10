# BlackBox: автозапуск при загрузке (Linux, systemd)

Этот файл про **одно**: как сделать, чтобы приложение **само поднималось после перезагрузки** устройства.

Полная пошаговая установка «с нуля» (создание `.env`, миграции, первый ручной запуск) — в **`DEPLOY_ON_DEVICE_RU.md`**. Там же объяснено, чем ручной запуск отличается от службы systemd.

---

## Что делает установщик службы

Скрипт `scripts/linux/install_systemd_service.sh` (запускать от root):

- создаёт системного пользователя `blackbox` (если его ещё нет);
- копирует текущий каталог проекта в **`/opt/blackbox`**;
- выставляет права на запуск `create_env.sh` и `run_blackbox.sh`;
- если в `/opt/blackbox` **нет** `.env`, запускает **`create_env.sh`** от имени `blackbox` (без TTY — только значения по умолчанию);
- ставит unit-файл **`blackbox.service`** и включает автозапуск;
- опционально создаёт **`/etc/default/blackbox`** с подсказками по переменным.

**Запуск приложения** внутри службы выполняет только **`run_blackbox.sh`** (зависимости, миграции, `uvicorn`). Это не «единый скрипт с фоном» — фон обеспечивает **systemd**, скрипт лишь стартует процесс, как при ручном запуске.

---

## Команды

Из корня **исходного** репозитория (до копирования в `/opt`):

```bash
chmod +x scripts/linux/install_systemd_service.sh scripts/linux/create_env.sh scripts/linux/run_blackbox.sh
sudo sh scripts/linux/install_systemd_service.sh
```

Проверка:

```bash
systemctl status blackbox.service
journalctl -u blackbox.service -f
```

Дополнительные переменные (порт, Modbus и т.д.) можно задать в **`/etc/default/blackbox`** и/или в **`/opt/blackbox/.env`**. После правок:

```bash
sudo systemctl restart blackbox.service
```

---

## Запуск из своей папки без копирования в `/opt`

Если нужно, чтобы служба работала из `~/app/BlackBox`, отредактируйте unit-файл **`deploy/systemd/blackbox.service`** (или уже установленный `/etc/systemd/system/blackbox.service`):

- `WorkingDirectory=/home/ВАШ_ПОЛЬЗОВАТЕЛЬ/app/BlackBox`
- `ExecStart=/home/ВАШ_ПОЛЬЗОВАТЕЛЬ/app/BlackBox/scripts/linux/run_blackbox.sh`
- `User=` и `Group=` — ваш пользователь

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl restart blackbox.service
```

---

## Ошибка «uv is not installed» в journalctl

У службы **другой PATH**, чем у вашего SSH-пользователя: `uv` мог быть установлен только в `~/.local/bin` того пользователя, под которым вы заходили, а не для `blackbox`.

**Что сделать (любой один вариант):**

1. Обновите unit-файл из репозитория (в нём задан расширенный `Environment=PATH=...` с `/opt/blackbox/.local/bin` и `/usr/local/bin`), затем:
   ```bash
   sudo cp /путь/к/репозиторию/deploy/systemd/blackbox.service /etc/systemd/system/blackbox.service
   sudo systemctl daemon-reload
   sudo systemctl restart blackbox.service
   ```
2. Установите `uv` для пользователя службы (часто достаточно так):
   ```bash
   sudo -u blackbox mkdir -p /opt/blackbox/.local/bin
   curl -LsSf https://astral.sh/uv/install.sh | sudo -u blackbox env HOME=/opt/blackbox sh
   ```
3. Либо положите исполняемый `uv` в `/usr/local/bin` на всех пользователей.
4. Либо в **`/etc/default/blackbox`** добавьте строку с полным путём, например:  
   `UV_BINARY=/полный/путь/к/uv`

Повторный запуск установщика `install_systemd_service.sh` с актуальной версией репозитория при отсутствии `uv` попытается поставить его под `blackbox` сам (нужен `curl` и доступ в интернет).

---

## Ошибка «Permission denied» на `/opt/blackbox/.venv/bin/python3`

Каталог **`.venv` создан не от пользователя `blackbox`** (типично: от root командой `sudo uv sync` в `/opt/blackbox`).

**Исправление от root:**

```bash
sudo chown -R blackbox:blackbox /opt/blackbox/.venv
sudo systemctl restart blackbox.service
```

Если не помогло — удалите окружение и дайте службе создать его заново:

```bash
sudo systemctl stop blackbox.service
sudo rm -rf /opt/blackbox/.venv
sudo systemctl start blackbox.service
```

Актуальный `install_systemd_service.sh` после копирования делает `chown -R blackbox:blackbox` и один раз выполняет `uv sync` от имени `blackbox`, чтобы `.venv` сразу был с правильным владельцем.

---

## Интерактивный мастер `.env` на установленной копии

Если при установке службы создался `.env` с дефолтами и вы хотите пройти вопросы мастера:

1. Сохраните копию `.env` при необходимости.
2. Удалите `/opt/blackbox/.env`.
3. Зайдите по SSH и выполните от имени пользователя службы:

```bash
sudo -u blackbox sh /opt/blackbox/scripts/linux/create_env.sh
```

4. Перезапустите службу: `sudo systemctl restart blackbox.service`.
