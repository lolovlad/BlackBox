"""Валидация выражений правил аварий (simpleeval + settings.json)."""

from __future__ import annotations

import ast
import builtins
import json
import types
from pathlib import Path
from typing import Any

from simpleeval import FunctionNotDefined, NameNotDefined, SimpleEval

_ALLOWED_CALL_NAMES = frozenset({"abs", "min", "max", "round", "len"})


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    pmap: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            pmap[child] = parent
    return pmap


def _is_chain_interior_name_or_attr(node: ast.AST, pmap: dict[ast.AST, ast.AST]) -> bool:
    parent = pmap.get(node)
    return isinstance(parent, ast.Attribute) and parent.value is node


def _dotted_from_name_or_attr(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_from_name_or_attr(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _collect_reference_roots(tree: ast.AST) -> list[ast.AST]:
    pmap = _parent_map(tree)
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if _is_chain_interior_name_or_attr(node, pmap):
                continue
            roots.append(node)
        elif isinstance(node, ast.Attribute):
            if _is_chain_interior_name_or_attr(node, pmap):
                continue
            roots.append(node)
    return roots


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect_membership_string_literals(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                s = _const_str(left)
                if s is not None:
                    out.append(s)
            left = right
    return out


def load_settings_fields(settings_path: Path | str) -> dict[str, Any]:
    path = Path(settings_path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings.json должен быть JSON-объектом")
    return data


def build_rule_validation_sets(config: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Имена полей (как ключи в processed) и подписи битовых ошибок из bitfield."""
    field_names: set[str] = set()
    error_labels: set[str] = set()
    for field in config.get("fields", []):
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if name:
            field_names.add(str(name))
        bits = field.get("bits")
        if isinstance(bits, dict):
            for label in bits.values():
                if label is not None:
                    error_labels.add(str(label))
    return field_names, error_labels


def validate_emergency_rule_expression(
    expr: str,
    *,
    settings_path: Path | str | None = None,
    settings_config: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """
    Проверяет синтаксис, допустимость имён (поля из settings.json) и строк в ``in`` / ``not in``
    против подписей битовых аварий. Пробное вычисление — с фиктивными значениями (результат должен быть bool).
    """
    text = (expr or "").strip()
    if not text:
        return False, "Выражение не должно быть пустым."

    if settings_config is not None:
        cfg = settings_config
    elif settings_path is not None:
        cfg = load_settings_fields(settings_path)
    else:
        return False, "Не задан путь к settings.json или объект конфигурации."

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        return False, f"Синтаксическая ошибка: {exc.msg}"

    field_names, error_labels = build_rule_validation_sets(cfg)

    roots = _collect_reference_roots(tree)
    for root in roots:
        ref = _dotted_from_name_or_attr(root)
        if ref is None:
            continue
        if ref not in field_names:
            return False, (
                f"Неизвестная переменная или поле: «{ref}». "
                "Используйте имена из settings.json (аналоги, дискреты, регистры bitfield, expr-поля)."
            )

    for literal in _collect_membership_string_literals(tree):
        if literal not in error_labels:
            return False, (
                f"Неизвестная строка аварии в проверке вхождения: «{literal}». "
                "Используйте подписи из секций bits в bitfield полях settings.json."
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                return False, "Вызовы методов не разрешены."
            if isinstance(func, ast.Name):
                if func.id not in _ALLOWED_CALL_NAMES:
                    return False, (
                        f"Вызов «{func.id}()» не разрешён. Допустимы: {', '.join(sorted(_ALLOWED_CALL_NAMES))}."
                    )
            else:
                return False, "Допустимы только простые вызовы abs, min, max, round, len."

    flat_dummy: dict[str, Any] = {n: _dummy_value_for_field(n, cfg) for n in field_names}
    try:
        dummy_names = _flat_field_names_to_eval_names(flat_dummy)
    except ValueError as exc:
        return False, str(exc)
    allowed_funcs = {n: getattr(builtins, n) for n in _ALLOWED_CALL_NAMES if hasattr(builtins, n)}
    s = SimpleEval(names=dummy_names, functions=allowed_funcs)
    try:
        result = s.eval(text)
    except NameNotDefined as exc:
        return False, f"Неизвестное имя в выражении: {exc}"
    except FunctionNotDefined as exc:
        return False, f"Функция не разрешена: {exc}"
    except TypeError as exc:
        return False, f"Типы операндов не согласованы (проверьте сравнения и списки): {exc}"
    except ZeroDivisionError:
        return False, "Деление на ноль при проверке с тестовыми значениями."
    except Exception as exc:  # noqa: BLE001
        return False, f"Ошибка при проверке выражения: {exc}"

    if not isinstance(result, bool):
        return False, "Выражение должно давать логическое значение (True/False) при тестовых данных."

    return True, None


def evaluate_emergency_rule_expression(expr: str, *, processed: dict[str, Any]) -> tuple[bool, bool, str | None]:
    """Выполняет правило на реальном срезе данных."""
    text = (expr or "").strip()
    if not text:
        return False, False, "Пустое выражение правила."
    try:
        names = _flat_field_names_to_eval_names(dict(processed))
    except ValueError as exc:
        return False, False, str(exc)
    allowed_funcs = {n: getattr(builtins, n) for n in _ALLOWED_CALL_NAMES if hasattr(builtins, n)}
    s = SimpleEval(names=names, functions=allowed_funcs)
    try:
        result = s.eval(text)
    except NameNotDefined as exc:
        # Runtime snapshot may temporarily miss a field that exists in the rule.
        # Treat as "rule not fired" to avoid noisy per-cycle error logs.
        return True, False, f"Пропущено поле в данных: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, False, f"Ошибка вычисления правила: {exc}"
    if not isinstance(result, bool):
        return False, False, "Правило должно возвращать bool."
    return True, result, None


def _dict_to_namespace(d: dict[str, Any]) -> Any:
    fields: dict[str, Any] = {}
    for kk, vv in d.items():
        fields[kk] = _dict_to_namespace(vv) if isinstance(vv, dict) else vv
    return types.SimpleNamespace(**fields)


def _flat_field_names_to_eval_names(flat: dict[str, Any]) -> dict[str, Any]:
    """Имена вида Gov.Reg.Value превращаются в объекты для цепочек атрибутов simpleeval."""
    tree: dict[str, Any] = {}
    for key, val in flat.items():
        parts = key.split(".")
        cur: dict[str, Any] = tree
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        leaf = parts[-1]
        if leaf in cur and isinstance(cur[leaf], dict):
            raise ValueError(
                f"Конфликт имён в settings.json: поле «{key}» пересекается с составным именем «{leaf}»."
            )
        cur[leaf] = val
    out: dict[str, Any] = {}
    for k, v in tree.items():
        out[k] = _dict_to_namespace(v) if isinstance(v, dict) else v
    return out


def _dummy_value_for_field(field_name: str, config: dict[str, Any]) -> Any:
    for field in config.get("fields", []):
        if not isinstance(field, dict) or str(field.get("name")) != field_name:
            continue
        f_type = field.get("type", "uint16")
        if f_type == "bool":
            return False
        if f_type == "bitfield":
            return []
        if f_type == "expr":
            lower = field_name.lower()
            if "alarm" in lower or "status" in lower:
                return []
            return 0.0
        if f_type in {"uint16", "uint32_be"}:
            return 1
        return 0.0
    return 0.0
