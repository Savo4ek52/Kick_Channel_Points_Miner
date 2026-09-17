#!/usr/bin/env python3
"""
Проверка Kick Bearer-токенов из config.json без запуска майнера.

Почему это нужно:
  Ошибка `403 при WS-токене` в Kick_Channel_Points_Miner означает,
  что Kick отклонил Authorization-токен аккаунта (протух / неверно скопирован),
  а НЕ бан IP и НЕ проблему с Cloudflare.

Что делает скрипт:
  1. Проверяет сеть и Cloudflare (анонимные запросы — должны давать 200).
  2. Проверяет КАЖДЫЙ токен из Accounts через приватный endpoint
     https://websockets.kick.com/viewer/v1/token :
       200 -> токен ВАЛИДЕН
       403 -> токен НЕВАЛИДЕН (протух / обрезан / не тот скопирован)
  3. Дополнительно проверяет через points-endpoint (401 = невалиден).
  4. Находит типовые ошибки формата: префикс "Bearer ", пробелы, кавычки.

Токены целиком НЕ печатаются — только маска вида 1234...abcd.

Использование:
  pip install curl_cffi
  python check_kick_token.py [путь/к/config.json]

  По умолчанию ищет ./config.json, затем ./Kick_Channel_Points_Miner/config.json
"""

import json
import os
import sys

try:
    from curl_cffi import requests
except ImportError:
    print("Нужна библиотека curl_cffi:  pip install curl_cffi")
    sys.exit(2)

CLIENT_TOKEN = "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823"
PROBE_STREAMER = "xqc"  # любой существующий канал, нужен только для 200-проверок
TIMEOUT = 15


def mask(token: str) -> str:
    t = (token or "").strip()
    if len(t) <= 10:
        return "***too-short***"
    return f"{t[:4]}...{t[-4:]} (len={len(t)})"


def sanitize(token: str):
    """Возвращает (чистый_токен, список_замечаний)."""
    notes = []
    t = token or ""
    if t != t.strip():
        notes.append("по краям есть пробелы/перенос строки — они будут обрезаны")
    t = t.strip().strip('"').strip("'").strip()
    if t.lower().startswith("bearer "):
        notes.append('в config.json лежит "Bearer <токен>" — префикс "Bearer " лишний, нужен только сам токен')
        t = t[7:].strip()
    if "|" not in t:
        notes.append('формат подозрительный: валидный токен выглядит как 12345678|xxxxxxxxxxxxxxxx (id|hash)')
    elif len(t) < 30:
        notes.append("токен слишком короткий — похоже, скопирован не полностью")
    return t, notes


def make_session():
    s = requests.Session(impersonate="chrome120")
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://kick.com",
        "Referer": "https://kick.com/",
        "X-Client-Token": CLIENT_TOKEN,
    })
    return s


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not cfg_path:
        for cand in ("config.json", "Kick_Channel_Points_Miner/config.json"):
            if os.path.exists(cand):
                cfg_path = cand
                break
    if not cfg_path or not os.path.exists(cfg_path):
        print("config.json не найден. Укажите путь:  python check_kick_token.py /путь/к/config.json")
        sys.exit(2)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    accounts = cfg.get("Accounts", [])
    if not accounts:  # старый формат
        tok = cfg.get("Private", {}).get("token", "")
        if tok:
            accounts = [{"alias": "Default", "token": tok}]

    if not accounts:
        print("В конфиге нет аккаунтов (Accounts пустой).")
        sys.exit(2)

    print(f"Конфиг: {cfg_path}, аккаунтов: {len(accounts)}")
    print("=" * 64)

    # --- Шаг 0: сеть / Cloudflare / IP (анонимно, без токена) ---
    print("[0] Проверка сети и Cloudflare (анонимно)...")
    s = make_session()
    try:
        r = s.get("https://kick.com", timeout=TIMEOUT)
        print(f"    kick.com -> {r.status_code}  {'OK' if r.status_code == 200 else 'ПРОБЛЕМА'}")
        if r.status_code == 403:
            print("    !! 403 уже на kick.com — IP заблокирован Cloudflare, нужен прокси/VPN.")
            print("    Дальнейшие проверки бессмысленны, пока не сменится IP.")
            return
        r = s.get(f"https://kick.com/api/v2/channels/{PROBE_STREAMER}", timeout=TIMEOUT)
        print(f"    channel API -> {r.status_code}  {'OK' if r.status_code == 200 else 'ПРОБЛЕМА'}")
        r = s.get("https://websockets.kick.com/viewer/v1/token", timeout=TIMEOUT)
        anon_ok = r.status_code == 200
        print(f"    viewer/token (анонимно) -> {r.status_code}  {'OK' if anon_ok else 'ПРОБЛЕМА'}")
        if not anon_ok:
            print("    !! Анонимный запрос к viewer/token не проходит — проблема сети/IP/CF, а не токена.")
            return
        print("    Вывод: сеть, Cloudflare и IP в порядке. Если дальше будет 403 — виноват именно токен.\n")
    except Exception as e:
        print(f"    Сетевая ошибка: {e}")
        return
    finally:
        s.close()

    # --- Проверка каждого аккаунта ---
    all_valid = True
    for acc in accounts:
        alias = acc.get("alias", "?")
        raw = acc.get("token", "") or ""
        print(f"--- [{alias}] токен: {mask(raw)} ---")
        if not raw.strip():
            print("    !! Токен пустой в config.json")
            all_valid = False
            continue
        token, notes = sanitize(raw)
        for n in notes:
            print(f"    !! Формат: {n}")

        s = make_session()
        try:
            s.get("https://kick.com", timeout=TIMEOUT)
            # Основная проверка: viewer/token с Authorization
            r = s.get("https://websockets.kick.com/viewer/v1/token",
                      headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            body = r.text[:120].replace("\n", " ")
            print(f"    viewer/token с токеном -> {r.status_code}  {body}")
            if r.status_code == 200:
                print("    ✅ ВАЛИДЕН — этот токен Kick принимает.")
            elif r.status_code == 403:
                print("    ❌ НЕВАЛИДЕН — Kick отвечает 403 Forbidden.")
                print("       Токен протух, обрезан или скопировано не то значение.")
                print("       Получите свежий Bearer-токен из DevTools (инструкция ниже) и замените в config.json.")
                all_valid = False
            else:
                print(f"    ⚠️ Неожиданный статус {r.status_code} — повторите позже.")
                all_valid = False

            # Дополнительная проверка через points endpoint
            r2 = s.get(f"https://kick.com/api/v2/channels/{PROBE_STREAMER}/points",
                       headers={"Authorization": f"Bearer {token}",
                                "Referer": f"https://kick.com/{PROBE_STREAMER}/"},
                       timeout=TIMEOUT)
            print(f"    points endpoint -> {r2.status_code}  "
                  f"{'(токен принят)' if r2.status_code in (200, 404) else '(токен отклонён: 401)' if r2.status_code == 401 else ''}")
        except Exception as e:
            print(f"    Сетевая ошибка: {e}")
            all_valid = False
        finally:
            s.close()
        print()

    print("=" * 64)
    if all_valid:
        print("ИТОГ: все токены валидны. Если майнер всё равно даёт 403 — обновите код (патч ниже) и перезапустите.")
    else:
        print("ИТОГ: найдены НЕВАЛИДНЫЕ токены — вот причина '403 при WS-токене' в логах.")
        print()
        print("Как получить свежий токен (2 минуты):")
        print("  1. В браузере залогиньтесь на kick.com под нужным аккаунтом.")
        print("  2. Нажмите F12 -> вкладка Network (Сеть), включите фильтр Fetch/XHR.")
        print("  3. Обновите страницу, кликните любой запрос к kick.com/api/...")
        print("  4. В Request Headers найдите  authorization: Bearer 12345678|xxxxxxxx...")
        print("  5. Скопируйте ТОЛЬКО часть после 'Bearer ' (без пробелов), целиком,")
        print("     вид:  цифры | длинный hash, напр. 12345678|AbC...")
        print("  6. Вставьте в config.json -> Accounts -> token нужного alias, сохраните, перезапустите майнер.")
        print()
        print("Типовые ошибки: скопировали вместе со словом 'Bearer',")
        print("обрезали хвост, взяли session_token из Cookies вместо Bearer,")
        print("взяли токен другого аккаунта, разлогинились на сайте (токен сгорел).")


if __name__ == "__main__":
    main()
