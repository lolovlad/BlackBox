from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import joinedload, sessionmaker

from src.database import Emergency, EmergencyConditions


class EmergencyRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def list_conditions(self) -> list[EmergencyConditions]:
        stmt = (
            select(EmergencyConditions)
            .where(EmergencyConditions.is_deleted.is_(False))
            .order_by(EmergencyConditions.id.asc())
        )
        with self._session_factory() as session:
            return list(session.execute(stmt).scalars().all())

    def create_condition(self, *, name: str, condition: str) -> EmergencyConditions:
        row = EmergencyConditions(name=name, condition=condition)
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def update_condition(self, *, condition_id: int, name: str, condition: str) -> bool:
        with self._session_factory() as session:
            row = session.get(EmergencyConditions, condition_id)
            if row is None or row.is_deleted:
                return False
            row.name = name
            row.condition = condition
            session.commit()
            return True

    def soft_delete_condition(self, condition_id: int) -> bool:
        with self._session_factory() as session:
            row = session.get(EmergencyConditions, condition_id)
            if row is None or row.is_deleted:
                return False
            row.is_deleted = True
            row.deleted_at = datetime.now()
            session.commit()
            return True

    def list_recent_emergencies(self, *, limit: int = 200) -> list[Emergency]:
        stmt = (
            select(Emergency)
            .options(joinedload(Emergency.emergency_condition))
            .where(Emergency.is_deleted.is_(False))
            .order_by(Emergency.datetime.desc())
            .limit(limit)
        )
        with self._session_factory() as session:
            return list(session.execute(stmt).unique().scalars().all())

    def get_emergency_event(self, event_id: int) -> Emergency | None:
        stmt = (
            select(Emergency)
            .options(joinedload(Emergency.emergency_condition))
            .where(
                Emergency.id == event_id,
                Emergency.is_deleted.is_(False),
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.execute(stmt).scalar_one_or_none()
