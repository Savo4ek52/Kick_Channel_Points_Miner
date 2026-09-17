"""Проверка config.json при старте: fail-fast вместо молчаливых глюков.

Возвращает (warnings, fatal):
  warnings — список строк-предупреждений (майнер запустится),
  fatal    — строка с причиной отказа в запуске или None.
"""

import re
from typing import Tuple, List, Optional

STREAMER_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")


def _check_token(alias: str, token) -> Tuple[List[str], bool]:
    warns: List[str] = []
    if not token or not str(token).strip():
        return [f"[{alias}] токен пустой"], False
    t = str(token).strip()
    if t.lower().startswith("bearer "):
        warns.append(
            f"[{alias}] токен начинается с 'Bearer ' — "
            f"префикс будет снят автоматически, но лучше "
            f"убрать его из config.json"
        )
        t = t[7:].strip()
    if "|" not in t:
        warns.append(
            f"[{alias}] токен не похож на валидный "
            f"(нет '|', ожидался вид 12345678|xxxx...)"
        )
        return warns, False
    if len(t) < 30:
        warns.append(f"[{alias}] токен подозрительно короткий")
        return warns, False
    return warns, True


def validate_config(config: dict) -> Tuple[List[str], Optional[str]]:
    warnings: List[str] = []

    accounts = config.get("Accounts")
    if accounts is None:
        # старый формат — менеджер сконвертирует сам
        old_token = config.get("Private", {}).get("token", "")
        old_streamers = config.get("Streamers", [])
        if not old_token or not old_streamers:
            return [], (
                "Нет ни Accounts[], ни старого формата "
                "(Private.token + Streamers). Нечего запускать."
            )
        w, _ = _check_token("Default", old_token)
        warnings.extend(w)
        return warnings, None

    if not isinstance(accounts, list) or not accounts:
        return [], "Accounts[] пуст — добавьте хотя бы один аккаунт."

    seen_aliases = set()
    usable = 0
    for i, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            return [], f"Accounts[{i}] — не объект."
        alias = acc.get("alias") or f"#{i + 1}"
        if alias in seen_aliases:
            warnings.append(f"Дублирующийся alias '{alias}'.")
        seen_aliases.add(alias)

        w, ok = _check_token(alias, acc.get("token", ""))
        warnings.extend(w)
        if ok and not acc.get("disabled"):
            usable += 1

        streamers = acc.get("streamers", [])
        if not streamers:
            warnings.append(f"[{alias}] список стримеров пуст.")
        for s in streamers or []:
            if not STREAMER_RE.match(str(s)):
                warnings.append(
                    f"[{alias}] плохое имя стримера '{s}' "
                    f"(латиница/цифры/_ , 1-25 символов)."
                )

        try:
            mc = int(acc.get("max_concurrent", 2))
        except (TypeError, ValueError):
            return [], f"[{alias}] max_concurrent — не число."
        if mc < 1 or mc > 10:
            warnings.append(
                f"[{alias}] max_concurrent={mc} вне 1..10 — "
                f"будет обрезан."
            )

    if usable == 0:
        return warnings, (
            "Нет ни одного рабочего аккаунта "
            "(все токены пустые/битые или все disabled)."
        )
    return warnings, None
