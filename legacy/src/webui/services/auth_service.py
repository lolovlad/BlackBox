from __future__ import annotations

from src.database import User
from src.webui.repositories.user_repository import UserRepository


class AuthService:
    def __init__(self, user_repository: UserRepository | None = None) -> None:
        self._users = user_repository

    def authenticate(self, username: str, password: str, *, session_factory=None) -> User | None:
        users = self._users or UserRepository(session_factory)
        user = users.get_active_by_username(username)
        if user and user.password == password:
            return user
        return None
