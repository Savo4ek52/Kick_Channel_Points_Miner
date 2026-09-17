"""Общая проверка валидности Kick Bearer-токена.

Используется и Telegram-ботом (/settoken, /checktokens),
и фоновым сторожем токенов в AccountManager.

Критерий: приватный endpoint viewer/v1/token
  200 -> токен валиден, 403 -> невалиден (протух/неверен).
"""

from typing import Tuple

CLIENT_TOKEN = (
    "e1393935a959b4020a4491574f6490129f678acda"
    "aa92760471263db43487f823"
)


def sanitize_token(token: str) -> str:
    t = (token or "").strip().strip('"').strip("'").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def validate_kick_token(token: str,
                        proxy: str = None,
                        timeout: int = 15) -> Tuple[bool, str]:
    """Проверить Bearer-токен через приватный endpoint Kick.

    Возвращает (valid, info), где info:
      'ok' | 'invalid' | 'error: ...'
    Синхронная — из async-кода вызывать через asyncio.to_thread.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return False, "error: curl_cffi not installed"

    t = sanitize_token(token)
    if not t:
        return False, "error: empty token"

    proxies = {"http": proxy, "https": proxy} if proxy else None
    s = cffi_requests.Session(
        impersonate="chrome120", proxies=proxies
    )
    try:
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://kick.com",
            "Referer": "https://kick.com/",
            "X-Client-Token": CLIENT_TOKEN,
        })
        s.get("https://kick.com", timeout=timeout)
        r = s.get(
            "https://websockets.kick.com/viewer/v1/token",
            headers={"Authorization": f"Bearer {t}"},
            timeout=timeout,
        )
        if r.status_code == 200:
            return True, "ok"
        if r.status_code == 403:
            return False, "invalid"
        return False, f"error: HTTP {r.status_code}"
    except Exception as e:
        return False, f"error: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass
