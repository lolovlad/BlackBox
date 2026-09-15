from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from src.database import AlarmRaspberry, Alarms, EventLog, Samples, Video


class DataRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _apply_date_filters(stmt, model, created_from: datetime | None, created_to: datetime | None):
        if created_from is not None:
            stmt = stmt.where(model.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(model.created_at <= created_to)
        return stmt

    def _count_rows(self, model, *, created_from: datetime | None = None, created_to: datetime | None = None) -> int:
        stmt = select(func.count()).select_from(model)
        stmt = self._apply_date_filters(stmt, model, created_from, created_to)
        with self._session_factory() as session:
            return int(session.execute(stmt).scalar_one())

    def _list_rows(
        self,
        model,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        stmt = select(model)
        stmt = self._apply_date_filters(stmt, model, created_from, created_to)
        order_col = model.created_at.desc() if sort_desc else model.created_at.asc()
        stmt = stmt.order_by(order_col)
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._session_factory() as session:
            return session.execute(stmt).scalars().all()

    def count_analogs(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        return self._count_rows(Samples, created_from=created_from, created_to=created_to)

    def count_discretes(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        return self._count_rows(Samples, created_from=created_from, created_to=created_to)

    def count_alarms(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        return self._count_rows(Alarms, created_from=created_from, created_to=created_to)

    def count_gpio_alarms(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        return self._count_rows(AlarmRaspberry, created_from=created_from, created_to=created_to)

    def list_analogs(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        return self._list_rows(
            Samples,
            created_from=created_from,
            created_to=created_to,
            sort_desc=sort_desc,
            offset=offset,
            limit=limit,
        )

    def list_discretes(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        return self._list_rows(
            Samples,
            created_from=created_from,
            created_to=created_to,
            sort_desc=sort_desc,
            offset=offset,
            limit=limit,
        )

    def list_alarms(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        return self._list_rows(
            Alarms,
            created_from=created_from,
            created_to=created_to,
            sort_desc=sort_desc,
            offset=offset,
            limit=limit,
        )

    def list_gpio_alarms(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        return self._list_rows(
            AlarmRaspberry,
            created_from=created_from,
            created_to=created_to,
            sort_desc=sort_desc,
            offset=offset,
            limit=limit,
        )

    def list_event_logs(
        self,
        *,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = 200,
    ):
        return self._list_rows(
            EventLog,
            sort_desc=sort_desc,
            offset=offset,
            limit=limit,
        )

    def count_videos(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> int:
        return self._count_rows(Video, created_from=created_from, created_to=created_to)

    def list_videos(
        self,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int | None = None,
    ):
        return self._list_rows(
            Video,
            created_from=created_from,
            created_to=created_to,
            sort_desc=sort_desc,
            offset=offset,
            limit=limit,
        )
