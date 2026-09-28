from __future__ import annotations

import ast
import builtins
import types
from typing import Any

from simpleeval import FunctionNotDefined, NameNotDefined, SimpleEval

ALLOWED_FUNCTIONS = frozenset({"abs", "min", "max", "round", "len"})


def _to_namespace_tree(values: dict[str, Any]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for key, value in values.items():
        parts = str(key).split(".")
        branch = tree
        for part in parts[:-1]:
            next_value = branch.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                branch[part] = next_value
            branch = next_value
        branch[parts[-1]] = value

    def convert(value: Any) -> Any:
        if isinstance(value, dict):
            return types.SimpleNamespace(**{key: convert(item) for key, item in value.items()})
        return value

    return {key: convert(value) for key, value in tree.items()}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def validate_rule_expression(
    expression: str,
    fields: set[str],
    *,
    list_fields: set[str] | None = None,
    error_labels: set[str] | None = None,
) -> tuple[bool, str | None]:
    text = str(expression or "").strip()
    if not text:
        return False, "Укажите выражение правила."
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        return False, f"Синтаксическая ошибка: {exc.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
                return False, "Разрешены только функции abs, min, max, round и len."
    parent_map = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            parent = parent_map.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            roots.append(node)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            parent = parent_map.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            roots.append(node)
    for root in roots:
        reference = _dotted(root)
        if reference and reference not in fields:
            return False, f"Неизвестное поле «{reference}». Проверьте имена полей карты ВМ."
    labels = error_labels or set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for operator, right in zip(node.ops, node.comparators):
            if isinstance(operator, (ast.In, ast.NotIn)) and isinstance(left, ast.Constant) and isinstance(left.value, str):
                reference = _dotted(right)
                if reference != "active_alarms" and left.value not in labels:
                    return False, f"Неизвестная подпись аварии «{left.value}». Используйте подписи битовых полей карты."
            left = right
    collections = set(list_fields or ()) | {"active_alarms", "active_status", "alarms"}
    sample = {name: ([] if name in collections else (False if name.startswith("GPIO_") else 1)) for name in fields}
    sample["active_alarms"] = []
    try:
        result = _make_eval(sample).eval(text)
    except (NameNotDefined, FunctionNotDefined, TypeError, ZeroDivisionError, AttributeError) as exc:
        return False, f"Не удалось проверить выражение: {exc}"
    except Exception as exc:
        return False, f"Ошибка проверки выражения: {exc}"
    if not isinstance(result, bool):
        return False, "Условие должно возвращать True или False."
    return True, None


def evaluate_rule_expression(expression: str, values: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        result = _make_eval(values).eval(str(expression or "").strip())
    except NameNotDefined as exc:
        return False, f"Пропущено поле данных: {exc}"
    except Exception as exc:
        return False, f"Ошибка вычисления правила: {exc}"
    if not isinstance(result, bool):
        return False, "Условие должно возвращать логическое значение."
    return result, None


def _make_eval(values: dict[str, Any]) -> SimpleEval:
    functions = {name: getattr(builtins, name) for name in ALLOWED_FUNCTIONS}
    return SimpleEval(names=_to_namespace_tree(values), functions=functions)
