# BlackBox: запуск в фоне и автозапуск (Linux)

Целевая директория проекта:

```bash
agk@BlackBox:~/app/BlackBox $
```

## 1) Подготовка

Выполнить из корня проекта:

```bash
cd ~/app/BlackBox
chmod +x scripts/linux/start_blackbox.sh scripts/linux/install_systemd_service.sh
```

## 2) Установка systemd-сервиса

```bash
sudo sh scripts/linux/install_systemd_service.sh
```

Скрипт:

- создаст пользователя `blackbox` (если нет);
- скопирует проект в `/opt/blackbox`;
- установит unit-файл `blackbox.service`;
- включит автозапуск и стартует сервис.

При **первом** запуске `start_blackbox.sh` сам создаёт `/opt/blackbox/.env`, если файла ещё нет (мастер встроен в скрипт). Под **systemd** (без TTY) подставляются значения по умолчанию. Чтобы пройти интерактивный мастер, удалите `.env` и запустите `./scripts/linux/start_blackbox.sh` из SSH-сессии с терминалом от имени пользователя сервиса, например:

```bash
sudo -u blackbox sh /opt/blackbox/scripts/linux/start_blackbox.sh
```

## 3) Проверка

```bash
systemctl status blackbox.service
journalctl -u blackbox.service -f
```

## 4) Настройки окружения

Основной файл конфигурации приложения в каталоге установки:

```text
/opt/blackbox/.env
```

Он создаётся автоматически при первом старте (см. выше). Переменные из `.env` подхватываются скриптом запуска и переопределяют значения из unit-файла для совпадающих имён.

Дополнительно unit подключает (если файл есть):

```text
/etc/default/blackbox
```

Пример параметров (частично дублируют `.env`; приоритет у переменных, заданных в `.env` после его загрузки в `start_blackbox.sh`):

```bash
HOST=0.0.0.0
PORT=5000
APP_TIMEZONE=Europe/Moscow
MODBUS_PORT=/dev/ttyAMA0
MODBUS_SLAVE=1
MODBUS_BAUDRATE=9600
MODBUS_TIMEOUT=0.35
MODBUS_INTERVAL=0.12
MODBUS_ADDRESS_OFFSET=1
RAM_BATCH_SIZE=60
SECRET_KEY=change-me
```

После изменения:

```bash
sudo systemctl restart blackbox.service
```

## 5) Частые команды

```bash
sudo systemctl restart blackbox.service
sudo systemctl stop blackbox.service
sudo systemctl start blackbox.service
sudo systemctl disable blackbox.service
sudo systemctl enable blackbox.service
journalctl -u blackbox.service -n 200 --no-pager
```

## Важно

Текущий установщик копирует проект в `/opt/blackbox`.
Если хотите запускать строго из `~/app/BlackBox` без копирования, можно изменить unit-файл:

- `WorkingDirectory=/home/agk/app/BlackBox`
- `ExecStart=/home/agk/app/BlackBox/scripts/linux/start_blackbox.sh`
- `User=agk`
- `Group=agk`

и затем выполнить:

```bash
sudo systemctl daemon-reload
sudo systemctl restart blackbox.service
```
