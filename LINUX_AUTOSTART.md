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

## Интерактивный мастер `.env` на установленной копии

Если при установке службы создался `.env` с дефолтами и вы хотите пройти вопросы мастера:

1. Сохраните копию `.env` при необходимости.
2. Удалите `/opt/blackbox/.env`.
3. Зайдите по SSH и выполните от имени пользователя службы:

```bash
sudo -u blackbox sh /opt/blackbox/scripts/linux/create_env.sh
```

4. Перезапустите службу: `sudo systemctl restart blackbox.service`.
