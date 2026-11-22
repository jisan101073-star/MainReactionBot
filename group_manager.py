"""GROUP MANAGER for the JR Reaction Bot Manager.

Owner-only tool, opened from the Manager Bot menu:

    GROUP MANAGER -> pick a Worker Bot -> pick one of its groups
                  -> write a message -> preview + choose a delete timer
                  -> confirm -> the Worker Bot posts it in that group.

Rules this module follows:
* Groups and supergroups only. Channels are never listed or used.
* The group list is READ from the Worker's own saved state
  (reaction_bots/<bot_id>/groups, or the running Worker's memory). It is never
  written to, so this module cannot damage any Worker or Firebase data.
* The only thing written anywhere is a delete job (same record the existing
  broadcast delete scheduler already processes) for messages with a timer.
* Every message carries a permanent "Add Bot to Group" button.
* Nothing here changes reactions: a Worker keeps reacting exactly as before
  (it reacts where Telegram allows it and stops where it does not).
* This file never imports bot.py. bot.py hands it what it needs through
  register(), so there is no circular import and no second copy of bot.py.
"""

import asyncio
import html
import logging
import re
import time
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Optional admin rights requested by the "Add Bot to Group" button, in
# Telegram's deep-link format, for example "delete_messages+restrict_members".
# Empty means a plain add link, identical to the Worker's own "ADD TO GROUP"
# button. The group's admin always sees and confirms the rights.
ADD_BOT_ADMIN_RIGHTS = ""

ADD_BUTTON_TEXT = "➕ Add Bot to Group"

PAGE_SIZE = 8
BUTTON_TITLE_LIMIT = 38

# The message text is shown inside a preview, so keep clear of Telegram's
# 4096-character limit for that preview message.
MAX_MESSAGE_CHARS = 3500

# Telegram only lets a bot delete its own messages younger than 48 hours,
# so no delete time above 47 hours is ever accepted (buttons or typed).
MIN_TIMER_SECONDS = 60
MAX_TIMER_SECONDS = 47 * 3600

TIMER_CHOICES = (
    (0, "♾ Permanent"),
    (3600, "1 Hour"),
    (21600, "6 Hours"),
    (43200, "12 Hours"),
    (86400, "24 Hours"),
    (MAX_TIMER_SECONDS, "47 Hours"),
)
TIMER_LABELS = {seconds: label for seconds, label in TIMER_CHOICES}

# A half-finished compose session is forgotten after this long.
PENDING_TTL_SECONDS = 15 * 60

_CTX_KEY = "gm_ctx"
_PENDING_KEY = "gm_pending"

CB_PREFIX = "gm:"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _header(title):
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        f"   🗂 <b>{html.escape(title)}</b> 🗂\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )


def _button(text, data=None, url=None, style=None):
    kwargs = {}
    if data is not None:
        kwargs["callback_data"] = data
    if url is not None:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(text, **kwargs)


def _short(text, limit=BUTTON_TITLE_LIMIT):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _natural_key(value):
    parts = re.split(r"(\d+)", str(value).lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _ctx(context):
    return context.application.bot_data.get(_CTX_KEY)


def _pending_store(context):
    return context.application.bot_data.setdefault(_PENDING_KEY, {})


def _get_pending(context, owner_id):
    store = _pending_store(context)
    pending = store.get(owner_id)
    if not pending:
        return None
    if time.time() - pending.get("created", 0) > PENDING_TTL_SECONDS:
        store.pop(owner_id, None)
        return None
    return pending


def _set_pending(context, owner_id, **fields):
    fields["created"] = time.time()
    _pending_store(context)[owner_id] = fields
    return fields


def _clear_pending(context, owner_id=None):
    store = _pending_store(context)
    if owner_id is None:
        store.clear()
    else:
        store.pop(owner_id, None)


def _is_owner(update, ctx):
    user = update.effective_user
    return bool(user and ctx and user.id == ctx.owner_id)


def _running_app(ctx, bot_id):
    app = ctx.running_bots.get(bot_id) or ctx.running_bots.get(str(bot_id))
    if app is None:
        return None
    checker = ctx.is_worker_running
    if checker is not None:
        try:
            if not checker(app):
                return None
        except Exception:
            return None
    return app


async def _show(update, text, markup):
    """Edit the menu message in place; fall back to a new message."""
    query = update.callback_query
    message = query.message if query else None
    if message is None:
        if update.effective_chat is not None:
            await update.get_bot().send_message(
                update.effective_chat.id,
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        return
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ---------------------------------------------------------------------------
# Data access (read-only)
# ---------------------------------------------------------------------------

async def _load_bots(ctx):
    """Return [(bot_id, username, enabled, running)] sorted naturally."""
    registry = await ctx.get_managed_bots()
    items = []
    seen = set()
    for key, record in registry.items():
        if not isinstance(record, dict):
            continue
        try:
            bot_id = int(record.get("bot_id", key))
        except (TypeError, ValueError):
            continue
        if bot_id in seen:
            continue
        seen.add(bot_id)
        username = str(record.get("bot_username") or f"bot_{bot_id}")
        items.append({
            "id": bot_id,
            "username": username,
            "enabled": bool(record.get("enabled", False)),
            "running": _running_app(ctx, bot_id) is not None,
            "record": record,
        })
    items.sort(key=lambda item: _natural_key(item["username"]))
    return items


async def _load_bot(ctx, bot_id):
    for item in await _load_bots(ctx):
        if item["id"] == bot_id:
            return item
    return None


async def _load_groups(ctx, bot_id):
    """Groups/supergroups this Worker is currently a member of (read-only).

    A running Worker's in-memory state is the freshest copy; otherwise the
    saved copy in Firebase is used. Channels live in a different collection
    ("channels") and are never read here.
    """
    groups = None
    app = _running_app(ctx, bot_id)
    if app is not None:
        state = app.bot_data.get("state")
        if isinstance(state, dict) and isinstance(state.get("groups"), dict):
            groups = dict(state["groups"])

    if groups is None:
        data = await ctx.firebase_call(
            ctx.db.reference(f"reaction_bots/{int(bot_id)}/groups").get
        )
        groups = data if isinstance(data, dict) else {}

    result = []
    for key, info in groups.items():
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            continue
        title = ""
        if isinstance(info, dict):
            title = str(info.get("title") or "")
        elif info:
            title = str(info)
        result.append((chat_id, title or "Unnamed group"))
    result.sort(key=lambda item: (item[1].lower(), item[0]))
    return result


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def _error_screen(text, back="gm:home"):
    markup = InlineKeyboardMarkup([
        [_button("♻️ Refresh", data=back)],
        [_button("‹ Main Menu", data="mgr:home")],
    ])
    return text, markup


async def _bots_screen(ctx, page):
    try:
        bots = await _load_bots(ctx)
    except Exception as exc:
        logger.exception("Group Manager could not load the bot list.")
        return _error_screen(
            _header("GROUP MANAGER")
            + "❌ <b>Could not load the bot list.</b>\n"
            "Firebase is temporarily unavailable. Nothing was changed.\n"
            f"<code>{html.escape(str(exc)[:200])}</code>",
        )

    text = _header("GROUP MANAGER")
    if not bots:
        text += "No Reaction Bots have been added yet."
        return text, InlineKeyboardMarkup([
            [_button("‹ Main Menu", data="mgr:home")],
        ])

    total_pages = max(1, (len(bots) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = bots[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text += "Select a Reaction Bot to see the groups it is in."
    keyboard = []
    for item in chunk:
        icon = "🟢" if item["running"] else ("🟡" if item["enabled"] else "⚪")
        keyboard.append([
            _button(f"{icon} @{item['username']}", data=f"gm:b:{item['id']}:0", style="primary")
        ])

    nav = []
    if page > 0:
        nav.append(_button("‹ Previous", data=f"gm:bots:{page - 1}"))
    if page < total_pages - 1:
        nav.append(_button("Next ›", data=f"gm:bots:{page + 1}"))
    if nav:
        keyboard.append(nav)
        text += f"\n\n📄 Page {page + 1}/{total_pages}"
    keyboard.append([_button("‹ Main Menu", data="mgr:home")])
    return text, InlineKeyboardMarkup(keyboard)


async def _groups_screen(ctx, bot_id, page):
    try:
        bot = await _load_bot(ctx, bot_id)
    except Exception:
        logger.exception("Group Manager could not load the bot.")
        bot = None
    if bot is None:
        return _error_screen(
            _header("GROUP MANAGER") + "❌ This bot is no longer in the Manager.",
            back="gm:home",
        )

    try:
        groups = await _load_groups(ctx, bot_id)
    except Exception as exc:
        logger.exception("Group Manager could not load groups for bot %s.", bot_id)
        return _error_screen(
            _header("GROUP MANAGER")
            + "❌ <b>Could not load the group list.</b>\n"
            "Firebase is temporarily unavailable. Nothing was changed.\n"
            f"<code>{html.escape(str(exc)[:200])}</code>",
            back=f"gm:b:{bot_id}:{page}",
        )

    text = _header("GROUP MANAGER") + f"🤖 <b>@{html.escape(bot['username'])}</b>\n\n"
    keyboard = []

    if not groups:
        text += "This bot is not in any group yet."
    else:
        total_pages = max(1, (len(groups) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        text += "Select a group:\n\n"
        for index, (chat_id, title) in enumerate(chunk, start=page * PAGE_SIZE + 1):
            text += f"<b>{index}.</b> {html.escape(title)}\n    🆔 <code>{chat_id}</code>\n"
            keyboard.append([
                _button(f"👥 {_short(title)}", data=f"gm:g:{bot_id}:{chat_id}:{page}", style="success")
            ])
        nav = []
        if page > 0:
            nav.append(_button("‹ Previous", data=f"gm:b:{bot_id}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(_button("Next ›", data=f"gm:b:{bot_id}:{page + 1}"))
        if nav:
            keyboard.append(nav)
            text += f"\n📄 Page {page + 1}/{total_pages}"

    keyboard.append([_button("‹ Back", data="gm:home"), _button("‹ Main Menu", data="mgr:home")])
    return text, InlineKeyboardMarkup(keyboard)


async def _group_panel_screen(ctx, bot_id, chat_id, page=0):
    bot = await _load_bot(ctx, bot_id)
    groups = await _load_groups(ctx, bot_id) if bot else []
    title = next((t for cid, t in groups if cid == chat_id), None)
    if bot is None or title is None:
        return _error_screen(
            _header("GROUP MANAGER")
            + "❌ This group is no longer in the bot's group list.",
            back=f"gm:b:{bot_id}:{page}",
        )
    text = (
        _header("GROUP MANAGER")
        + f"🤖 <b>@{html.escape(bot['username'])}</b>\n"
        f"👥 <b>{html.escape(title)}</b>\n"
        f"🆔 <code>{chat_id}</code>"
    )
    markup = InlineKeyboardMarkup([
        [_button("✉️ Send Message", data=f"gm:s:{bot_id}:{chat_id}:{page}", style="success")],
        [_button("‹ Back", data=f"gm:b:{bot_id}:{page}"), _button("‹ Main Menu", data="mgr:home")],
    ])
    return text, markup


def _format_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def _timer_text(seconds):
    return _format_duration(seconds)


_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_HOURS_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|hour|hours)?$", re.IGNORECASE)


def parse_hours(raw):
    """Turn typed hours (2, 5.5, 2h, Bangla digits) into seconds.

    Returns (seconds, None) or (None, error_text). Anything above 47 hours is
    refused: Telegram will not let a bot delete its own message after 48 hours.
    """
    value = str(raw or "").strip().translate(_BANGLA_DIGITS)
    match = _HOURS_RE.match(value)
    if not match:
        return None, "Please type just a number of hours, for example 2 or 5.5."
    hours = float(match.group(1).replace(",", "."))
    if hours > MAX_TIMER_SECONDS / 3600:
        return None, (
            "The longest delete time is 47 hours. Telegram does not allow a bot "
            "to delete its own message after 48 hours, so a longer time is not accepted."
        )
    seconds = int(round(hours * 3600))
    if seconds < MIN_TIMER_SECONDS:
        return None, "The shortest delete time is 1 minute (0.02 hours)."
    return seconds, None


def _utf16_len(text):
    """Telegram measures message length in UTF-16 units (emoji count as 2)."""
    return len(text.encode("utf-16-le")) // 2


def _preview_screen(pending):
    text = (
        _header("PREVIEW")
        + f"🤖 <b>@{html.escape(pending['username'])}</b>\n"
        f"👥 <b>{html.escape(pending['title'])}</b>\n\n"
        f"<blockquote>{html.escape(pending['text'])}</blockquote>\n\n"
        "A permanent <b>➕ Add Bot to Group</b> button will be under the message.\n\n"
        "⏱ <b>Choose when the message should be deleted</b> (longest: 47 hours):"
    )
    keyboard = [
        [_button(TIMER_LABELS[0], data="gm:t:0", style="success")],
        [
            _button(TIMER_LABELS[3600], data="gm:t:3600", style="primary"),
            _button(TIMER_LABELS[21600], data="gm:t:21600", style="primary"),
            _button(TIMER_LABELS[43200], data="gm:t:43200", style="primary"),
        ],
        [
            _button(TIMER_LABELS[86400], data="gm:t:86400", style="primary"),
            _button(TIMER_LABELS[MAX_TIMER_SECONDS], data=f"gm:t:{MAX_TIMER_SECONDS}", style="primary"),
        ],
        [_button("✏️ Custom hours", data="gm:tc", style="primary")],
        [_button("❌ Cancel", data="gm:x")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def _custom_timer_screen(pending):
    text = (
        _header("CUSTOM TIME")
        + f"👥 <b>{html.escape(pending['title'])}</b>\n\n"
        "⏱ <b>Type after how many hours the message should be deleted.</b>\n"
        "Examples: <code>2</code>, <code>5.5</code>, <code>47</code>\n\n"
        "Shortest: 1 minute. Longest: <b>47 hours</b>. Telegram does not allow a bot "
        "to delete its own message after 48 hours, so a longer time is not accepted."
    )
    markup = InlineKeyboardMarkup([[
        _button("‹ Back", data="gm:back_timer"),
        _button("❌ Cancel", data="gm:x"),
    ]])
    return text, markup


def _confirm_screen(pending):
    seconds = pending["seconds"]
    when = "never (permanent)" if seconds == 0 else f"after {_timer_text(seconds)}"
    text = (
        _header("CONFIRM")
        + f"🤖 <b>@{html.escape(pending['username'])}</b>\n"
        f"👥 <b>{html.escape(pending['title'])}</b>\n\n"
        f"<blockquote>{html.escape(pending['text'])}</blockquote>\n\n"
        f"🗑 <b>Delete:</b> {when}\n\n"
        "Send this message now?"
    )
    markup = InlineKeyboardMarkup([
        [_button("✅ Send now", data="gm:go", style="success")],
        [_button("‹ Change timer", data="gm:back_timer"), _button("❌ Cancel", data="gm:x")],
    ])
    return text, markup


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _add_button_url(username):
    url = f"https://t.me/{username}?startgroup=true"
    if ADD_BOT_ADMIN_RIGHTS:
        url += f"&admin={ADD_BOT_ADMIN_RIGHTS}"
    return url


def _explain_send_error(exc):
    """Short, honest explanation for the owner. Never exposes tokens."""
    detail = str(exc)
    lowered = detail.lower()
    if isinstance(exc, RetryAfter):
        try:
            wait = max(1, int(getattr(exc, "retry_after", 1)))
        except (TypeError, ValueError):
            wait = 1
        return f"Telegram asked the bot to wait {wait} seconds before sending again. Try again after that."
    if isinstance(exc, Forbidden):
        return (
            "The bot is not allowed to post in this group (it may have been "
            "removed, blocked or muted)."
        )
    if isinstance(exc, BadRequest):
        if "not enough rights" in lowered or "have no rights" in lowered:
            return (
                "This group does not let the bot send messages. Members "
                "must be allowed to send messages for it to post."
            )
        if "chat not found" in lowered:
            return "Telegram cannot find this group any more (the bot may have been removed)."
        if "upgraded to a supergroup" in lowered:
            return "This group was upgraded to a supergroup, so its ID changed. The list will update when the bot is active there."
    return f"Telegram refused the message: {detail[:180]}"


async def _post_message(ctx, bot_item, chat_id, text):
    """Send through the Worker's own bot. Returns (message_id)."""
    record = bot_item["record"]
    bot_id = bot_item["id"]
    app = _running_app(ctx, bot_id)
    temp_bot = None
    try:
        if app is not None:
            telegram_bot = app.bot
        else:
            encrypted = record.get("token")
            if not encrypted:
                raise RuntimeError("This bot has no saved token.")
            temp_bot = ctx.bot_class(token=ctx.decrypt_token(encrypted))
            await temp_bot.initialize()
            telegram_bot = temp_bot

        username = record.get("bot_username") or getattr(telegram_bot, "username", None)
        reply_markup = None
        if username:
            reply_markup = InlineKeyboardMarkup([[
                _button(ADD_BUTTON_TEXT, url=_add_button_url(username), style="success")
            ]])

        sent = await telegram_bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return sent.message_id
    finally:
        if temp_bot is not None:
            try:
                await temp_bot.shutdown()
            except Exception:
                pass


async def _do_send(ctx, pending):
    """Send + schedule deletion. Returns (ok, human_text)."""
    bot_id = pending["bot_id"]
    chat_id = pending["chat_id"]

    planned = pending.get("seconds", 0)
    if planned != 0 and not (MIN_TIMER_SECONDS <= planned <= MAX_TIMER_SECONDS):
        return False, "The delete time is outside the allowed range (1 minute to 47 hours). Nothing was sent."

    bot_item = await _load_bot(ctx, bot_id)
    if bot_item is None:
        return False, "This bot is no longer in the Manager."
    groups = await _load_groups(ctx, bot_id)
    if chat_id not in {cid for cid, _ in groups}:
        return False, "The bot is no longer in this group, so nothing was sent."

    try:
        message_id = await _post_message(ctx, bot_item, chat_id, pending["text"])
    except asyncio.CancelledError:
        raise
    except TelegramError as exc:
        logger.warning("Group Manager send failed bot=%s chat=%s: %s", bot_id, chat_id, exc)
        return False, _explain_send_error(exc)
    except Exception as exc:
        logger.exception("Group Manager send failed unexpectedly bot=%s chat=%s", bot_id, chat_id)
        return False, f"Could not send: {str(exc)[:180]}"

    seconds = pending["seconds"]
    logger.info(
        "Group Manager sent a message bot=%s chat=%s message_id=%s timer=%s",
        bot_id, chat_id, message_id, seconds,
    )

    if seconds <= 0:
        return True, "✅ <b>Message sent.</b>\n🗑 It will stay permanently."

    try:
        await ctx.save_delete_job(bot_id, chat_id, message_id, time.time() + seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Group Manager could not save the delete job.")
        return True, (
            "✅ <b>Message sent.</b>\n"
            "⚠️ The delete timer could NOT be saved (Firebase problem). "
            "Delete this message manually if it must not stay."
        )
    return True, (
        "✅ <b>Message sent.</b>\n"
        f"🗑 It will be deleted after {_timer_text(seconds)}."
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _deny(update):
    query = update.callback_query
    if query:
        await query.answer("Owner only.", show_alert=True)


async def gm_callback(update, context):
    ctx = _ctx(context)
    query = update.callback_query
    if ctx is None or query is None:
        return
    if not _is_owner(update, ctx):
        await _deny(update)
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    owner_id = ctx.owner_id

    try:
        if action == "home":
            _clear_pending(context, owner_id)
            await query.answer()
            text, markup = await _bots_screen(ctx, 0)
            await _show(update, text, markup)
            return

        if action == "bots":
            _clear_pending(context, owner_id)
            await query.answer()
            text, markup = await _bots_screen(ctx, _to_int(parts, 2, 0))
            await _show(update, text, markup)
            return

        if action == "b":
            _clear_pending(context, owner_id)
            await query.answer()
            text, markup = await _groups_screen(ctx, _to_int(parts, 2, 0), _to_int(parts, 3, 0))
            await _show(update, text, markup)
            return

        if action == "g":
            _clear_pending(context, owner_id)
            await query.answer()
            text, markup = await _group_panel_screen(
                ctx, _to_int(parts, 2, 0), _to_int(parts, 3, 0), _to_int(parts, 4, 0)
            )
            await _show(update, text, markup)
            return

        if action == "s":
            await _start_compose(
                update, context, ctx, _to_int(parts, 2, 0), _to_int(parts, 3, 0), _to_int(parts, 4, 0)
            )
            return

        if action == "t":
            await _pick_timer(update, context, ctx, _to_int(parts, 2, -1))
            return

        if action == "tc":
            pending = _get_pending(context, owner_id)
            if not pending or "text" not in pending or pending.get("step") not in ("timer", "custom_timer"):
                await _session_expired(update, context, ctx)
                return
            await query.answer()
            pending["step"] = "custom_timer"
            pending["created"] = time.time()
            text, markup = _custom_timer_screen(pending)
            await _show(update, text, markup)
            return

        if action == "back_timer":
            pending = _get_pending(context, owner_id)
            if not pending or "text" not in pending:
                await _session_expired(update, context, ctx)
                return
            await query.answer()
            pending["step"] = "timer"
            pending["created"] = time.time()
            pending.pop("seconds", None)
            text, markup = _preview_screen(pending)
            await _show(update, text, markup)
            return

        if action == "go":
            await _confirm_send(update, context, ctx)
            return

        if action == "x":
            pending = _get_pending(context, owner_id)
            _clear_pending(context, owner_id)
            await query.answer("Cancelled.")
            if pending and pending.get("bot_id") and pending.get("chat_id"):
                text, markup = await _group_panel_screen(
                    ctx, pending["bot_id"], pending["chat_id"], pending.get("page", 0)
                )
            else:
                text, markup = await _bots_screen(ctx, 0)
            await _show(update, text, markup)
            return

        await query.answer()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Group Manager callback failed: %s", data)
        try:
            await query.answer("Something went wrong. Nothing was changed.", show_alert=True)
        except Exception:
            # The button press was already acknowledged: tell the owner in chat.
            try:
                await query.message.reply_text(
                    "❌ Something went wrong. Nothing was changed. Please try again."
                )
            except Exception:
                pass


def _to_int(parts, index, default):
    try:
        return int(parts[index])
    except (IndexError, TypeError, ValueError):
        return default


async def _session_expired(update, context, ctx):
    _clear_pending(context, ctx.owner_id)
    await update.callback_query.answer("This session expired. Please start again.", show_alert=True)
    text, markup = await _bots_screen(ctx, 0)
    await _show(update, text, markup)


async def _start_compose(update, context, ctx, bot_id, chat_id, page=0):
    query = update.callback_query
    await query.answer()
    bot_item = await _load_bot(ctx, bot_id)
    groups = await _load_groups(ctx, bot_id) if bot_item else []
    title = next((t for cid, t in groups if cid == chat_id), None)
    if bot_item is None or title is None:
        text, markup = _error_screen(
            _header("GROUP MANAGER") + "❌ This group is no longer in the list.",
            back=f"gm:b:{bot_id}:{page}",
        )
        await _show(update, text, markup)
        return

    _set_pending(
        context, ctx.owner_id,
        step="text",
        bot_id=bot_id,
        chat_id=chat_id,
        page=page,
        username=bot_item["username"],
        title=title,
    )
    text = (
        _header("SEND MESSAGE")
        + f"🤖 <b>@{html.escape(bot_item['username'])}</b>\n"
        f"👥 <b>{html.escape(title)}</b>\n\n"
        "✍️ <b>Now type the message you want to send.</b>\n"
        f"<i>Plain text, up to {MAX_MESSAGE_CHARS} characters.</i>"
    )
    markup = InlineKeyboardMarkup([[_button("❌ Cancel", data="gm:x")]])
    await _show(update, text, markup)


async def _pick_timer(update, context, ctx, seconds):
    pending = _get_pending(context, ctx.owner_id)
    if not pending or pending.get("step") != "timer":
        await _session_expired(update, context, ctx)
        return
    if seconds not in TIMER_LABELS:
        await update.callback_query.answer("Invalid choice.", show_alert=True)
        return
    await update.callback_query.answer()
    pending["seconds"] = seconds
    pending["step"] = "confirm"
    pending["created"] = time.time()
    text, markup = _confirm_screen(pending)
    await _show(update, text, markup)


async def _confirm_send(update, context, ctx):
    query = update.callback_query
    pending = _get_pending(context, ctx.owner_id)
    if not pending or pending.get("step") != "confirm":
        await _session_expired(update, context, ctx)
        return

    # Consume the session first so a double tap can never send twice.
    _clear_pending(context, ctx.owner_id)
    await query.answer("Sending…")

    ok, result = await _do_send(ctx, pending)
    title = html.escape(pending["title"])
    if ok:
        text = _header("GROUP MANAGER") + f"👥 <b>{title}</b>\n\n{result}"
    else:
        text = _header("GROUP MANAGER") + f"👥 <b>{title}</b>\n\n❌ <b>Not sent.</b>\n{html.escape(result)}"
    markup = InlineKeyboardMarkup([
        [_button("✉️ Send another", data=f"gm:s:{pending['bot_id']}:{pending['chat_id']}:{pending.get('page', 0)}", style="success")],
        [
            _button("‹ Groups", data=f"gm:b:{pending['bot_id']}:{pending.get('page', 0)}"),
            _button("‹ Main Menu", data="mgr:home"),
        ],
    ])
    await _show(update, text, markup)


async def gm_text(update, context):
    """Takes the owner's typed text ONLY while a compose session is open.

    In every other case it returns without stopping, so the Manager's own text
    handler (Add Bot, Broadcast) keeps working exactly as before.
    """
    ctx = _ctx(context)
    message = update.effective_message
    if ctx is None or message is None or not _is_owner(update, ctx):
        return
    pending = _get_pending(context, ctx.owner_id)
    if not pending:
        return
    step = pending.get("step")
    if step not in ("text", "custom_timer"):
        return

    raw = message.text or ""

    if step == "custom_timer":
        seconds, error = parse_hours(raw)
        if error:
            await message.reply_text("❌ " + error)
            raise ApplicationHandlerStop
        pending["seconds"] = seconds
        pending["step"] = "confirm"
        pending["created"] = time.time()
        confirm, markup = _confirm_screen(pending)
        await message.reply_text(confirm, parse_mode="HTML", reply_markup=markup)
        raise ApplicationHandlerStop

    text = raw.strip()
    if not text:
        await message.reply_text("❌ The message is empty. Please type the text you want to send.")
        raise ApplicationHandlerStop

    length = _utf16_len(text)
    if length > MAX_MESSAGE_CHARS:
        await message.reply_text(
            f"❌ That message is {length} characters long. The limit is "
            f"{MAX_MESSAGE_CHARS}. Please send a shorter one."
        )
        raise ApplicationHandlerStop

    pending["text"] = text
    pending["step"] = "timer"
    pending["created"] = time.time()
    preview, markup = _preview_screen(pending)
    await message.reply_text(preview, parse_mode="HTML", reply_markup=markup)
    raise ApplicationHandlerStop


async def gm_reset_on_other_action(update, context):
    """A Manager menu press or a /command abandons a half-finished compose.

    Never stops the update, so the normal handler still runs.
    """
    ctx = _ctx(context)
    if ctx is None or not _is_owner(update, ctx):
        return
    _clear_pending(context, ctx.owner_id)


# ---------------------------------------------------------------------------
# Wiring (called once by bot.py)
# ---------------------------------------------------------------------------

def register(
    app,
    *,
    owner_id,
    get_managed_bots,
    running_bots,
    decrypt_token,
    save_delete_job,
    firebase_call,
    db,
    bot_class,
    is_worker_running=None,
):
    """Attach the Group Manager to the Manager Bot application.

    All handlers are built first and added at the end, so a failure while
    preparing leaves the Manager completely untouched.
    """
    ctx = SimpleNamespace(
        owner_id=int(owner_id),
        get_managed_bots=get_managed_bots,
        running_bots=running_bots,
        decrypt_token=decrypt_token,
        save_delete_job=save_delete_job,
        firebase_call=firebase_call,
        db=db,
        bot_class=bot_class,
        is_worker_running=is_worker_running,
    )

    handlers = [
        # Abandon a half-written message when the owner does anything else.
        (CallbackQueryHandler(gm_reset_on_other_action, pattern=r"^(?:mgr:|mgrbot:|jrbc:)"), -2),
        (MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, gm_reset_on_other_action), -2),
        # Group Manager screens.
        (CallbackQueryHandler(gm_callback, pattern=r"^gm:"), -1),
        # The typed message (only acts while a compose session is open).
        (MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, gm_text), -1),
    ]

    app.bot_data[_CTX_KEY] = ctx
    app.bot_data.setdefault(_PENDING_KEY, {})
    for handler, group in handlers:
        app.add_handler(handler, group=group)
    logger.info("Group Manager registered on the Manager Bot.")
    return ctx
