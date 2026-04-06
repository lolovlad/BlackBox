from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from src.database import Alarms, Analogs, Discretes


class DataRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def list_analogs(self, limit: int):
        dbs = self._session_factory()
        try:
            return dbs.query(Analogs).order_by(Analogs.created_at.desc()).limit(limit).all()
        finally:
            dbs.close()

    def list_discretes(self, limit: int):
        dbs = self._session_factory()
        try:
            return dbs.query(Discretes).order_by(Discretes.created_at.desc()).limit(limit).all()
        finally:
            dbs.close()

    def list_alarms(self, limit: int):
        dbs = self._session_factory()
        try:
            return dbs.query(Alarms).order_by(Alarms.created_at.desc()).limit(limit).all()
        finally:
            dbs.close()
