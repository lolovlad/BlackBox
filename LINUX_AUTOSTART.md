# BlackBox: автозапуск при загрузке (Linux, systemd)

Этот файл про **одно**: как сделать, чтобы приложение **само поднималось после перезагрузки** устройства.

Полная пошаговая установка «с нуля» — в **`DEPLOY_ON_DEVICE_RU.md`**.

---

## Что делает установщик службы

Скрипт `scripts/linux/install_systemd_service.sh` (запускать от **root**):

- копирует текущий каталог проекта в **`/opt/blackbox`**, владелец **root**;
- удаляет старый **`/opt/blackbox/.venv`** (чтобы не оставались каталоги с «чужими» правами);
- при необходимости ставит **`uv`** для root (официальный установщик → обычно `/root/.local/bin`);
- выполняет **`uv sync`** в `/opt/blackbox` от root;
- если нет **`.env`**, создаёт его через **`create_env.sh`** (дефолты без TTY);
- ставит **`blackbox.service`**: процесс идёт от **`User=root`** (проще на закрытом контроллере: нет конфликтов прав на `.venv` и доступ к последовательному порту);
- опционально создаёт **`/etc/default/blackbox`**.

**Запуск** внутри службы — только **`run_blackbox.sh`** (sync, миграции, uvicorn). Фон обеспечивает **systemd**.

---

## Команды

Из корня репозитория на устройстве:

```bash
chmod +x scripts/linux/install_systemd_service.sh scripts/linux/create_env.sh scripts/linux/run_blackbox.sh
sudo sh scripts/linux/install_systemd_service.sh
```

Проверка:

```bash
systemctl status blackbox.service
journalctl -u blackbox.service -f
```

Переменные (порт, Modbus и т.д.) — в **`/etc/default/blackbox`** и **`/opt/blackbox/.env`**. После правок:

```bash
sudo systemctl restart blackbox.service
```

---

## Запуск из своей папки без `/opt/blackbox`

Отредактируйте **`deploy/systemd/blackbox.service`** (или `/etc/systemd/system/blackbox.service`):

- `WorkingDirectory=/home/ВАШ_ПОЛЬЗОВАТЕЛЬ/app/BlackBox`
- `ExecStart=.../run_blackbox.sh`
- при желании оставьте **`User=root`** или укажите своего пользователя и сделайте **`chown`** на каталог проекта.

```bash
sudo systemctl daemon-reload
sudo systemctl restart blackbox.service
```

---

## Ошибка «uv is not installed» в journalctl

В unit задан **`PATH`** с **`/root/.local/bin`** и **`/usr/local/bin`**.

**Варианты:**

1. Обновите **`blackbox.service`** из репозитория, затем `daemon-reload` и `restart`.
2. От root: `curl -LsSf https://astral.sh/uv/install.sh | sh` (даст `uv` в `/root/.local/bin`).
3. Положите бинарник в **`/usr/local/bin`**.
4. В **`/etc/default/blackbox`**: `UV_BINARY=/полный/путь/к/uv`

Повторный запуск **`install_systemd_service.sh`** при отсутствии `uv` попытается установить его сам (нужны **curl** и сеть).

---

## Ошибка «Permission denied» на `.venv/bin/python3`

Часто остаётся старый `.venv` от другого пользователя или после неудачной установки.

**От root:**

```bash
sudo systemctl stop blackbox.service
sudo rm -rf /opt/blackbox/.venv
cd /opt/blackbox && uv sync --frozen --no-dev
sudo systemctl start blackbox.service
```

Либо заново: **`sudo sh .../install_systemd_service.sh`** (скрипт сам удаляет `.venv` перед `sync`).

---

## Интерактивный мастер `.env` на `/opt/blackbox`

1. Сохраните копию `.env` при необходимости.
2. Удалите **`/opt/blackbox/.env`**.
3. Под SSH с TTY:

```bash
sudo sh /opt/blackbox/scripts/linux/create_env.sh
```

(из каталога `/opt/blackbox`: `cd /opt/blackbox && sh scripts/linux/create_env.sh`)

4. `sudo systemctl restart blackbox.service`

---

## Безопасность

Служба с **`User=root`** упрощает эксплуатацию на изолированном устройстве. На машине с открытым интернетом и несколькими сервисами разумнее отдельный пользователь и **`chown`** каталога проекта.
