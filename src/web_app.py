from __future__ import annotations

import atexit
import csv
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import Flask, Response, redirect, render_template_string, request, session, url_for
from flask_migrate import Migrate
from modbus_acquire.deif import ANALOG_CSV_COLUMNS, DISCRETE_CSV_COLUMNS, analog_discrete_for_csv, convert_raw, poll_raw
from modbus_acquire.instrument import build_instrument
from sqlalchemy.orm import sessionmaker

from src.database import Alarms, Analogs, Discretes, db

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    db_path: str = os.getenv("BLACKBOX_DB_PATH", "blackbox.db")
    modbus_port: str = os.getenv("MODBUS_PORT", "COM3")
    modbus_slave: int = int(os.getenv("MODBUS_SLAVE", "1"))
    modbus_baudrate: int = int(os.getenv("MODBUS_BAUDRATE", "9600"))
    modbus_timeout: float = float(os.getenv("MODBUS_TIMEOUT", "0.35"))
    modbus_interval: float = float(os.getenv("MODBUS_INTERVAL", "0.12"))
    address_offset: int = int(os.getenv("MODBUS_ADDRESS_OFFSET", "1"))
    ram_batch_size: int = int(os.getenv("RAM_BATCH_SIZE", "60"))
    app_username: str = os.getenv("APP_USERNAME", "admin")
    app_password: str = os.getenv("APP_PASSWORD", "admin")
    secret_key: str = os.getenv("SECRET_KEY", "change-me")


class ModbusCollector:
    def __init__(self, session_factory, config: RuntimeConfig) -> None:
        self._session_factory = session_factory
        self._config = config
        self._lock = threading.Lock()
        self._ram_buffer: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.flush_remaining()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        try:
            instrument = build_instrument(
                {
                    "port": self._config.modbus_port,
                    "slave_id": self._config.modbus_slave,
                    "baudrate": self._config.modbus_baudrate,
                    "timeout": self._config.modbus_timeout,
                    "clear_buffers_before_each_transaction": True,
                    "close_port_after_each_call": False,
                }
            )
            logger.info("Modbus instrument initialized: port=%s slave=%s", self._config.modbus_port, self._config.modbus_slave)
        except Exception:
            logger.exception("Failed to initialize Modbus instrument")
            instrument = None

        while not self._stop_event.is_set():
            try:
                if instrument is None:
                    logger.warning("Modbus instrument is unavailable, retrying in %.2fs", self._config.modbus_interval)
                    time.sleep(self._config.modbus_interval)
                    continue
                raw = poll_raw(instrument, address_offset=self._config.address_offset)
                sample = {"created_at": datetime.now(), "raw": raw}
                self._append(sample)
            except Exception:
                logger.exception("Unhandled error in Modbus polling loop")
            time.sleep(self._config.modbus_interval)

    def _append(self, sample: dict[str, Any]) -> None:
        batch: list[dict[str, Any]] = []
        with self._lock:
            self._ram_buffer.append(sample)
            if len(self._ram_buffer) >= self._config.ram_batch_size:
                batch = self._ram_buffer[:]
                self._ram_buffer.clear()
        if batch:
            self._flush(batch)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        session = self._session_factory()
        try:
            for sample in batch:
                created_at: datetime = sample["created_at"]
                raw: dict[str, Any] = sample["raw"]
                raw_bytes = json.dumps(raw, ensure_ascii=False).encode("utf-8")
                session.add(Analogs(created_at=created_at, date=raw_bytes))
                session.add(Discretes(created_at=created_at, date=raw_bytes))

                processed = convert_raw(raw)
                active_alarms = processed.get("active_alarms", [])
                if isinstance(active_alarms, list):
                    for alarm_name in active_alarms:
                        payload = {"alarm": alarm_name, "created_at": created_at.isoformat()}
                        session.add(
                            Alarms(
                                created_at=created_at,
                                date=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                name=str(alarm_name),
                                description="Alarm from converted Modbus data",
                            )
                        )
            session.commit()
            logger.info("Flushed %d samples from RAM buffer to database", len(batch))
        except Exception:
            session.rollback()
            logger.exception("Database flush failed for %d samples", len(batch))
        finally:
            session.close()

    def flush_remaining(self) -> None:
        with self._lock:
            batch = self._ram_buffer[:]
            self._ram_buffer.clear()
        if batch:
            self._flush(batch)


def _decode_raw(payload: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
        if isinstance(decoded, dict):
            return decoded
    except Exception:
        pass
    return {}


def create_app() -> Flask:
    config = RuntimeConfig()
    app = Flask(__name__)
    app.secret_key = config.secret_key
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{config.db_path.replace(os.sep, '/')}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.config["TRAP_HTTP_EXCEPTIONS"] = True
    app.config["TRAP_BAD_REQUEST_ERRORS"] = True

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app.logger.setLevel(logging.DEBUG)
    logger.info("Starting web app with DB path: %s", config.db_path)

    db.init_app(app)
    Migrate(app, db, compare_type=True, render_as_batch=True)
    with app.app_context():
        session_factory = sessionmaker(bind=db.engine, autoflush=False, autocommit=False, expire_on_commit=False)
    collector = ModbusCollector(session_factory, config)
    collector.start()
    atexit.register(collector.stop)

    def is_auth() -> bool:
        return bool(session.get("auth"))

    @app.route("/", methods=["GET", "POST"])
    def login():
        if is_auth():
            return redirect(url_for("dashboard"))
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == config.app_username and password == config.app_password:
                session["auth"] = True
                return redirect(url_for("dashboard"))
            error = "Неверный логин или пароль"
        return render_template_string(
            """
            <h2>Вход в систему</h2>
            {% if error %}<p style="color:red;">{{ error }}</p>{% endif %}
            <form method="post">
                <label>Логин: <input name="username" /></label><br />
                <label>Пароль: <input type="password" name="password" /></label><br />
                <button type="submit">Войти</button>
            </form>
            """
            ,
            error=error,
        )

    @app.get("/dashboard")
    def dashboard():
        if not is_auth():
            return redirect(url_for("login"))
        return render_template_string(
            """
            <h1>BlackBox</h1>
            <p>Сбор Modbus запущен. Доступен экспорт и просмотр данных.</p>
            <a href="{{ url_for('export_csv') }}">Экспорт CSV</a><br />
            <a href="{{ url_for('data_view') }}">Просмотр данных БД</a><br />
            <a href="{{ url_for('logout') }}">Выход</a>
            """
        )

    @app.get("/logout")
    def logout():
        session.pop("auth", None)
        return redirect(url_for("login"))

    @app.get("/data")
    def data_view():
        if not is_auth():
            return redirect(url_for("login"))

        db = session_factory()
        try:
            analog_rows = db.query(Analogs).order_by(Analogs.created_at.desc()).limit(100).all()
            discrete_rows = db.query(Discretes).order_by(Discretes.created_at.desc()).limit(100).all()
            alarm_rows = db.query(Alarms).order_by(Alarms.created_at.desc()).limit(100).all()
        finally:
            db.close()

        analog_table: list[dict[str, Any]] = []
        for item in analog_rows:
            raw = _decode_raw(item.date)
            processed = convert_raw(raw)
            analog, _ = analog_discrete_for_csv(processed)
            analog_table.append(
                {
                    "created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "values": [analog.get(col, "") for col in ANALOG_CSV_COLUMNS],
                }
            )

        discrete_table: list[dict[str, Any]] = []
        for item in discrete_rows:
            raw = _decode_raw(item.date)
            processed = convert_raw(raw)
            _, discrete = analog_discrete_for_csv(processed)
            discrete_table.append(
                {
                    "created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "values": [1 if bool(discrete.get(col, False)) else 0 for col in DISCRETE_CSV_COLUMNS],
                }
            )

        alarms_table: list[dict[str, Any]] = []
        for item in alarm_rows:
            payload = _decode_raw(item.date)
            alarms_table.append(
                {
                    "created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "name": item.name,
                    "description": item.description or "",
                    "payload": json.dumps(payload, ensure_ascii=False),
                }
            )

        return render_template_string(
            """
            <h1>Данные БД</h1>
            <a href="{{ url_for('dashboard') }}">Назад</a>
            <h2>Analogs (CSV-вид)</h2>
            <table border="1" cellspacing="0" cellpadding="4">
                <tr><th>created_at</th>{% for col in analog_cols %}<th>{{ col }}</th>{% endfor %}</tr>
                {% for row in analog_table %}
                    <tr><td>{{ row.created_at }}</td>{% for v in row.values %}<td>{{ v }}</td>{% endfor %}</tr>
                {% endfor %}
            </table>
            <h2>Discretes (CSV-вид)</h2>
            <table border="1" cellspacing="0" cellpadding="4">
                <tr><th>created_at</th>{% for col in discrete_cols %}<th>{{ col }}</th>{% endfor %}</tr>
                {% for row in discrete_table %}
                    <tr><td>{{ row.created_at }}</td>{% for v in row.values %}<td>{{ v }}</td>{% endfor %}</tr>
                {% endfor %}
            </table>
            <h2>Alarms</h2>
            <table border="1" cellspacing="0" cellpadding="4">
                <tr><th>created_at</th><th>name</th><th>description</th><th>payload</th></tr>
                {% for row in alarms_table %}
                    <tr><td>{{ row.created_at }}</td><td>{{ row.name }}</td><td>{{ row.description }}</td><td>{{ row.payload }}</td></tr>
                {% endfor %}
            </table>
            """,
            analog_cols=ANALOG_CSV_COLUMNS,
            discrete_cols=DISCRETE_CSV_COLUMNS,
            analog_table=analog_table,
            discrete_table=discrete_table,
            alarms_table=alarms_table,
        )

    @app.get("/export.csv")
    def export_csv():
        if not is_auth():
            return redirect(url_for("login"))

        db = session_factory()
        try:
            rows = db.query(Analogs).order_by(Analogs.created_at.asc()).all()
        finally:
            db.close()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=",")
        writer.writerow(["line_no", "date", "time", *ANALOG_CSV_COLUMNS, *DISCRETE_CSV_COLUMNS])

        line_no = 0
        for row in rows:
            raw = _decode_raw(row.date)
            processed = convert_raw(raw)
            analog, discrete = analog_discrete_for_csv(processed)
            line_no += 1
            dt = row.created_at
            writer.writerow(
                [
                    line_no,
                    dt.strftime("%Y-%m-%d"),
                    dt.strftime("%H:%M:%S.%f")[:12],
                    *[analog.get(k, "") for k in ANALOG_CSV_COLUMNS],
                    *[1 if bool(discrete.get(k, False)) else 0 for k in DISCRETE_CSV_COLUMNS],
                ]
            )

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=export_{datetime.now():%Y%m%d_%H%M%S}.csv"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
