from __future__ import annotations

from functools import wraps

from flask import redirect, session, url_for


def login_required(view_func):
    @wraps(view_func)
    def _wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("auth.login"))
        return view_func(*args, **kwargs)

    return _wrapped
