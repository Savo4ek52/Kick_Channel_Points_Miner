import asyncio
import random
import time
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING
from dataclasses import dataclass, field
from loguru import logger

from _websockets.ws_token import KickPoints
from _websockets.ws_connect import KickWebSocket
from utils.kick_utility import KickUtility
from utils.get_points_amount import PointsAmount
from utils.token_check import validate_kick_token
from stats_db import StatsDB

if TYPE_CHECKING:
    from discord_webhook import DiscordWebhook
    from tg_bot.bot import TelegramBot


@dataclass
class StreamerState:
    name: str
    priority: int

    is_online: bool = False
    is_watching: bool = False
    points: int = 0
    last_points_update: Optional[datetime] = None
    stream_id: Optional[int] = None
    channel_id: Optional[int] = None

    ws_client: Optional[KickWebSocket] = None
    ws_task: Optional[asyncio.Task] = None
    points_task: Optional[asyncio.Task] = None

    last_error: Optional[str] = None
    error_count: int = 0
    cooldown_until: float = 0.0


@dataclass
class AccountState:
    alias: str
    token: str
    proxy: Optional[str]
    max_concurrent: int
    streamers: Dict[str, StreamerState] = field(default_factory=dict)
    streamer_order: List[str] = field(default_factory=list)

    @property
    def active_count(self) -> int:
        return sum(1 for s in self.streamers.values() if s.is_watching)

    @property
    def active_names(self) -> List[str]:
        return [s.name for s in self.streamers.values() if s.is_watching]


class AccountWorker:
    def __init__(
        self,
        account_cfg: dict,
        global_proxy: Optional[str] = None,
        check_interval: int = 120,
        reconnect_cooldown: int = 600,
        stagger_min: float = 3.0,
        stagger_max: float = 8.0,
        stats_db: Optional[StatsDB] = None,
    ):
        self._stats_db = stats_db
        self.alias = account_cfg["alias"]
        self.token = self._clean_token(account_cfg.get("token", ""))
        self.paused = bool(account_cfg.get("disabled", False))
        self.proxy = account_cfg.get("proxy") or global_proxy
        self.max_concurrent = account_cfg.get("max_concurrent", 2)
        self.check_interval = check_interval
        self.reconnect_cooldown = reconnect_cooldown
        self.stagger_min = stagger_min
        self.stagger_max = stagger_max

        streamer_names: List[str] = account_cfg.get("streamers", [])

        self.state = AccountState(
            alias=self.alias,
            token=self.token,
            proxy=self.proxy,
            max_concurrent=self.max_concurrent,
            streamer_order=streamer_names,
        )
        for idx, name in enumerate(streamer_names):
            self.state.streamers[name] = StreamerState(
                name=name, priority=idx
            )

        self._utility_cache: Dict[str, KickUtility] = {}
        self._points_checker: Optional[PointsAmount] = None
        self._ws_token_getter: Optional[KickPoints] = None
        self._discord: Optional["DiscordWebhook"] = None

        self._rebalance_lock = asyncio.Lock()
        self._running = False

    @staticmethod
    def _clean_token(token: str) -> str:
        """Чистит типовой мусор в токене из config.json.

        Самая частая причина '403 при WS-токене' — токен вставлен
        вместе со словом 'Bearer', с пробелами или кавычками.
        """
        cleaned = (token or "").strip().strip('"').strip("'").strip()
        if cleaned.lower().startswith("bearer "):
            logger.warning(
                "⚠️ В токене найден лишний префикс 'Bearer ' — "
                "убран автоматически. Исправьте config.json "
                "(нужен только сам токен, без слова Bearer)"
            )
            cleaned = cleaned[7:].strip()
        if cleaned and "|" not in cleaned:
            logger.warning(
                "⚠️ Токен не похож на валидный "
                "(нет символа '|', вид должен быть 12345678|xxxx...). "
                "Проверьте: python check_kick_token.py"
            )
        return cleaned

    def set_discord(self, discord: "DiscordWebhook"):
        self._discord = discord

    def _get_utility(self, streamer: str) -> KickUtility:
        if streamer not in self._utility_cache:
            self._utility_cache[streamer] = KickUtility(
                streamer, proxy=self.proxy
            )
        return self._utility_cache[streamer]

    def _get_points_checker(self) -> PointsAmount:
        if self._points_checker is None:
            self._points_checker = PointsAmount(proxy=self.proxy)
        return self._points_checker

    def _get_ws_token_getter(self) -> KickPoints:
        if self._ws_token_getter is None:
            self._ws_token_getter = KickPoints(
                self.token, proxy=self.proxy
            )
        return self._ws_token_getter

    async def start(self):
        self._running = True
        logger.info(
            f"[{self.alias}] Запуск: "
            f"{len(self.state.streamers)} стримеров, "
            f"лимит={self.max_concurrent}, "
            f"proxy={'да' if self.proxy else 'нет'}"
        )

        try:
            if self.paused:
                logger.info(
                    f"[{self.alias}] ⏸ Пауза (disabled) — "
                    f"проверки пропущены, снимите паузой /resume"
                )
            else:
                await self._check_all_online()
                await self._rebalance()

            while self._running:
                jitter = random.uniform(
                    self.check_interval * 0.8,
                    self.check_interval * 1.2,
                )
                await asyncio.sleep(jitter)
                if self.paused:
                    continue
                await self._check_all_online()
                await self._rebalance()

        except asyncio.CancelledError:
            logger.info(f"[{self.alias}] Worker отменён")
        except Exception as e:
            logger.error(f"[{self.alias}] Worker упал: {e}")
        finally:
            await self.stop()

    async def _check_all_online(self):
        for name in self.state.streamer_order:
            if not self._running:
                break
            try:
                utility = self._get_utility(name)
                stream_id = utility.get_stream_id(self.token)

                st = self.state.streamers[name]
                was_online = st.is_online
                st.is_online = stream_id is not None
                st.stream_id = stream_id

                if not was_online and st.is_online:
                    logger.info(
                        f"[{self.alias}] 🟢 {name} "
                        f"ОНЛАЙН (stream={stream_id})"
                    )
                    if self._discord:
                        self._discord.send_streamer_online(
                            self.alias, name,
                            st.priority, "online"
                        )

                elif was_online and not st.is_online:
                    logger.info(
                        f"[{self.alias}] 🔴 {name} ОФФЛАЙН"
                    )
                    if self._discord:
                        self._discord.send_streamer_online(
                            self.alias, name,
                            st.priority, "offline"
                        )

                elif not st.is_online:
                    logger.debug(
                        f"[{self.alias}] ⚫ {name} оффлайн "
                        f"(stream_id=None)"
                    )

                await asyncio.sleep(random.uniform(1.0, 2.5))

            except Exception as e:
                logger.warning(
                    f"[{self.alias}] Ошибка проверки {name}: {e}"
                )

    async def _rebalance(self):
        async with self._rebalance_lock:
            online_by_priority = [
                name
                for name in self.state.streamer_order
                if self.state.streamers[name].is_online
            ]

            desired = set(
                online_by_priority[: self.max_concurrent]
            )
            current = {
                name
                for name, s in self.state.streamers.items()
                if s.is_watching
            }

            to_stop = current - desired
            for name in to_stop:
                reason = (
                    "оффлайн"
                    if not self.state.streamers[name].is_online
                    else "вытеснен приоритетом"
                )
                logger.info(
                    f"[{self.alias}] ⏹ {name} — {reason}"
                )
                await self._stop_streamer(name)

                if (
                    self._discord
                    and reason == "вытеснен приоритетом"
                ):
                    self._discord.send_streamer_online(
                        self.alias, name,
                        self.state.streamers[name].priority,
                        "displaced"
                    )

            to_start = desired - current
            now = time.time()
            for name in to_start:
                st = self.state.streamers[name]
                if now < st.cooldown_until:
                    wait = int(st.cooldown_until - now)
                    logger.debug(
                        f"[{self.alias}] ⏳ {name} в кулдауне "
                        f"ещё {wait}с — пропуск"
                    )
                    continue
                logger.info(
                    f"[{self.alias}] ▶ {name} "
                    f"(приоритет={st.priority})"
                )
                await self._start_streamer(name)

                if self._discord:
                    self._discord.send_streamer_online(
                        self.alias, name, pri, "started"
                    )

                await asyncio.sleep(
                    random.uniform(
                        self.stagger_min, self.stagger_max
                    )
                )

            if desired:
                logger.info(
                    f"[{self.alias}] Активны: "
                    f"{sorted(desired)} "
                    f"({len(desired)}/{self.max_concurrent})"
                )


    async def _start_streamer(self, name: str):
        st = self.state.streamers[name]

        try:
            if not st.channel_id:
                utility = self._get_utility(name)
                st.channel_id = utility.get_channel_id(
                    self.token
                )
                if not st.channel_id:
                    raise RuntimeError(
                        f"Не удалось получить channel_id "
                        f"для {name}"
                    )

            ws_token_getter = self._get_ws_token_getter()
            ws_token = ws_token_getter.get_ws_token(name)
            if not ws_token:
                raise RuntimeError(
                    f"Не удалось получить WS-токен для {name} — "
                    f"скорее всего протух/неверен Bearer-токен "
                    f"аккаунта [{self.alias}]. "
                    f"Проверьте: python check_kick_token.py"
                )

            async def on_disconnect():
                st.is_watching = False
                st.cooldown_until = (
                    time.time() + self.reconnect_cooldown
                )
                logger.warning(
                    f"[{self.alias}] WS {name} "
                    f"окончательно отключился — "
                    f"повтор через {self.reconnect_cooldown}с"
                )

            ws_client = KickWebSocket(
                data={
                    "token": ws_token,
                    "streamId": st.stream_id or 0,
                    "channelId": st.channel_id,
                },
                proxy=self.proxy,
                on_disconnect=on_disconnect,
            )

            st.ws_client = ws_client
            st.is_watching = True
            st.error_count = 0

            st.ws_task = asyncio.create_task(
                self._ws_wrapper(name, ws_client)
            )
            st.points_task = asyncio.create_task(
                self._points_loop(name)
            )

            try:
                pts = self._get_points_checker().get_amount(
                    name, self.token
                )
                if pts is not None:
                    st.points = pts
                    st.last_points_update = datetime.now()
                    self._record_stats(name, pts, watching=True)
            except Exception:
                pass

            logger.success(
                f"[{self.alias}] ✅ Смотрим {name}"
            )

        except Exception as e:
            logger.error(
                f"[{self.alias}] Ошибка запуска {name}: {e}"
            )
            st.error_count += 1
            st.last_error = str(e)
            st.is_watching = False

            if self._discord:
                self._discord.send_error(
                    self.alias, name, str(e)
                )

    async def _stop_streamer(self, name: str):
        st = self.state.streamers[name]

        for task in (st.ws_task, st.points_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if st.ws_client:
            try:
                await st.ws_client.disconnect()
            except Exception:
                pass

        st.is_watching = False
        st.ws_client = None
        st.ws_task = None
        st.points_task = None
        logger.info(f"[{self.alias}] ⏹ {name} остановлен")

    async def _ws_wrapper(
        self, name: str, ws_client: KickWebSocket
    ):
        try:
            await ws_client.connect()
        except Exception as e:
            logger.error(
                f"[{self.alias}] WS {name} упал: {e}"
            )
        finally:
            self.state.streamers[name].is_watching = False

    async def _points_loop(self, name: str):
        st = self.state.streamers[name]
        checker = self._get_points_checker()

        while st.is_watching and self._running:
            try:
                await asyncio.sleep(random.uniform(120, 180))
                if not st.is_watching:
                    break

                amount = checker.get_amount(name, self.token)
                if amount is None:
                    continue

                old = st.points
                st.points = amount
                st.last_points_update = datetime.now()
                self._record_stats(name, amount, watching=True)

                if amount > old:
                    gain = amount - old
                    logger.success(
                        f"[{self.alias}] 💰 {name}: "
                        f"+{gain} (Всего: {amount})"
                    )

                    if self._discord:
                        self._discord.send_points_update(
                            self.alias, name, old, amount
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[{self.alias}] Ошибка поинтов "
                    f"{name}: {e}"
                )

    # ---------- Live-управление (Telegram) ----------

    async def pause(self):
        """Остановить фарм, но оставить воркер живым."""
        self.paused = True
        for name, s in list(self.state.streamers.items()):
            if s.is_watching:
                await self._stop_streamer(name)
        logger.info(f"[{self.alias}] ⏸ Пауза включена")

    async def resume(self):
        """Снять паузу и сразу перепроверить онлайны."""
        self.paused = False
        logger.info(f"[{self.alias}] ▶ Пауза снята")
        await self._check_all_online()
        await self._rebalance()

    async def add_streamer(self, name: str) -> str:
        """Добавить стримера на лету. Возвращает статус-строку."""
        name = (name or "").strip().lstrip("@")
        if not name:
            return "empty"
        if name in self.state.streamers:
            return "exists"
        prio = len(self.state.streamer_order)
        self.state.streamers[name] = StreamerState(
            name=name, priority=prio
        )
        self.state.streamer_order.append(name)
        try:
            utility = self._get_utility(name)
            stream_id = await asyncio.to_thread(
                utility.get_stream_id, self.token
            )
            st = self.state.streamers[name]
            st.is_online = stream_id is not None
            st.stream_id = stream_id
        except Exception as e:
            logger.warning(f"[{self.alias}] check {name}: {e}")
        await self._rebalance()
        return "added"

    async def remove_streamer(self, name: str) -> str:
        """Убрать стримера на лету. Возвращает статус-строку."""
        name = (name or "").strip().lstrip("@")
        if name not in self.state.streamers:
            return "missing"
        if self.state.streamers[name].is_watching:
            await self._stop_streamer(name)
        del self.state.streamers[name]
        self.state.streamer_order = [
            s for s in self.state.streamer_order if s != name
        ]
        for i, s in enumerate(self.state.streamer_order):
            self.state.streamers[s].priority = i
        util = self._utility_cache.pop(name, None)
        if util:
            try:
                util.close()
            except Exception:
                pass
        await self._rebalance()
        return "removed"

    async def set_max_concurrent(self, n: int) -> int:
        self.max_concurrent = max(1, min(int(n), 10))
        self.state.max_concurrent = self.max_concurrent
        await self._rebalance()
        return self.max_concurrent

    def set_token(self, new_token: str) -> str:
        """Заменить Bearer-токен и сбросить сессии.

        Активные WS-подключения продолжают работу на старом
        viewer-токене до переподключения; новый Bearer сразу
        используется для проверок онлайна/баланса/новых подключений.
        """
        self.token = self._clean_token(new_token)
        self.state.token = self.token
        if self._ws_token_getter:
            try:
                self._ws_token_getter.close()
            except Exception:
                pass
            self._ws_token_getter = None
        if self._points_checker:
            try:
                self._points_checker.close()
            except Exception:
                pass
            self._points_checker = None
        for u in self._utility_cache.values():
            try:
                u.close()
            except Exception:
                pass
        self._utility_cache.clear()
        logger.info(f"[{self.alias}] 🔑 Токен обновлён")
        return self.token

    def _record_stats(self, name: str, points: int,
                      watching: bool = False):
        if self._stats_db is None:
            return
        try:
            self._stats_db.record(
                self.alias, name, points, watching=watching
            )
        except Exception:
            pass

    async def move_streamer(self, name: str, pos: int) -> str:
        """Переставить стримера на позицию pos (0-based внутри).

        Возвращает 'moved' | 'missing'.
        """
        name = (name or "").strip().lstrip("@")
        if name not in self.state.streamers:
            return "missing"
        order = [s for s in self.state.streamer_order if s != name]
        pos = max(0, min(int(pos), len(order)))
        order.insert(pos, name)
        self.state.streamer_order = order
        for i, s in enumerate(order):
            self.state.streamers[s].priority = i
        await self._rebalance()
        return "moved"

    async def stop(self):
        self._running = False

        for name in list(self.state.streamers):
            if self.state.streamers[name].is_watching:
                await self._stop_streamer(name)

        if self._points_checker:
            self._points_checker.close()
            self._points_checker = None

        if self._ws_token_getter:
            self._ws_token_getter.close()
            self._ws_token_getter = None

        for u in self._utility_cache.values():
            u.close()
        self._utility_cache.clear()

        logger.info(f"[{self.alias}] Worker остановлен")

    def get_status(self) -> dict:
        return {
            "alias": self.alias,
            "proxy": bool(self.proxy),
            "paused": self.paused,
            "max_concurrent": self.max_concurrent,
            "active_count": self.state.active_count,
            "active_streamers": self.state.active_names,
            "streamer_order": self.state.streamer_order,
            "streamers": {
                name: {
                    "priority": s.priority,
                    "online": s.is_online,
                    "watching": s.is_watching,
                    "points": s.points,
                    "last_update": (
                        s.last_points_update.isoformat()
                        if s.last_points_update
                        else None
                    ),
                    "stream_id": s.stream_id,
                    "errors": s.error_count,
                    "last_error": s.last_error or "",
                    "cooldown_s": max(
                        0, int(s.cooldown_until - time.time())
                    ),
                }
                for name, s in self.state.streamers.items()
            },
        }


class AccountManager:
    def __init__(self, config: dict):
        self.workers: List[AccountWorker] = []
        self._tasks: List[asyncio.Task] = []
        self._discord: Optional["DiscordWebhook"] = None
        self._tg: Optional["TelegramBot"] = None
        self._token_bad: set = set()

        stats_path = config.get("Stats_db", "miner_stats.db")
        try:
            self.stats_db: Optional[StatsDB] = StatsDB(stats_path)
            logger.info(f"📊 Статистика поинтов: {stats_path}")
        except Exception as e:
            logger.warning(f"📊 StatsDB недоступна: {e}")
            self.stats_db = None

        proxy_cfg = config.get("Proxy", {})
        global_proxy = (
            proxy_cfg.get("url")
            if proxy_cfg.get("enabled") else None
        )

        check_interval = config.get("Check_interval", 120)
        reconnect_cooldown = config.get(
            "Reconnect_cooldown", 600
        )
        stagger_min = config.get("Connection_stagger_min", 3)
        stagger_max = config.get("Connection_stagger_max", 8)

        # Обратная совместимость
        accounts = config.get("Accounts", [])
        if not accounts:
            old_token = config.get(
                "Private", {}
            ).get("token", "")
            old_streamers = config.get("Streamers", [])
            old_max = config.get("Max_active_channels", 5)
            if old_token and old_streamers:
                accounts = [{
                    "alias": "Default",
                    "token": old_token,
                    "streamers": old_streamers,
                    "max_concurrent": old_max,
                }]
                logger.warning(
                    "⚠️ Старый формат конфига. "
                    "Переведите на Accounts[]."
                )

        for acc in accounts:
            self.workers.append(
                AccountWorker(
                    acc,
                    global_proxy=global_proxy,
                    check_interval=check_interval,
                    reconnect_cooldown=reconnect_cooldown,
                    stagger_min=stagger_min,
                    stagger_max=stagger_max,
                    stats_db=self.stats_db,
                )
            )

        logger.info(
            f"📊 Загружено аккаунтов: {len(self.workers)}, "
            f"глобальный прокси: "
            f"{'да' if global_proxy else 'нет'}"
        )

    def set_discord(self, discord: "DiscordWebhook"):
        """Подключить Discord webhook ко всем аккаунтам"""
        self._discord = discord
        for worker in self.workers:
            worker.set_discord(discord)
        logger.info(
            f"🟣 Discord webhook подключён к "
            f"{len(self.workers)} аккаунтам"
        )

    def set_telegram(self, bot: "TelegramBot"):
        """Подключить Telegram-бота для алертов сторожа токенов"""
        self._tg = bot

    def get_gains(self, hours: int = 24) -> Dict[str, Dict[str, int]]:
        if self.stats_db is None:
            return {}
        try:
            return self.stats_db.gains(hours)
        except Exception:
            return {}

    async def token_watchdog(self, interval_hours: float = 6):
        """Фоновый сторож: периодически проверяет Bearer-токены.

        При смерти токена — лог + Discord + Telegram (один раз,
        повторный алерт только после 'воскрешения' токена).
        """
        if interval_hours <= 0:
            return
        logger.info(
            f"🔑 Сторож токенов: проверка каждые {interval_hours}ч"
        )
        await self._check_tokens_once()
        while True:
            await asyncio.sleep(interval_hours * 3600)
            await self._check_tokens_once()

    async def _check_tokens_once(self):
        for worker in self.workers:
            try:
                valid, info = await asyncio.to_thread(
                    validate_kick_token, worker.token,
                    worker.proxy,
                )
            except Exception as e:
                logger.debug(f"🔑 Watchdog {worker.alias}: {e}")
                continue
            if valid:
                if worker.alias in self._token_bad:
                    self._token_bad.discard(worker.alias)
                    logger.success(
                        f"🔑 [{worker.alias}] токен снова валиден"
                    )
                continue
            if info != "invalid":
                logger.debug(
                    f"🔑 [{worker.alias}] проверка не удалась: {info}"
                )
                continue
            if worker.alias in self._token_bad:
                continue
            self._token_bad.add(worker.alias)
            logger.error(
                f"🔑 [{worker.alias}] ТОКЕН НЕВАЛИДЕН — фарм "
                f"остановится. Замените через /settoken"
            )
            if self._discord:
                try:
                    self._discord.send_token_alert(worker.alias)
                except Exception:
                    pass
            if self._tg is not None:
                try:
                    await self._tg.send_token_alert(worker.alias)
                except Exception:
                    pass

    async def start_all(self):
        for i, worker in enumerate(self.workers):
            if i > 0:
                delay = random.uniform(5, 15)
                logger.info(
                    f"⏳ Задержка {delay:.0f}с перед "
                    f"аккаунтом [{worker.alias}]"
                )
                await asyncio.sleep(delay)

            task = asyncio.create_task(worker.start())
            self._tasks.append(task)

        await asyncio.gather(
            *self._tasks, return_exceptions=True
        )

    async def stop_all(self):
        for task in self._tasks:
            task.cancel()
        for w in self.workers:
            await w.stop()
        if self.stats_db is not None:
            self.stats_db.close()
            self.stats_db = None

    def find_worker(self, ident: str) -> Optional[AccountWorker]:
        """Найти воркер по alias (без учёта регистра) или номеру с 1."""
        ident = (ident or "").strip()
        if not ident:
            return None
        if ident.startswith("#"):
            ident = ident[1:]
        if ident.isdigit():
            i = int(ident) - 1
            if 0 <= i < len(self.workers):
                return self.workers[i]
            return None
        low = ident.lower()
        for w in self.workers:
            if w.alias.lower() == low:
                return w
        return None

    def get_all_status(self) -> List[dict]:
        return [w.get_status() for w in self.workers]

    def get_all_streamers_flat(self) -> List[str]:
        seen = set()
        result = []
        for w in self.workers:
            for s in w.state.streamer_order:
                if s not in seen:
                    seen.add(s)
                    result.append(s)
        return result