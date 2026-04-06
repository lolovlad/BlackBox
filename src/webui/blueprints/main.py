from __future__ import annotations

from flask import Blueprint, redirect, render_template, session, url_for

from src.webui.auth_utils import login_required

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    if session.get("auth"):
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("auth.login"))


@bp.get("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard/index.html")
