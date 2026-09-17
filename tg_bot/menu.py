"""Инлайн-меню Telegram-бота: управление майнером с телефона.

Навигация через callback_data вида ``m:<action>[:args...]``.
Лимит Telegram на callback_data — 64 байта, поэтому аккаунты
адресуются ИНДЕКСОМ воркера, а стримеры — индексом в порядке
приоритета (alias с пробелами не ломают навигацию).

Все мутирующие действия доступны только админу (chat_id из конфига).
"""

import asyncio
import html
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode


def _t(bot, key: str, lang: str, **kwargs) -> str:
    return bot.get_text(key, lang, **kwargs)


def _btn(bot, key: str, lang: str, cb: str, **kwargs):
    return InlineKeyboardButton(
        _t(bot, key, lang, **kwargs), callback_data=cb
    )


def _back_home(bot, lang: str, back_to: str = "m:home"):
    return [
        [
            InlineKeyboardButton(
                _t(bot, "menu_back", lang), callback_data=back_to
            ),
            InlineKeyboardButton(
                _t(bot, "menu_home", lang), callback_data="m:home"
            ),
        ]
    ]


async def _edit(query, text: str, markup=None):
    text = (text or "")[:4000]
    try:
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await query.message.reply_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


# ---------- Экраны ----------

def main_menu(bot, lang: str, is_admin: bool):
    rows = [
        [
            _btn(bot, "menu_status", lang, "m:status"),
            _btn(bot, "menu_balance", lang, "m:balance"),
        ],
        [
            _btn(bot, "menu_accounts", lang, "m:accounts"),
            _btn(bot, "menu_stats", lang, "m:stats"),
        ],
        [
            _btn(bot, "menu_errors", lang, "m:errors"),
            _btn(bot, "menu_logs", lang, "m:logs"),
        ],
    ]
    if is_admin:
        rows.append([
            _btn(bot, "menu_tokens", lang, "m:chktokens"),
            _btn(bot, "menu_restart", lang, "m:restart?"),
        ])
    return _t(bot, "menu_title", lang), InlineKeyboardMarkup(rows)


def accounts_menu(bot, lang: str):
    rows = []
    for i, w in enumerate(bot.account_manager.workers):
        st = w.get_status()
        icon = "⏸" if st.get("paused") else "▶"
        rows.append([InlineKeyboardButton(
            f"{icon} {w.alias} [{st['active_count']}/"
            f"{st['max_concurrent']}]",
            callback_data=f"m:acc:{i}",
        )])
    rows.extend(_back_home(bot, lang))
    return (
        _t(bot, "menu_accounts_title", lang),
        InlineKeyboardMarkup(rows),
    )


def account_menu(bot, worker, idx: int, lang: str):
    num = bot.account_manager.workers.index(worker) + 1
    text = bot._build_account_card(worker, num, lang)
    paused = worker.get_status().get("paused")
    toggle = (
        _btn(bot, "menu_resume", lang, f"m:resume:{idx}")
        if paused else
        _btn(bot, "menu_pause", lang, f"m:pause:{idx}")
    )
    rows = [
        [toggle, _btn(bot, "menu_chktoken", lang, f"m:chk:{idx}")],
        [
            _btn(bot, "menu_addst", lang, f"m:addst:{idx}"),
            _btn(bot, "menu_delst", lang, f"m:dellist:{idx}"),
        ],
        [
            _btn(bot, "menu_move", lang, f"m:mvlist:{idx}"),
            _btn(bot, "menu_limit", lang, f"m:lim:{idx}"),
        ],
        [_btn(bot, "menu_settoken", lang, f"m:tok:{idx}")],
    ]
    rows.extend(_back_home(bot, lang, back_to="m:accounts"))
    return text, InlineKeyboardMarkup(rows)


def limit_menu(bot, worker, idx: int, lang: str):
    cur = worker.max_concurrent
    rows = [[
        InlineKeyboardButton(
            f"{'✅ ' if n == cur else ''}{n}",
            callback_data=f"m:setlim:{idx}:{n}",
        )
        for n in (1, 2, 3, 4, 5)
    ]]
    rows.extend(_back_home(bot, lang, back_to=f"m:acc:{idx}"))
    return (
        _t(bot, "menu_limit_title", lang,
           alias=html.escape(worker.alias), cur=cur),
        InlineKeyboardMarkup(rows),
    )


def _streamer_list_menu(bot, worker, idx: int, lang: str,
                        title_key: str, action: str):
    rows = []
    for s, name in enumerate(worker.state.streamer_order):
        info = worker.get_status()["streamers"].get(name, {})
        icon = (
            "👁" if info.get("watching")
            else ("🟢" if info.get("online") else "⚫")
        )
        rows.append([InlineKeyboardButton(
            f"{icon} #{s + 1} {name}",
            callback_data=f"m:{action}:{idx}:{s}",
        )])
    rows.extend(_back_home(bot, lang, back_to=f"m:acc:{idx}"))
    return (
        _t(bot, title_key, lang,
           alias=html.escape(worker.alias)),
        InlineKeyboardMarkup(rows),
    )


def move_pos_menu(bot, worker, idx: int, s: int, lang: str):
    order = worker.state.streamer_order
    name = order[s]
    rows = [[
        InlineKeyboardButton(
            f"{n + 1}",
            callback_data=f"m:mvto:{idx}:{s}:{n}",
        )
        for n in range(len(order))
    ]]
    rows.extend(_back_home(bot, lang, back_to=f"m:mvlist:{idx}"))
    return (
        _t(bot, "menu_pick_pos", lang,
           streamer=html.escape(name)),
        InlineKeyboardMarkup(rows),
    )


def stats_menu(bot, lang: str):
    rows = [[
        _btn(bot, "menu_24h", lang, "m:stats:24"),
        _btn(bot, "menu_7d", lang, "m:stats:168"),
    ]]
    rows.extend(_back_home(bot, lang))
    return (
        _t(bot, "menu_period", lang),
        InlineKeyboardMarkup(rows),
    )


def restart_confirm(bot, lang: str):
    rows = [[
        _btn(bot, "menu_yes", lang, "m:restart!"),
        _btn(bot, "menu_no", lang, "m:home"),
    ]]
    return (
        _t(bot, "menu_confirm_restart", lang),
        InlineKeyboardMarkup(rows),
    )


def prompt_menu(bot, lang: str, back_to: str):
    return InlineKeyboardMarkup([[
        _btn(bot, "menu_cancel", lang, "m:cancelinput"),
    ]] + _back_home(bot, lang, back_to=back_to))


# ---------- Диспетчер ----------

# Действия, требующие прав админа
ADMIN_ACTIONS = {
    "pause", "resume", "addst", "dellist", "rmst",
    "mvlist", "mvpos", "mvto", "lim", "setlim",
    "tok", "chktokens", "restart?", "restart!",
}


async def on_callback(bot, update: Update, context):
    query = update.callback_query
    uid = update.effective_user.id
    if not bot.is_user_allowed(uid) or bot.account_manager is None:
        try:
            await query.answer()
        except Exception:
            pass
        return
    lang = bot._lang(uid)
    is_admin = bot.is_admin(uid)

    data = (query.data or "")
    if not data.startswith("m:"):
        await query.answer()
        return
    parts = data[2:].split(":")
    action = parts[0]
    args = parts[1:]

    if action in ADMIN_ACTIONS and not is_admin:
        await query.answer(
            _t(bot, "menu_only_admin", lang), show_alert=True
        )
        return

    handler = _HANDLERS.get(action)
    if handler is None:
        await query.answer()
        return
    try:
        await handler(bot, query, uid, lang, is_admin, args)
    except Exception as e:
        try:
            await query.answer(f"Error: {e}", show_alert=True)
        except Exception:
            pass


def _worker(bot, args) -> tuple:
    """(worker, idx) по первому аргументу или (None, -1)."""
    try:
        idx = int(args[0])
    except (IndexError, ValueError):
        return None, -1
    return bot.worker_by_index(idx), idx


async def _show_account(bot, query, lang, idx):
    worker = bot.worker_by_index(idx)
    if worker is None:
        await query.answer(
            _t(bot, "menu_gone", lang), show_alert=True
        )
        text, markup = accounts_menu(bot, lang)
        await _edit(query, text, markup)
        return
    text, markup = account_menu(bot, worker, idx, lang)
    await query.answer()
    await _edit(query, text, markup)


async def h_home(bot, query, uid, lang, is_admin, args):
    text, markup = main_menu(bot, lang, is_admin)
    await query.answer()
    await _edit(query, text, markup)


async def h_status(bot, query, uid, lang, is_admin, args):
    await query.answer()
    await _edit(
        query, bot._build_status_text(lang),
        InlineKeyboardMarkup(_back_home(bot, lang)),
    )


async def h_balance(bot, query, uid, lang, is_admin, args):
    await query.answer()
    text = bot._build_balance_text(lang)
    if len(text) > 4000:
        text = text[:4000]
    await _edit(
        query, text,
        InlineKeyboardMarkup(_back_home(bot, lang)),
    )


async def h_stats(bot, query, uid, lang, is_admin, args):
    if not args:
        await query.answer()
        text, markup = stats_menu(bot, lang)
        await _edit(query, text, markup)
        return
    try:
        hours = max(1, min(int(args[0]), 720))
    except ValueError:
        hours = 24
    await query.answer()
    await _edit(query, _t(bot, "menu_working", lang))
    gains = await asyncio.to_thread(
        bot.account_manager.get_gains, hours
    )
    await _edit(
        query, bot._build_stats_text(lang, hours, gains),
        InlineKeyboardMarkup(_back_home(bot, lang, "m:stats")),
    )


async def h_accounts(bot, query, uid, lang, is_admin, args):
    await query.answer()
    text, markup = accounts_menu(bot, lang)
    await _edit(query, text, markup)


async def h_acc(bot, query, uid, lang, is_admin, args):
    _, idx = _worker(bot, args)
    await _show_account(bot, query, lang, idx)


async def h_pause(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await worker.pause()
    bot._save_config()
    await _show_account(bot, query, lang, idx)


async def h_resume(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await query.answer()
    await _edit(query, _t(bot, "menu_working", lang))
    await worker.resume()
    bot._save_config()
    await _show_account(bot, query, lang, idx)


async def h_addst(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    bot.pending[uid] = {"op": "addstreamer", "i": idx}
    await query.answer()
    await _edit(
        query,
        _t(bot, "menu_send_nick", lang,
           alias=html.escape(worker.alias)),
        prompt_menu(bot, lang, back_to=f"m:acc:{idx}"),
    )


async def h_dellist(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await query.answer()
    text, markup = _streamer_list_menu(
        bot, worker, idx, lang, "menu_pick_del", "rmst"
    )
    await _edit(query, text, markup)


async def h_rmst(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    try:
        s = int(args[1])
        name = worker.state.streamer_order[s]
    except (IndexError, ValueError, AttributeError, TypeError):
        await query.answer(
            _t(bot, "menu_gone", lang), show_alert=True
        )
        return
    await worker.remove_streamer(name)
    bot._save_config()
    await query.answer(
        _t(bot, "streamer_removed", lang,
           streamer=name, alias=worker.alias)[:200]
    )
    if worker.state.streamer_order:
        text, markup = _streamer_list_menu(
            bot, worker, idx, lang, "menu_pick_del", "rmst"
        )
        await _edit(query, text, markup)
    else:
        await _show_account(bot, query, lang, idx)


async def h_mvlist(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await query.answer()
    text, markup = _streamer_list_menu(
        bot, worker, idx, lang, "menu_pick_move", "mvpos"
    )
    await _edit(query, text, markup)


async def h_mvpos(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    try:
        s = int(args[1])
        _ = worker.state.streamer_order[s]
    except (IndexError, ValueError, AttributeError, TypeError):
        await query.answer(
            _t(bot, "menu_gone", lang), show_alert=True
        )
        return
    await query.answer()
    text, markup = move_pos_menu(bot, worker, idx, s, lang)
    await _edit(query, text, markup)


async def h_mvto(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    try:
        s = int(args[1])
        p = int(args[2])
        name = worker.state.streamer_order[s]
    except (IndexError, ValueError, AttributeError, TypeError):
        await query.answer(
            _t(bot, "menu_gone", lang), show_alert=True
        )
        return
    await worker.move_streamer(name, p)
    bot._save_config()
    await _show_account(bot, query, lang, idx)


async def h_lim(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await query.answer()
    text, markup = limit_menu(bot, worker, idx, lang)
    await _edit(query, text, markup)


async def h_setlim(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    try:
        n = int(args[1])
    except (IndexError, ValueError):
        await query.answer(
            _t(bot, "menu_gone", lang), show_alert=True
        )
        return
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await worker.set_max_concurrent(max(1, min(n, 10)))
    bot._save_config()
    await _show_account(bot, query, lang, idx)


async def h_tok(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    bot.pending[uid] = {"op": "settoken", "i": idx}
    await query.answer()
    await _edit(
        query,
        _t(bot, "menu_send_token", lang,
           alias=html.escape(worker.alias)),
        prompt_menu(bot, lang, back_to=f"m:acc:{idx}"),
    )


async def h_chk(bot, query, uid, lang, is_admin, args):
    worker, idx = _worker(bot, args)
    if worker is None:
        return await h_accounts(bot, query, uid, lang, is_admin, args)
    await query.answer()
    await _edit(query, _t(bot, "token_checking", lang))
    valid, info = await asyncio.to_thread(
        bot._validate_kick_token, worker.token
    )
    if valid:
        mark = _t(bot, "token_check_ok", lang)
    elif info == "invalid":
        mark = _t(bot, "token_check_bad", lang)
    else:
        mark = _t(bot, "token_check_err", lang,
                   info=html.escape(info))
    text = (
        f"👤 <b>{html.escape(worker.alias)}</b> "
        f"<code>{bot._mask_token(worker.token)}</code>\n"
        f"   └ {mark}"
    )
    await _edit(
        query, text,
        InlineKeyboardMarkup(
            _back_home(bot, lang, back_to=f"m:acc:{idx}")
        ),
    )


async def h_chktokens(bot, query, uid, lang, is_admin, args):
    await query.answer()
    await _edit(query, _t(bot, "menu_working", lang))
    results = await bot.run_tokens_check()
    await _edit(
        query, bot._format_tokens_report(lang, results),
        InlineKeyboardMarkup(_back_home(bot, lang)),
    )


async def h_errors(bot, query, uid, lang, is_admin, args):
    await query.answer()
    await _edit(
        query, bot._build_errors_text(lang),
        InlineKeyboardMarkup(_back_home(bot, lang)),
    )


async def h_logs(bot, query, uid, lang, is_admin, args):
    await query.answer()
    await _edit(
        query, bot._read_log_tail(lang, 20),
        InlineKeyboardMarkup(_back_home(bot, lang)),
    )


async def h_restart_q(bot, query, uid, lang, is_admin, args):
    await query.answer()
    text, markup = restart_confirm(bot, lang)
    await _edit(query, text, markup)


async def h_restart_go(bot, query, uid, lang, is_admin, args):
    from loguru import logger
    await query.answer(_t(bot, "menu_restarting", lang))
    await _edit(query, _t(bot, "menu_restarting", lang))
    await asyncio.sleep(1)
    logger.info("Restart requested via Telegram menu")
    sys.exit(1)


async def h_cancelinput(bot, query, uid, lang, is_admin, args):
    bot.pending.pop(uid, None)
    await query.answer()
    text, markup = main_menu(bot, lang, is_admin)
    await _edit(query, text, markup)


_HANDLERS = {
    "home": h_home,
    "status": h_status,
    "balance": h_balance,
    "stats": h_stats,
    "accounts": h_accounts,
    "acc": h_acc,
    "pause": h_pause,
    "resume": h_resume,
    "addst": h_addst,
    "dellist": h_dellist,
    "rmst": h_rmst,
    "mvlist": h_mvlist,
    "mvpos": h_mvpos,
    "mvto": h_mvto,
    "lim": h_lim,
    "setlim": h_setlim,
    "tok": h_tok,
    "chk": h_chk,
    "chktokens": h_chktokens,
    "errors": h_errors,
    "logs": h_logs,
    "restart?": h_restart_q,
    "restart!": h_restart_go,
    "cancelinput": h_cancelinput,
}


# ---------- Многошаговый ввод ----------

async def handle_pending_text(bot, update: Update, context):
    """Обработка текстового ответа на запрос меню (ник/токен)."""
    uid = update.effective_user.id
    lang = bot._lang(uid)
    job = bot.pending.pop(uid, None)
    if job is None:
        return
    worker = bot.worker_by_index(job.get("i", -1))
    if worker is None:
        await update.message.reply_text(
            _t(bot, "menu_gone", lang), parse_mode=ParseMode.HTML
        )
        return
    idx = job["i"]
    num = bot.account_manager.workers.index(worker) + 1

    if job.get("op") == "addstreamer":
        name = (update.message.text or "").strip().lstrip("@")
        if not bot._valid_streamer_name(name):
            text = _t(bot, "bad_streamer_name", lang,
                       streamer=html.escape(name))
            markup = InlineKeyboardMarkup(
                _back_home(bot, lang, back_to=f"m:acc:{idx}")
            )
            await update.message.reply_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
            return
        result = await worker.add_streamer(name)
        if result == "exists":
            text = _t(bot, "streamer_exists", lang,
                       streamer=html.escape(name),
                       alias=html.escape(worker.alias))
        else:
            bot._save_config()
            st = worker.state.streamers[name]
            text = _t(bot, "streamer_added", lang,
                       streamer=html.escape(name),
                       alias=html.escape(worker.alias),
                       prio=st.priority,
                       online="🟢" if st.is_online else "⚫")
        card, markup = account_menu(bot, worker, idx, lang)
        await update.message.reply_text(
            f"{text}\n\n{card}",
            reply_markup=markup, parse_mode=ParseMode.HTML,
        )
        return

    if job.get("op") == "settoken":
        try:
            await update.message.delete()
        except Exception:
            pass
        new_token = (update.message.text or "").strip()
        wait_msg = await update.message.reply_text(
            _t(bot, "token_checking", lang),
            parse_mode=ParseMode.HTML,
        )
        valid, info = await asyncio.to_thread(
            bot._validate_kick_token, new_token
        )
        worker.set_token(new_token)
        bot._save_config()
        if valid:
            check = _t(bot, "token_check_ok", lang)
        elif info == "invalid":
            check = _t(bot, "token_check_bad", lang)
        else:
            check = _t(bot, "token_check_err", lang,
                        info=html.escape(info))
        text = _t(bot, "token_updated", lang,
                   alias=html.escape(worker.alias),
                   mask=bot._mask_token(worker.token),
                   check=check)
        _, markup = account_menu(bot, worker, idx, lang)
        try:
            await wait_msg.edit_text(
                text, reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await update.message.reply_text(
                text, reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        return
