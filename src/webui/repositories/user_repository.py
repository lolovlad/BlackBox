from __future__ import annotations

from sqlalchemy.orm import joinedload

from src.database import User


class UserRepository:
    @staticmethod
    def get_active_by_username(username: str) -> User | None:
        return (
            User.query.options(joinedload(User.type_user))
            .filter_by(username=username, is_deleted=False)
            .first()
        )
