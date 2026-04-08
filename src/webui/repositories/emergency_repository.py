from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import joinedload, sessionmaker

from src.database import Emergency, EmergencyConditions


class EmergencyRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def list_conditions(self) -> list[EmergencyConditions]:
        stmt = select(EmergencyConditions).order_by(EmergencyConditions.id.asc())
        with self._session_factory() as session:
            return list(session.execute(stmt).scalars().all())

    def create_condition(self, *, condition: str) -> EmergencyConditions:
        row = EmergencyConditions(condition=condition)
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def delete_condition(self, condition_id: int) -> bool:
        with self._session_factory() as session:
            row = session.get(EmergencyConditions, condition_id)
            if row is None:
                return False
            session.delete(row)
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
