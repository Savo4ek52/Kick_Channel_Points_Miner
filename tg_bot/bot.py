import os
import json
import asyncio
import re
import sys
import html
from datetime import datetime
from typing import Optional, TYPE_CHECKING, Tuple

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from loguru import logger

from utils.token_check import validate_kick_token as _shared_validate

if TYPE_CHECKING:
    from account_manager import AccountManager

try:
    from localization import t as loc_t

    def t(key, **kwargs):
        val = loc_t(key, **kwargs)
        return val if val else key
except ImportError:
    def t(key, **kwargs):
        return key


class TelegramBot:
    def __init__(self, config: dict):
        self.active = False
        self.application: Optional[Application] = None
        self.user_language: dict[int, str] = {}
        self.language_files: dict[str, dict] = {}
        self.config = config
        self.config_path = "config.json"
        self.account_manager: Optional["AccountManager"] = None
        # Многошаговый ввод из инлайн-меню:
        # user_id -> {"op": "addstreamer"|"settoken", "i": worker_idx}
        self.pending: dict[int, dict] = {}

        # Обратная совместимость
        self._legacy_streamers: list[str] = []
        self._legacy_points: dict = {}

        self.load_language_files()

    def set_account_manager(self, manager: "AccountManager"):
        self.account_manager = manager
        logger.info(
            f"TG Bot: подключён AccountManager "
            f"({len(manager.workers)} аккаунтов)"
        )

    def set_streamers(self, streamers: list[str]):
        self._legacy_streamers = streamers

    def set_points_data(self, streamer_name: str, points: int):
        now = datetime.now().strftime("%H:%M:%S")
        if streamer_name not in self._legacy_points:
            self._legacy_points[streamer_name] = {"history": []}
        self._legacy_points[streamer_name].update({
            "amount": points,
            "last_update": now,
        })
        history = self._legacy_points[streamer_name]["history"]
        history.append((now, points))
        if len(history) > 10:
            self._legacy_points[streamer_name]["history"] = (
                history[-10:]
            )

    async def start(self):
        tg_conf = self.config.get("Telegram", {})
        if not tg_conf.get("enabled", False):
            logger.info("Telegram bot disabled in config.")
            return

        token = tg_conf.get("bot_token", "")
        if not token:
            logger.error("Telegram token not found!")
            return

        try:
            self.application = (
                Application.builder().token(token).build()
            )

            handlers = [
                CommandHandler("start", self.cmd_start),
                CommandHandler("status", self.cmd_status),
                CommandHandler("balance", self.cmd_balance),
                CommandHandler("accounts", self.cmd_accounts),
                CommandHandler("account", self.cmd_account),
                CommandHandler("errors", self.cmd_errors),
                CommandHandler("addstreamer", self.cmd_addstreamer),
                CommandHandler("delstreamer", self.cmd_delstreamer),
                CommandHandler("setlimit", self.cmd_setlimit),
                CommandHandler("settoken", self.cmd_settoken),
                CommandHandler("pause", self.cmd_pause),
                CommandHandler("resume", self.cmd_resume),
                CommandHandler("checktokens", self.cmd_checktokens),
                CommandHandler("stats", self.cmd_stats),
                CommandHandler("move", self.cmd_move),
                CommandHandler("logs", self.cmd_logs),
                CommandHandler("menu", self.cmd_menu),
                CommandHandler("cancel", self.cmd_cancel),
                CallbackQueryHandler(self.on_callback),
                CommandHandler("restart", self.cmd_restart),
                CommandHandler("help", self.cmd_help),
                CommandHandler("language", self.cmd_language),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    self.handle_message,
                ),
            ]

            for h in handlers:
                self.application.add_handler(h)

            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(
                drop_pending_updates=True
            )

            self.active = True
            logger.success("✅ Telegram bot initialized")
            await self._send_startup()

        except Exception as e:
            logger.error(f"❌ Telegram init failed: {e}")
            self.active = False

    async def stop(self):
        if self.application:
            try:
                if self.application.updater.running:
                    await self.application.updater.stop()
                if self.application.running:
                    await self.application.stop()
                await self.application.shutdown()
                self.active = False
            except Exception as e:
                logger.error(f"Error stopping TG bot: {e}")

    def load_language_files(self):
        lang_dir = "tg_bot/lang"
        os.makedirs(lang_dir, exist_ok=True)
        for lang in ("en", "ru"):
            path = os.path.join(lang_dir, f"{lang}.lang")
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        self.language_files[lang] = json.load(f)
                else:
                    self.language_files[lang] = {}
            except Exception as e:
                logger.error(f"Error loading lang {lang}: {e}")
                self.language_files[lang] = {}

    def get_text(self, key, lang="en", **kwargs):
        d = self.language_files.get(
            lang.lower(),
            self.language_files.get("en", {}),
        )
        text = d.get(key, f"🔑 {key}")
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text

    def _lang(self, uid: int) -> str:
        return self.user_language.get(
            uid, self.config.get("Language", "en")
        )

    def get_keyboard(self, lang="en", is_admin=False):
        d = self.language_files.get(
            lang, self.language_files.get("en", {})
        )
        btn_stat = d.get("btn_status", "📊 Status")
        btn_bal = d.get("btn_balance", "💰 Balance")
        btn_help = d.get("btn_help", "❓ Help")
        btn_acc = d.get("btn_accounts", "👥 Accounts")
        btn_menu = d.get("btn_menu", "🎛 Menu")

        keyboard = [[btn_stat, btn_bal], [btn_acc, btn_menu]]

        if is_admin:
            btn_restart = d.get("btn_restart", "🔄 Restart")
            keyboard.append([btn_help, btn_restart])
        else:
            keyboard.append([btn_help])

        return ReplyKeyboardMarkup(
            keyboard, resize_keyboard=True
        )

    def is_user_allowed(self, user_id: int) -> bool:
        conf = self.config.get("Telegram", {})
        allowed = conf.get("allowed_users", [])
        owner = conf.get("chat_id")
        ids = {str(u) for u in allowed}
        if owner:
            ids.add(str(owner))
        return bool(ids) and str(user_id) in ids

    def is_admin(self, user_id: int) -> bool:
        owner = self.config.get("Telegram", {}).get("chat_id")
        return str(user_id) == str(owner)

    # ---------- Управление аккаунтами ----------

    async def _admin_guard(self, update: Update) -> Optional[Tuple[int, str]]:
        """Проверка прав админа. Возвращает (uid, lang) или None."""
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return None
        lang = self._lang(uid)
        if not self.is_admin(uid):
            await update.message.reply_text(
                self.get_text("not_enough_permissions", lang),
                parse_mode=ParseMode.HTML,
            )
            return None
        if not self.account_manager:
            await update.message.reply_text("No AccountManager")
            return None
        return uid, lang

    def _resolve_worker(self, args, trailing: int = 0):
        """Найти воркер по alias (может содержать пробелы) или номеру.

        trailing — сколько последних аргументов НЕ входят в alias
        (например, имя стримера или число лимита).
        Возвращает (worker|None, остаток_аргументов).
        """
        if not args or len(args) <= trailing or not self.account_manager:
            return None, []
        if trailing:
            alias = " ".join(args[:-trailing])
            rest = list(args[-trailing:])
        else:
            alias = " ".join(args)
            rest = []
        return self.account_manager.find_worker(alias), rest

    def _available_aliases(self) -> str:
        if not self.account_manager:
            return "—"
        return ", ".join(
            f"#{i + 1} {w.alias}"
            for i, w in enumerate(self.account_manager.workers)
        )

    @staticmethod
    def _mask_token(token: str) -> str:
        t = (token or "").strip()
        if len(t) <= 10:
            return "***"
        return f"{t[:4]}...{t[-4:]}"

    def _sync_workers_to_config(self):
        """Перенести живое состояние воркеров в self.config."""
        accounts = self.config.get("Accounts")
        if not accounts or not self.account_manager:
            return
        by_alias = {a.get("alias"): a for a in accounts}
        for w in self.account_manager.workers:
            entry = by_alias.get(w.alias)
            if entry is None:
                continue
            entry["streamers"] = list(w.state.streamer_order)
            entry["max_concurrent"] = w.max_concurrent
            entry["token"] = w.token
            if w.paused:
                entry["disabled"] = True
            else:
                entry.pop("disabled", None)

    def _save_config(self) -> Optional[str]:
        """Сохранить config.json. Возвращает None или текст ошибки."""
        try:
            self._sync_workers_to_config()
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return None
        except Exception as e:
            logger.error(f"TG: не удалось сохранить config: {e}")
            return str(e)

    @staticmethod
    def _validate_kick_token(token: str) -> Tuple[bool, str]:
        """Проверить Bearer-токен через приватный endpoint Kick.

        Возвращает (valid, info): info = 'ok' | 'invalid' | 'error ...'.
        Синхронная — вызывать через asyncio.to_thread.
        """
        return _shared_validate(token)

    @staticmethod
    def _valid_streamer_name(name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_]{1,25}", name or ""))

    def _build_status_text(self, lang: str) -> str:
        if self.account_manager:
            return self._build_multi_status(lang)
        return self._build_legacy_status(lang)

    def _build_multi_status(self, lang: str) -> str:
        lines = ["<b>📊 Multi-Account Status</b>\n"]
        now = datetime.now().strftime("%H:%M:%S")

        for worker in self.account_manager.workers:
            st = worker.get_status()
            alias = html.escape(st["alias"])
            active = st["active_count"]
            limit = st["max_concurrent"]
            proxy = "🔒" if st["proxy"] else "🌐"

            lines.append(
                f"{proxy} <b>{alias}</b> [{active}/{limit}]"
            )

            for name in worker.state.streamer_order:
                info = st["streamers"].get(name, {})
                pri = info.get("priority", "?")
                online = info.get("online", False)
                watching = info.get("watching", False)
                pts = info.get("points", 0)
                errors = info.get("errors", 0)

                if watching:
                    icon = "👁"
                elif online:
                    icon = "🟢"
                else:
                    icon = "⚫"

                err_str = f" ⚠️x{errors}" if errors else ""
                name_esc = html.escape(name)

                lines.append(
                    f"  {icon} #{pri} "
                    f"<code>{name_esc}</code> "
                    f"— {pts} pts{err_str}"
                )
            lines.append("")

        lines.append(f"🕐 {now}")
        return "\n".join(lines)

    def _build_legacy_status(self, lang: str) -> str:
        if not self._legacy_streamers:
            return self.get_text("status_inactive", lang)
        sl = "\n".join([
            f"• <code>{html.escape(str(s))}</code>"
            for s in self._legacy_streamers
        ])
        now = datetime.now().strftime("%H:%M:%S")
        return self.get_text(
            "status_active", lang,
            streamers=sl, last_update=now, rate="120",
        )

    def _build_balance_text(self, lang: str) -> str:
        if self.account_manager:
            return self._build_multi_balance(lang)
        return self._build_legacy_balance(lang)

    def _build_multi_balance(self, lang: str) -> str:
        lines = ["<b>💰 Points by Account</b>\n"]
        total_all = 0

        for worker in self.account_manager.workers:
            st = worker.get_status()
            alias = html.escape(st["alias"])
            lines.append(f"<b>{alias}</b>:")
            acc_total = 0

            for name in worker.state.streamer_order:
                info = st["streamers"].get(name, {})
                pts = info.get("points", 0)
                last = info.get("last_update")
                watching = info.get("watching", False)
                acc_total += pts

                ts = (
                    last.split("T")[1][:8] if last else "N/A"
                )
                icon = "👁" if watching else "  "
                lines.append(
                    f"  {icon} <code>{html.escape(name)}</code>"
                    f": <b>{pts}</b> ({ts})"
                )

            total_all += acc_total
            lines.append(
                f"  📊 Subtotal: <b>{acc_total}</b>\n"
            )

        lines.append(f"🏆 <b>Total: {total_all}</b>")
        return "\n".join(lines)

    def _build_legacy_balance(self, lang: str) -> str:
        if not self._legacy_streamers:
            return self.get_text("balance_no_streamers", lang)
        msgs = []
        for s in self._legacy_streamers:
            data = self._legacy_points.get(
                s, {"amount": 0, "last_update": "N/A"}
            )
            msgs.append(self.get_text(
                "balance_info", lang,
                streamer=html.escape(str(s)),
                amount=data["amount"],
                time=data["last_update"],
            ))
        text = "\n\n".join(msgs)
        return text[:4000] if len(text) <= 4000 else text[:4000] + "..."

    def _build_accounts_text(self, lang: str = "en") -> str:
        if not self.account_manager:
            return "No AccountManager"

        lines = [
            f"<b>{html.escape(self.get_text('ov_title', lang))}</b>\n"
        ]

        for i, worker in enumerate(
            self.account_manager.workers
        ):
            st = worker.get_status()
            alias = html.escape(st["alias"])
            proxy = "🔒 proxy" if st["proxy"] else "🌐 direct"
            paused = (
                f" {self.get_text('ov_paused', lang)}"
                if st.get("paused") else ""
            )
            active = st["active_count"]
            limit = st["max_concurrent"]
            total = len(st["streamers"])
            online = sum(
                1 for s in st["streamers"].values()
                if s.get("online")
            )

            order = " > ".join(
                html.escape(s) for s in worker.state.streamer_order
            ) or "—"

            lines.append(
                f"<b>#{i + 1} {alias}</b>{paused}\n"
                f"  {proxy}\n"
                f"  {self.get_text('ov_streamers', lang)}: "
                f"{total} ({self.get_text('ov_online', lang)}: "
                f"{online})\n"
                f"  {self.get_text('ov_active', lang)}: "
                f"{active}/{limit}\n"
                f"  {self.get_text('ov_priority', lang)}: "
                f"{order}\n"
            )

        return "\n".join(lines)

    def _build_account_card(self, worker, num: int, lang: str) -> str:
        st = worker.get_status()
        alias = html.escape(st["alias"])
        proxy = "🔒 proxy" if st["proxy"] else "🌐 direct"
        state = (
            self.get_text("ov_paused", lang)
            if st.get("paused")
            else self.get_text("ov_running", lang)
        )
        token_ok = (
            "✅"
            if worker.token and "|" in worker.token
            else "⚠️"
        )
        lines = [
            f"<b>👤 #{num} {alias}</b> — {state}",
            f"  {proxy} | {self.get_text('ov_active', lang)}: "
            f"{st['active_count']}/{st['max_concurrent']}",
            f"  🔑 <code>{self._mask_token(worker.token)}</code> "
            f"{token_ok}",
            "",
        ]
        if not worker.state.streamer_order:
            lines.append(self.get_text("card_empty", lang))
        for name in worker.state.streamer_order:
            info = st["streamers"].get(name, {})
            pri = info.get("priority", "?")
            watching = info.get("watching", False)
            online = info.get("online", False)
            pts = info.get("points", 0)
            errors = info.get("errors", 0)
            icon = "👁" if watching else ("🟢" if online else "⚫")
            err = f" ⚠️x{errors}" if errors else ""
            lines.append(
                f"  {icon} #{pri} <code>{html.escape(name)}</code>"
                f" — {pts} pts{err}"
            )
            last_err = info.get("last_error", "")
            if last_err:
                lines.append(
                    f"      └ <code>{html.escape(last_err[:120])}</code>"
                )
        return "\n".join(lines)

    async def cmd_start(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        lang = self._lang(uid)
        self.user_language[uid] = lang
        await update.message.reply_text(
            self.get_text("start_message", lang),
            reply_markup=self.get_keyboard(
                lang, self.is_admin(uid)
            ),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_status(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        text = self._build_status_text(self._lang(uid))
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    async def cmd_balance(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        text = self._build_balance_text(self._lang(uid))
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await update.message.reply_text(
                    text[i:i + 4000],
                    parse_mode=ParseMode.HTML,
                )
        else:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML
            )

    async def cmd_accounts(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        await update.message.reply_text(
            self._build_accounts_text(self._lang(uid)),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_account(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid) or not self.account_manager:
            return
        lang = self._lang(uid)
        worker, _ = self._resolve_worker(context.args or [])
        if not worker:
            await update.message.reply_text(
                self.get_text(
                    "account_not_found", lang,
                    ident=html.escape(
                        " ".join(context.args or [])
                    ),
                    available=html.escape(
                        self._available_aliases()
                    ),
                )
                + "\n\n"
                + self.get_text(
                    "usage", lang,
                    cmd="/account &lt;alias|номер&gt;",
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        num = self.account_manager.workers.index(worker) + 1
        await update.message.reply_text(
            self._build_account_card(worker, num, lang),
            parse_mode=ParseMode.HTML,
        )

    def _build_errors_text(self, lang: str) -> str:
        lines = []
        for worker in self.account_manager.workers:
            st = worker.get_status()
            bad = [
                (name, info)
                for name, info in st["streamers"].items()
                if info.get("errors")
            ]
            if not bad:
                continue
            lines.append(f"<b>{html.escape(st['alias'])}</b>:")
            for name, info in bad:
                lines.append(
                    f"  ⚠️ <code>{html.escape(name)}</code> "
                    f"x{info['errors']}"
                )
                if info.get("last_error"):
                    lines.append(
                        f"      └ <code>"
                        f"{html.escape(info['last_error'][:150])}"
                        f"</code>"
                    )
        return (
            "\n".join(lines)
            if lines else self.get_text("no_errors", lang)
        )

    async def cmd_errors(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid) or not self.account_manager:
            return
        await update.message.reply_text(
            self._build_errors_text(self._lang(uid)),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_addstreamer(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, rest = self._resolve_worker(
            context.args or [], trailing=1
        )
        if not worker or not rest:
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/addstreamer &lt;alias|номер&gt; &lt;streamer&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        name = rest[0].strip().lstrip("@")
        if not self._valid_streamer_name(name):
            await update.message.reply_text(
                self.get_text(
                    "bad_streamer_name", lang, streamer=html.escape(name)
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        result = await worker.add_streamer(name)
        if result == "exists":
            await update.message.reply_text(
                self.get_text(
                    "streamer_exists", lang,
                    streamer=html.escape(name),
                    alias=html.escape(worker.alias),
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        err = self._save_config()
        st = worker.state.streamers[name]
        text = self.get_text(
            "streamer_added", lang,
            streamer=html.escape(name),
            alias=html.escape(worker.alias),
            prio=st.priority,
            online="🟢" if st.is_online else "⚫",
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    async def cmd_delstreamer(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, rest = self._resolve_worker(
            context.args or [], trailing=1
        )
        if not worker or not rest:
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/delstreamer &lt;alias|номер&gt; &lt;streamer&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        name = rest[0].strip().lstrip("@")
        result = await worker.remove_streamer(name)
        if result == "missing":
            await update.message.reply_text(
                self.get_text(
                    "streamer_missing", lang,
                    streamer=html.escape(name),
                    alias=html.escape(worker.alias),
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        err = self._save_config()
        text = self.get_text(
            "streamer_removed", lang,
            streamer=html.escape(name),
            alias=html.escape(worker.alias),
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    async def cmd_setlimit(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, rest = self._resolve_worker(
            context.args or [], trailing=1
        )
        if not worker or not rest or not rest[0].isdigit():
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/setlimit &lt;alias|номер&gt; &lt;1-10&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        n = max(1, min(int(rest[0]), 10))
        await worker.set_max_concurrent(n)
        err = self._save_config()
        text = self.get_text(
            "limit_set", lang,
            alias=html.escape(worker.alias), n=n,
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    async def cmd_settoken(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, rest = self._resolve_worker(
            context.args or [], trailing=1
        )
        if not worker or not rest:
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/settoken &lt;alias|номер&gt; &lt;токен_без_Bearer&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            await update.message.delete()
        except Exception:
            pass
        new_token = rest[0].strip()
        wait_msg = await update.message.reply_text(
            self.get_text("token_checking", lang),
            parse_mode=ParseMode.HTML,
        )
        valid, info = await asyncio.to_thread(
            self._validate_kick_token, new_token
        )
        worker.set_token(new_token)
        err = self._save_config()
        if valid:
            check = self.get_text("token_check_ok", lang)
        elif info == "invalid":
            check = self.get_text("token_check_bad", lang)
        else:
            check = self.get_text(
                "token_check_err", lang,
                info=html.escape(info),
            )
        text = self.get_text(
            "token_updated", lang,
            alias=html.escape(worker.alias),
            mask=self._mask_token(worker.token),
            check=check,
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        try:
            await wait_msg.edit_text(
                text, parse_mode=ParseMode.HTML
            )
        except Exception:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML
            )

    async def cmd_pause(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, _ = self._resolve_worker(context.args or [])
        if not worker:
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/pause &lt;alias|номер&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        await worker.pause()
        err = self._save_config()
        text = self.get_text(
            "paused", lang, alias=html.escape(worker.alias)
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    async def cmd_resume(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, _ = self._resolve_worker(context.args or [])
        if not worker:
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/resume &lt;alias|номер&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        await worker.resume()
        err = self._save_config()
        text = self.get_text(
            "resumed", lang, alias=html.escape(worker.alias)
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    async def run_tokens_check(self) -> list:
        """Проверить токены всех воркеров.

        Возвращает [(worker, valid, info), ...].
        """
        results = []
        for worker in self.account_manager.workers:
            valid, info = await asyncio.to_thread(
                self._validate_kick_token, worker.token
            )
            results.append((worker, valid, info))
        return results

    def _format_tokens_report(self, lang: str, results: list) -> str:
        lines = [
            f"<b>{html.escape(self.get_text('tokens_title', lang))}</b>\n"
        ]
        for worker, valid, info in results:
            if valid:
                mark = self.get_text("token_check_ok", lang)
            elif info == "invalid":
                mark = self.get_text("token_check_bad", lang)
            else:
                mark = self.get_text(
                    "token_check_err", lang,
                    info=html.escape(info),
                )
            lines.append(
                f"👤 <b>{html.escape(worker.alias)}</b> "
                f"<code>{self._mask_token(worker.token)}</code>\n"
                f"   └ {mark}"
            )
        return "\n".join(lines)

    async def cmd_checktokens(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        wait_msg = await update.message.reply_text(
            self.get_text("token_checking", lang),
            parse_mode=ParseMode.HTML,
        )
        results = await self.run_tokens_check()
        text = self._format_tokens_report(lang, results)
        try:
            await wait_msg.edit_text(
                text, parse_mode=ParseMode.HTML
            )
        except Exception:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML
            )

    def _build_stats_text(self, lang: str, hours: int,
                            gains: dict) -> str:
        if not gains or not any(gains.values()):
            return self.get_text("stats_empty", lang, hours=hours)
        lines = [self.get_text(
            "stats_title", lang, hours=hours
        ) + "\n"]
        grand = 0
        for worker in self.account_manager.workers:
            ag = gains.get(worker.alias, {})
            sub = sum(ag.values())
            grand += sub
            lines.append(
                f"<b>{html.escape(worker.alias)}</b> "
                f"(+{sub}):"
            )
            for name in worker.state.streamer_order:
                g = ag.get(name, 0)
                info = worker.get_status()["streamers"].get(name, {})
                icon = "👁" if info.get("watching") else "·"
                lines.append(
                    f"  {icon} <code>{html.escape(name)}</code>"
                    f": +{g}"
                )
            lines.append("")
        lines.append(self.get_text(
            "stats_total", lang, total=grand
        ))
        return "\n".join(lines)

    async def cmd_stats(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid) or not self.account_manager:
            return
        lang = self._lang(uid)
        hours = 24
        if context.args:
            try:
                hours = max(1, min(int(context.args[0]), 720))
            except ValueError:
                pass
        gains = await asyncio.to_thread(
            self.account_manager.get_gains, hours
        )
        await update.message.reply_text(
            self._build_stats_text(lang, hours, gains),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_move(self, update: Update, context):
        guard = await self._admin_guard(update)
        if not guard:
            return
        _, lang = guard
        worker, rest = self._resolve_worker(
            context.args or [], trailing=2
        )
        if (not worker or len(rest) != 2
                or not rest[1].isdigit()):
            await update.message.reply_text(
                self.get_text(
                    "usage", lang,
                    cmd="/move &lt;alias|номер&gt; "
                        "&lt;streamer&gt; &lt;позиция_с_1&gt;",
                )
                + "\n"
                + html.escape(self._available_aliases()),
                parse_mode=ParseMode.HTML,
            )
            return
        name, pos = rest[0].strip(), int(rest[1])
        result = await worker.move_streamer(name, pos - 1)
        if result == "missing":
            await update.message.reply_text(
                self.get_text(
                    "streamer_missing", lang,
                    streamer=html.escape(name),
                    alias=html.escape(worker.alias),
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        err = self._save_config()
        real_pos = worker.state.streamer_order.index(
            name.strip().lstrip("@")
        ) + 1
        text = self.get_text(
            "moved", lang,
            streamer=html.escape(name.strip().lstrip("@")),
            alias=html.escape(worker.alias),
            pos=real_pos,
        )
        if err:
            text += "\n" + self.get_text(
                "cfg_save_failed", lang,
                error=html.escape(err),
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML
        )

    def _read_log_tail(self, lang: str, n: int) -> str:
        log_path = self.config.get("Log_file", "miner.log")
        if not log_path:
            return self.get_text("logs_disabled", lang)
        try:
            with open(log_path, "r", encoding="utf-8",
                      errors="ignore") as f:
                lines = f.readlines()[-n:]
        except FileNotFoundError:
            return self.get_text("logs_empty", lang)
        except Exception as e:
            return html.escape(str(e))
        if not lines:
            return self.get_text("logs_empty", lang)
        body = html.escape("".join(lines).strip())
        text = f"<pre>{body}</pre>"
        if len(text) > 4000:
            text = f"<pre>{body[-3800:]}</pre>"
        return text

    async def cmd_logs(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        lang = self._lang(uid)
        n = 30
        if context.args:
            try:
                n = max(5, min(int(context.args[0]), 100))
            except ValueError:
                pass
        await update.message.reply_text(
            self._read_log_tail(lang, n),
            parse_mode=ParseMode.HTML,
        )

    def worker_by_index(self, idx: int):
        if not self.account_manager:
            return None
        workers = self.account_manager.workers
        if 0 <= idx < len(workers):
            return workers[idx]
        return None

    async def cmd_menu(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid) or not self.account_manager:
            return
        from tg_bot.menu import main_menu
        lang = self._lang(uid)
        text, markup = main_menu(self, lang, self.is_admin(uid))
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )

    async def cmd_cancel(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        lang = self._lang(uid)
        if uid in self.pending:
            del self.pending[uid]
            await update.message.reply_text(
                self.get_text("menu_cancelled", lang),
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                self.get_text("menu_nothing_to_cancel", lang),
                parse_mode=ParseMode.HTML,
            )

    async def on_callback(self, update: Update, context):
        from tg_bot.menu import on_callback
        await on_callback(self, update, context)

    async def cmd_restart(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        lang = self._lang(uid)
        if not self.is_admin(uid):
            await update.message.reply_text(
                self.get_text(
                    "not_enough_permissions", lang
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        await update.message.reply_text(
            self.get_text("restart_confirmation", lang),
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(1)
        logger.info("Restart requested via Telegram")
        sys.exit(1)

    async def cmd_help(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        lang = self._lang(uid)
        key = (
            "help_full_admin" if self.is_admin(uid)
            else "help_full"
        )
        await update.message.reply_text(
            self.get_text(key, lang),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_language(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        if not self.is_admin(uid):
            lang = self._lang(uid)
            await update.message.reply_text(
                self.get_text(
                    "not_enough_permissions", lang
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /language [en/ru]"
            )
            return
        code = context.args[0].lower()
        if code in ("en", "ru"):
            self.user_language[uid] = code
            await update.message.reply_text(
                self.get_text(
                    "language_changed", code,
                    language=code.upper(),
                ),
                reply_markup=self.get_keyboard(
                    code, is_admin=True
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "Supported: en, ru"
            )

    async def handle_message(self, update: Update, context):
        uid = update.effective_user.id
        if not self.is_user_allowed(uid):
            return
        text = update.message.text
        lang = self._lang(uid)
        d = self.language_files.get(
            lang, self.language_files.get("en", {})
        )
        # Многошаговый ввод из инлайн-меню — в приоритете
        if uid in self.pending:
            from tg_bot.menu import handle_pending_text
            await handle_pending_text(self, update, context)
            return
        btn_map = {
            d.get("btn_status", "📊 Status"): self.cmd_status,
            d.get("btn_balance", "💰 Balance"): self.cmd_balance,
            d.get("btn_help", "❓ Help"): self.cmd_help,
            d.get("btn_restart", "🔄 Restart"): self.cmd_restart,
            d.get("btn_accounts", "👥 Accounts"): self.cmd_accounts,
            d.get("btn_menu", "🎛 Menu"): self.cmd_menu,
        }
        handler = btn_map.get(text)
        if handler:
            await handler(update, context)

    async def _send_startup(self):
        if not self.active:
            return
        owner = self.config.get("Telegram", {}).get("chat_id")
        if not owner:
            return

        if self.account_manager:
            text = "🚀 <b>Miner Started!</b>\n\n"
            try:
                lang = self._lang(int(owner))
            except (TypeError, ValueError):
                lang = self.config.get("Language", "en")
            text += self._build_accounts_text(lang)
        else:
            sl = "\n".join([
                f"• <code>{html.escape(str(s))}</code>"
                for s in self._legacy_streamers
            ]) if self._legacy_streamers else "None"
            lang = self._lang(int(owner))
            text = self.get_text(
                "startup_notification", lang, streamers=sl
            )

        await self._send(owner, text)

    async def send_points_update(
        self, streamer, old_amount, new_amount,
        account_alias="",
    ):
        if not self.active:
            return
        gain = new_amount - old_amount
        if gain <= 0:
            return
        conf = self.config.get("Telegram", {})
        recipients = set(conf.get("allowed_users", []))
        owner = conf.get("chat_id")
        if owner:
            recipients.add(owner)
        prefix = (
            f"[{html.escape(account_alias)}] "
            if account_alias else ""
        )
        for uid in recipients:
            await self._send(
                uid,
                f"{prefix}💰 <b>{html.escape(streamer)}</b>"
                f": +{gain} (Total: {new_amount})",
            )

    async def send_alert(self, streamers):
        if not self.active:
            return
        owner = self.config.get("Telegram", {}).get("chat_id")
        if owner:
            names = ", ".join(html.escape(s) for s in streamers)
            await self._send(
                owner, f"⚠️ Нет начислений: {names}"
            )

    async def send_token_alert(self, alias: str):
        """Алерт сторожа: Bearer-токен аккаунта умер."""
        if not self.active:
            return
        conf = self.config.get("Telegram", {})
        recipients = set(conf.get("allowed_users", []))
        owner = conf.get("chat_id")
        if owner:
            recipients.add(owner)
        for uid in recipients:
            try:
                lang = self._lang(int(uid))
            except (TypeError, ValueError):
                lang = self.config.get("Language", "en")
            await self._send(
                uid,
                self.get_text(
                    "token_alert", lang,
                    alias=html.escape(alias),
                ),
            )

    async def send_restart_notification(self):
        if not self.active:
            return
        owner = self.config.get("Telegram", {}).get("chat_id")
        if owner:
            await self._send(owner, "🔄 Перезапуск...")

    async def send_streamer_started(self, streamer):
        pass

    async def send_streamer_error(self, streamer, error):
        if not self.active:
            return
        owner = self.config.get("Telegram", {}).get("chat_id")
        if not owner:
            return
        safe = html.escape(str(error)[:300])
        lang = self._lang(int(owner))
        await self._send(
            owner,
            self.get_text(
                "streamer_error", lang,
                streamer=streamer, error=safe,
            ),
        )

    async def _send(self, user_id, text):
        try:
            if self.application:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.error(f"TG send to {user_id} failed: {e}")