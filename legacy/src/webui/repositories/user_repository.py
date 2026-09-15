from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import sessionmaker

from src.database import User


class UserRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_active_by_username(self, username: str) -> User | None:
        stmt = (
            select(User)
            .options(joinedload(User.type_user))
            .where(User.username == username, User.is_deleted.is_(False))
            .limit(1)
        )
        with self._session_factory() as session:
            return session.execute(stmt).scalars().first()
