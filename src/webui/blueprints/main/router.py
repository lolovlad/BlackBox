from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user, login_required

from src.webui.paths import TEMPLATES_DIR

main_router = Blueprint("main", __name__, template_folder=str(TEMPLATES_DIR))


@main_router.route("/", methods=["GET"])
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main_blueprint.dashboard"))
    return redirect(url_for("auth_blueprint.login"))


@main_router.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    return render_template("dashboard/index.html")
