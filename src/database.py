from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


class DeleteMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(default=datetime.now)
    is_deleted: Mapped[bool] = mapped_column(default=False)


class SystemMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=True)


class DateMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    date: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class TypeUser(db.Model, SystemMixin):
    __tablename__ = "type_user"


class User(db.Model, UserMixin, DeleteMixin):
    __tablename__ = "user"
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    type_user_id: Mapped[int] = mapped_column(ForeignKey("type_user.id"))
    type_user: Mapped[TypeUser] = relationship(TypeUser)


class Samples(db.Model, DateMixin):
    __tablename__ = "samples"


class Alarms(db.Model, DateMixin):
    __tablename__ = "alarms"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    description: Mapped[str] = mapped_column(String(255), nullable=True)
    videos: Mapped[list["Video"]] = relationship(back_populates="alarm")


class EventLog(db.Model):
    __tablename__ = "event_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmergencyConditions(db.Model, DeleteMixin):
    __tablename__ = "emergency_conditions"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    emergencies: Mapped[list["Emergency"]] = relationship(back_populates="emergency_condition")


class Emergency(db.Model, DeleteMixin):
    __tablename__ = "emergency"
    datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    id_emergency_condition: Mapped[int] = mapped_column(ForeignKey("emergency_conditions.id"), nullable=False)
    emergency_condition: Mapped[EmergencyConditions] = relationship(back_populates="emergencies")


class Video(db.Model):
    __tablename__ = "videos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    alarm_id: Mapped[int | None] = mapped_column(ForeignKey("alarms.id"), nullable=True)
    alarm: Mapped[Alarms | None] = relationship(back_populates="videos")
