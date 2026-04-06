from __future__ import annotations

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String
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
    __tablename__ = 'type_user'


class User(db.Model, DeleteMixin):
    __tablename__ = 'user'
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    type_user_id: Mapped[int] = mapped_column(ForeignKey('type_user.id'))
    type_user: Mapped[TypeUser] = relationship(TypeUser)


class Analogs(db.Model, DateMixin):
    __tablename__ = 'analogs'


class Discretes(db.Model, DateMixin):
    __tablename__ = 'discretes'


class Alarms(db.Model, DateMixin):
    __tablename__ = 'alarms'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=True)
