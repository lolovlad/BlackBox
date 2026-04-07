from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from src.database import Alarms, Analogs, Discretes


class DataRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def count_analogs(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        dbs = self._session_factory()
        try:
            q = dbs.query(Analogs)
            if created_from is not None:
                q = q.filter(Analogs.created_at >= created_from)
            if created_to is not None:
                q = q.filter(Analogs.created_at <= created_to)
            return q.count()
        finally:
            dbs.close()

    def count_discretes(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        dbs = self._session_factory()
        try:
            q = dbs.query(Discretes)
            if created_from is not None:
                q = q.filter(Discretes.created_at >= created_from)
            if created_to is not None:
                q = q.filter(Discretes.created_at <= created_to)
            return q.count()
        finally:
            dbs.close()

    def count_alarms(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        dbs = self._session_factory()
        try:
            q = dbs.query(Alarms)
            if created_from is not None:
                q = q.filter(Alarms.created_at >= created_from)
            if created_to is not None:
                q = q.filter(Alarms.created_at <= created_to)
            return q.count()
        finally:
            dbs.close()

    def list_analogs(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        dbs = self._session_factory()
        try:
            q = dbs.query(Analogs)
            if created_from is not None:
                q = q.filter(Analogs.created_at >= created_from)
            if created_to is not None:
                q = q.filter(Analogs.created_at <= created_to)
            order_col = Analogs.created_at.desc() if sort_desc else Analogs.created_at.asc()
            q = q.order_by(order_col)
            if offset:
                q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            return q.all()
        finally:
            dbs.close()

    def list_discretes(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        dbs = self._session_factory()
        try:
            q = dbs.query(Discretes)
            if created_from is not None:
                q = q.filter(Discretes.created_at >= created_from)
            if created_to is not None:
                q = q.filter(Discretes.created_at <= created_to)
            order_col = Discretes.created_at.desc() if sort_desc else Discretes.created_at.asc()
            q = q.order_by(order_col)
            if offset:
                q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            return q.all()
        finally:
            dbs.close()

    def list_alarms(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        dbs = self._session_factory()
        try:
            q = dbs.query(Alarms)
            if created_from is not None:
                q = q.filter(Alarms.created_at >= created_from)
            if created_to is not None:
                q = q.filter(Alarms.created_at <= created_to)
            order_col = Alarms.created_at.desc() if sort_desc else Alarms.created_at.asc()
            q = q.order_by(order_col)
            if offset:
                q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            return q.all()
        finally:
            dbs.close()
