import os
import random
import logging
import asyncio
import time
import html
import json
import copy
import re
import hashlib
import hmac
from threading import Thread

from flask import Flask

import firebase_admin
from firebase_admin import credentials, db

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, Update
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken


# =========================================================
# BASIC CONFIG
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

OWNER_ID = 8223664417

IMAGE_URL = "https://telegra.ph/file/07f45aef0cc6323c21c78.jpg"
SUPPORT_URL = "https://t.me/Jr_Auto_ReactionBot"


# =========================================================
# REACTIONS
# =========================================================

REACTION_EMOJIS = [
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂️",
    "🤷", "🤷‍♀️", "😡"
]


# =========================================================
# FLASK SERVER
# =========================================================

app_flask = Flask(__name__)


@app_flask.route("/")
def home():
    return "Reaction Bot is running perfectly! 🚀"


def run_server():
    try:
        port = int(os.environ.get("PORT", 8080))

        app_flask.run(
            host="0.0.0.0",
            port=port,
        )

    except Exception:
        logger.exception("Flask server failed.")


# =========================================================
# FIREBASE
# =========================================================

firebase_initialized = False


def init_firebase():
    global firebase_initialized

    if firebase_initialized:
        return

    database_url = os.getenv(
        "FIREBASE_DATABASE_URL",
        ""
    ).strip()

    service_account_json = os.getenv(
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        ""
    ).strip()

    if not database_url:
        raise RuntimeError(
            "Missing environment variable: FIREBASE_DATABASE_URL"
        )

    if not service_account_json:
        raise RuntimeError(
            "Missing environment variable: FIREBASE_SERVICE_ACCOUNT_JSON"
        )

    try:
        service_account_data = json.loads(
            service_account_json
        )

    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: "
            f"{e}"
        ) from e

    try:
        firebase_admin.get_app()

        logger.info(
            "Firebase app already initialized."
        )

    except ValueError:
        cred = credentials.Certificate(
            service_account_data
        )

        firebase_admin.initialize_app(
            cred,
            {
                "databaseURL": database_url
            }
        )

        logger.info(
            "Firebase initialized successfully."
        )

    firebase_initialized = True


# =========================================================
# MANAGER / DYNAMIC BOT SYSTEM
# =========================================================

MANAGER_BOT_TOKEN = os.getenv("MANAGER_BOT_TOKEN", "").strip()
MANAGER_BOT_USERNAME = ""
TOKEN_ENCRYPTION_SECRET = os.getenv("TOKEN_ENCRYPTION_SECRET", "").strip()

# Runtime map: Telegram bot id -> running Application
RUNNING_BOTS = {}

# Owner-only temporary state for the Manager Bot.
MANAGER_PENDING_ADD = set()
MANAGER_BROADCAST_PENDING = {}

# Short in-memory cache so UI/Manager button presses do not hit Firebase
# on every click. Writes always invalidate the cache immediately.
MANAGED_BOTS_CACHE = None
MANAGED_BOTS_CACHE_EXPIRES = 0.0
MANAGED_BOTS_CACHE_TTL = 12.0

# Limits startup bursts so one Render instance does not try to initialize
# every managed bot at exactly the same moment.
MANAGED_BOT_START_CONCURRENCY = 2
MANAGED_BOT_START_DELAY = 0.35

# Analytics counters are kept in memory and flushed periodically so a busy
# reaction bot does not write to Firebase for every message.
METRICS_SAVE_INTERVAL = 60.0


def _fernet():
    if not TOKEN_ENCRYPTION_SECRET:
        raise RuntimeError(
            "Missing environment variable: TOKEN_ENCRYPTION_SECRET"
        )

    # Stable 32-byte key derived from a Render secret.
    import base64
    import hashlib

    key = base64.urlsafe_b64encode(
        hashlib.sha256(
            TOKEN_ENCRYPTION_SECRET.encode("utf-8")
        ).digest()
    )
    return Fernet(key)


def encrypt_token(token):
    return _fernet().encrypt(
        token.encode("utf-8")
    ).decode("utf-8")


def decrypt_token(value):
    try:
        return _fernet().decrypt(
            value.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Stored bot token cannot be decrypted. Check TOKEN_ENCRYPTION_SECRET."
        ) from e


def managed_bots_ref():
    return db.reference("managed_bots")


async def get_managed_bots(force=False):
    global MANAGED_BOTS_CACHE, MANAGED_BOTS_CACHE_EXPIRES

    now = time.monotonic()
    if (
        not force
        and MANAGED_BOTS_CACHE is not None
        and now < MANAGED_BOTS_CACHE_EXPIRES
    ):
        return copy.deepcopy(MANAGED_BOTS_CACHE)

    try:
        data = await asyncio.to_thread(
            managed_bots_ref().get
        )
    except Exception:
        logger.exception("Failed to read managed bot registry.")
        return copy.deepcopy(MANAGED_BOTS_CACHE or {})

    if not isinstance(data, dict):
        data = {}

    MANAGED_BOTS_CACHE = copy.deepcopy(data)
    MANAGED_BOTS_CACHE_EXPIRES = now + MANAGED_BOTS_CACHE_TTL
    return copy.deepcopy(data)


def invalidate_managed_bots_cache():
    global MANAGED_BOTS_CACHE_EXPIRES
    MANAGED_BOTS_CACHE_EXPIRES = 0.0


async def save_managed_bot(bot_id, data):
    await asyncio.to_thread(
        managed_bots_ref().child(str(bot_id)).set,
        data
    )
    invalidate_managed_bots_cache()


async def delete_managed_bot(bot_id):
    await asyncio.to_thread(
        managed_bots_ref().child(str(bot_id)).delete
    )
    invalidate_managed_bots_cache()


# =========================================================
# STATE
# =========================================================

def create_empty_state(
    bot_id=None,
    bot_username=None
):
    return {
        "bot_id": bot_id,
        "bot_username": bot_username,
        "users": {},
        "channels": {},
        "groups": {},
        "metrics": {
            "messages_seen": 0,
            "reactions_sent": 0,
            "reaction_failures": 0,
        },
    }


def normalize_state(
    data,
    bot_id=None,
    bot_username=None
):
    if not isinstance(data, dict):
        data = {}

    users = data.get(
        "users",
        {}
    )

    channels = data.get(
        "channels",
        {}
    )

    groups = data.get(
        "groups",
        {}
    )

    if not isinstance(users, dict):
        users = {}

    if not isinstance(channels, dict):
        channels = {}

    if not isinstance(groups, dict):
        groups = {}

    metrics = data.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    def metric_int(name):
        try:
            value = int(metrics.get(name, 0))
            return max(0, value)
        except (TypeError, ValueError):
            return 0

    return {
        "bot_id": data.get(
            "bot_id",
            bot_id
        ),
        "bot_username": data.get(
            "bot_username",
            bot_username
        ),
        "users": users,
        "channels": channels,
        "groups": groups,
        "metrics": {
            "messages_seen": metric_int("messages_seen"),
            "reactions_sent": metric_int("reactions_sent"),
            "reaction_failures": metric_int("reaction_failures"),
        },
    }


async def load_state(
    firebase_ref,
    bot_id,
    bot_username
):
    try:
        data = await asyncio.to_thread(
            firebase_ref.get
        )
    except Exception:
        logger.exception(
            "Firebase read failed for bot @%s",
            bot_username or "unknown"
        )
        data = {}

    state = normalize_state(
        data,
        bot_id,
        bot_username
    )

    logger.info(
        "Firebase data loaded for bot @%s",
        bot_username or "unknown"
    )

    return state


async def save_state(context):
    firebase_ref = (
        context.application.bot_data.get(
            "firebase_ref"
        )
    )

    state = (
        context.application.bot_data.get(
            "state"
        )
    )

    if firebase_ref is None:
        logger.error(
            "Firebase reference is missing."
        )
        return

    if state is None:
        logger.error(
            "Bot state is missing."
        )
        return

    lock = (
        context.application.bot_data.get(
            "save_lock"
        )
    )

    if lock is None:
        logger.error(
            "Firebase save lock is missing."
        )
        return

    try:
        async with lock:
            payload = copy.deepcopy(
                state
            )

            await asyncio.to_thread(
                firebase_ref.set,
                payload
            )

    except Exception:
        logger.exception(
            "Firebase save failed."
        )


async def schedule_state_save(context, delay=1.25):
    """Batch closely spaced state changes into one Firebase write."""
    existing = context.application.bot_data.get("state_save_task")
    if existing and not existing.done():
        return

    async def _flush():
        try:
            await asyncio.sleep(delay)
            await save_state(context)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Debounced Firebase save failed.")

    context.application.bot_data["state_save_task"] = asyncio.create_task(_flush())


def increment_metric(context, name, amount=1):
    """Update per-bot analytics in RAM; a shared 60-second flusher persists it."""
    state = context.application.bot_data.get("state")
    if not isinstance(state, dict):
        return

    metrics = state.setdefault("metrics", {})
    try:
        metrics[name] = max(0, int(metrics.get(name, 0)) + int(amount))
    except (TypeError, ValueError):
        metrics[name] = max(0, int(amount))

    # Revision lets the periodic flusher detect updates that happen while a
    # Firebase write is in progress, so no fresh metric increment is lost.
    context.application.bot_data["metrics_dirty"] = True
    context.application.bot_data["metrics_revision"] = int(
        context.application.bot_data.get("metrics_revision", 0)
    ) + 1


def get_metric_int(state, name):
    try:
        return max(0, int((state.get("metrics") or {}).get(name, 0)))
    except (TypeError, ValueError):
        return 0


def format_compact_number(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}K".rstrip("0").rstrip(".")
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    return f"{value / 1_000_000_000:.1f}B".rstrip("0").rstrip(".")


# =========================================================
# CHAT INFORMATION
# =========================================================

def get_chat_info(chat):
    title = getattr(
        chat,
        "title",
        None
    )

    username = getattr(
        chat,
        "username",
        None
    )

    if not title:
        title = (
            getattr(
                chat,
                "first_name",
                None
            )
            or getattr(
                chat,
                "full_name",
                None
            )
            or "Unknown"
        )

    return {
        "id": chat.id,
        "title": title,
        "username": username or "",
    }


def record_chat(
    state,
    chat
):
    if chat.type == "channel":
        collection = state[
            "channels"
        ]
    elif chat.type in (
        "group",
        "supergroup"
    ):
        collection = state[
            "groups"
        ]
    else:
        return False

    key = str(
        chat.id
    )

    new_info = get_chat_info(
        chat
    )

    old_info = collection.get(
        key
    )

    if old_info != new_info:
        collection[key] = new_info
        return True

    return False


def remove_chat(
    state,
    chat
):
    key = str(
        chat.id
    )

    changed = False

    if key in state["channels"]:
        del state["channels"][key]
        changed = True

    if key in state["groups"]:
        del state["groups"][key]
        changed = True

    return changed


# =========================================================
# UPTIME
# =========================================================

def get_uptime(context):
    start_time = (
        context.application.bot_data.get(
            "start_time",
            time.time()
        )
    )

    uptime_seconds = int(
        time.time() - start_time
    )

    hours, remainder = divmod(
        uptime_seconds,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    return (
        f"{hours}h "
        f"{minutes}m "
        f"{seconds}s"
    )


def get_user_display_name(user):
    """Return the caller's Telegram display name, preserving styled Unicode."""
    if not user:
        return "there"

    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    name = " ".join(part for part in (first, last) if part)
    return name or "there"

# =========================================================
# MAIN MENU — PREMIUM UI
# =========================================================

REACTION_BOT_USERNAME_RE = re.compile(
    r"^Jr_Auto_Reaction_(\d+)_Bot$",
    re.IGNORECASE
)


def reaction_bot_serial(username):
    """Return the numeric serial from Jr_Auto_Reaction_<N>_Bot."""
    clean = str(username or "").strip().lstrip("@")
    match = REACTION_BOT_USERNAME_RE.fullmatch(clean)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def get_serial_reaction_bots(
    registry,
    current_username=None,
    enabled_only=True
):
    """
    Only return numbered JR reaction bots, ordered by their numeric serial.

    Accepted username format:
        @Jr_Auto_Reaction_1_Bot
        @Jr_Auto_Reaction_2_Bot
        ...

    Any other username is intentionally ignored.
    """
    current = str(current_username or "").strip().lstrip("@").lower()
    found = {}

    for key, record in (registry or {}).items():
        if not isinstance(record, dict):
            continue

        username = str(record.get("bot_username") or "").strip().lstrip("@")
        serial = reaction_bot_serial(username)
        if serial is None:
            continue

        if username.lower() == current:
            continue

        if enabled_only and not record.get("enabled", False):
            continue

        # One username/serial should appear only once even if Firebase has
        # a duplicate record. Keep the first valid record encountered.
        found.setdefault(
            serial,
            {
                "serial": serial,
                "username": username,
                "bot_id": record.get("bot_id", key),
            }
        )

    return [
        found[number]
        for number in sorted(found)
    ]


def get_main_menu(
    user_name,
    bot_username,
    user_id
):
    display_name = html.escape(
        str(user_name or "there").strip()
    )
    welcome_text = (
        "<b>𝐇𝐞𝐲! 👋</b>\n"
        f"×º°”˜ {display_name} ˜”°º× 😎\n\n"
        "🌹 <b>⟨ 𝐉𝐑 ⟩ 𝐀𝐮𝐭𝐨 𝐑𝐞𝐚𝐜𝐭𝐢𝐨𝐧</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ <b>𝐉𝐨𝐢𝐧 𝐨𝐟𝐟𝐢𝐜𝐢𝐚𝐥 𝐂𝐡𝐚𝐧𝐧𝐞𝐥</b> 👉\n"
        f'<a href="{SUPPORT_URL}">https://t.me/Jr_Auto_ReactionBot</a> ✅\n\n'
        "I can seamlessly react to messages in channels or groups. "
        "Just add me as an <b>admin</b>! ✨\n"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "📣 𝐀𝐃𝐃 𝐓𝐎 𝐂𝐇𝐀𝐍𝐍𝐄𝐋",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startchannel=true"
                ),
                style="primary"
            ),
            InlineKeyboardButton(
                "👥 𝐀𝐃𝐃 𝐓𝐎 𝐆𝐑𝐎𝐔𝐏",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startgroup=true"
                ),
                style="success"
            )
        ],
        [
            InlineKeyboardButton(
                "📘 𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄",
                callback_data="how_to_use",
                style="primary"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 𝐌𝐎𝐑𝐄 𝐁𝐎𝐓𝐒",
                callback_data="more_bots",
                style="success"
            )
        ]
    ]

    if user_id == OWNER_ID:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "📢 𝐀𝐋𝐋 𝐂𝐇𝐀𝐍𝐍𝐄𝐋𝐒",
                    callback_data="all_channels:0",
                    style="primary"
                ),
                InlineKeyboardButton(
                    "👥 𝐀𝐋𝐋 𝐆𝐑𝐎𝐔𝐏𝐒",
                    callback_data="all_groups:0",
                    style="success"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "💬 𝐒𝐔𝐏𝐏𝐎𝐑𝐓",
                url=SUPPORT_URL,
                style="primary"
            )
        ]
    )

    return (
        welcome_text,
        InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# MORE BOTS — NUMBERED JR BOTS ONLY
# =========================================================

async def more_bots_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    registry = await get_managed_bots()
    current_username = context.bot.username or ""
    bots = get_serial_reaction_bots(
        registry,
        current_username=current_username,
        enabled_only=True
    )

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "     ✦ <b>MORE 𝐉𝐑 𝐑𝐄𝐀𝐂𝐓𝐈𝐎𝐍 𝐁𝐎𝐓𝐒</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🌹 <i>Choose your Reaction Bot</i>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = []
    for item in bots:
        serial = item["serial"]
        username = item["username"]
        keyboard.append([
            InlineKeyboardButton(
                f"{serial}. 🤖 @{username}",
                url=f"https://t.me/{username}",
                style="success"
            )
        ])

    if not bots:
        text += (
            "\n\n❌ <i>No numbered Reaction Bots are active yet.</i>"
        )

    keyboard.append([
        InlineKeyboardButton(
            "‹ BACK TO MENU",
            callback_data="back_to_menu",
            style="primary"
        )
    ])

    markup = InlineKeyboardMarkup(keyboard)
    msg = query.message
    if not msg:
        return

    if msg.photo:
        try:
            await msg.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
        except Exception as e:
            logger.warning("More bots caption edit failed: %s", e)

    try:
        await msg.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.warning("More bots text edit failed: %s", e)


# =========================================================
# BOT ANALYTICS
# =========================================================



# =========================================================
# PUBLIC COMMANDS / ADMIN STATUS
# =========================================================

BOT_COMMANDS = [
    BotCommand("start", "Open the main menu"),
    BotCommand("help", "Show commands and help"),
    BotCommand("howtouse", "How to set up and use the bot"),
    BotCommand("bots", "List all numbered JR Reaction Bots"),
    BotCommand("contact", "Contact the admin"),
    BotCommand("status", "Admin-only system health"),
    BotCommand("stats", "Admin-only reaction statistics"),
]


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "🌹 <b>JR AUTO REACTION — HELP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/start — Open the main menu\n"
        "/help — Show this help message\n"
        "/howtouse — Setup and usage instructions\n"
        "/bots — View all active numbered JR Reaction Bots\n"
        "/contact — Contact the admin\n"
        "/status — Detailed system health (Admin only)\n"
        "/stats — Reaction statistics (Admin only)\n\n"
        "For automatic reactions, add the bot to your channel/group "
        "with the required admin permissions.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def howtouse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "📘 <b>HOW TO USE — JR AUTO REACTION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1. ADD THE BOT</b>\n"
        "• Add the bot to your Telegram channel or group.\n"
        "• Give it the required admin permissions.\n"
        "• Make sure reactions are allowed in the chat.\n\n"
        "<b>2. AUTOMATIC REACTION</b>\n"
        "After setup, the bot tries a random supported reaction on new "
        "channel/group messages.\n\n"
        "<b>⚠️ NOT REACTING?</b>\n"
        "Check admin permissions and the chat's allowed reactions.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    registry = await get_managed_bots()
    bots = get_serial_reaction_bots(
        registry,
        current_username=None,
        enabled_only=True,
    )
    if not bots:
        await update.message.reply_text(
            "🤖 <b>ALL JR REACTION BOTS</b>\n\n"
            "No active numbered Reaction Bots were found.",
            parse_mode="HTML",
        )
        return

    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "   ✦ <b>ALL JR REACTION BOTS</b> ✦",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"📊 <b>Total active bots:</b> <code>{len(bots)}</code>",
        "",
    ]
    for item in bots:
        serial = item["serial"]
        username = html.escape(item["username"])
        lines.append(f'{serial}. 🤖 <a href="https://t.me/{username}">@{username}</a>')
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "💬 <b>JR AUTO REACTION — CONTACT</b>\n\n"
        "👤 Admin: @ZinnBro\n"
        f"📣 Channel: <a href=\"{SUPPORT_URL}\">Open official link</a>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ This command is Admin only.")
        return

    registry = await get_managed_bots(force=True)
    numbered = get_serial_reaction_bots(
        registry,
        current_username=None,
        enabled_only=False,
    )
    enabled_records = {
        str(record.get("bot_id", key)): record
        for key, record in (registry or {}).items()
        if isinstance(record, dict) and record.get("enabled", False)
    }
    running_ids = {str(bot_id) for bot_id in RUNNING_BOTS.keys()}
    configured_enabled_ids = set(enabled_records.keys())
    running_count = len(running_ids & configured_enabled_ids)
    stopped_count = max(0, len(configured_enabled_ids) - running_count)
    firebase_ok = True
    try:
        await asyncio.to_thread(managed_bots_ref().get)
    except Exception:
        firebase_ok = False
        logger.exception("Firebase health check failed in /status.")

    state = context.application.bot_data.get("state", {})
    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "   ✦ <b>JR SYSTEM STATUS</b> ✦",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"🧠 <b>Manager runtime:</b> <code>{'Running' if MANAGER_BOT_TOKEN else 'Not configured'}</code>",
        f"🤖 <b>Numbered bots in registry:</b> <code>{len(numbered)}</code>",
        f"⚙️ <b>Enabled workers:</b> <code>{len(configured_enabled_ids)}</code>",
        f"🟢 <b>Running workers in this process:</b> <code>{running_count}</code>",
        f"🔴 <b>Enabled but not running:</b> <code>{stopped_count}</code>",
        f"🔥 <b>Firebase:</b> <code>{'Connected' if firebase_ok else 'Error'}</code>",
        f"⏱ <b>This bot uptime:</b> <code>{get_uptime(context)}</code>",
        f"🕒 <b>Checked:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</code>",
        "",
        "<b>Worker details</b>",
    ]
    for item in numbered:
        bot_id = str(item.get("bot_id", ""))
        record = registry.get(bot_id, {})
        enabled = bool(record.get("enabled", False)) if isinstance(record, dict) else False
        is_running = bot_id in running_ids
        if enabled and is_running:
            label = "🟢 Running"
        elif enabled:
            label = "🟠 Enabled / not running"
        else:
            label = "⚪ Disabled"
        lines.append(f'{item["serial"]}. @{html.escape(item["username"])} — {label}')
    if not numbered:
        lines.append("No numbered JR Reaction Bots found.")
    lines.append("\nℹ️ Running state is based on this Render process's in-memory worker registry.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# =========================================================
# /START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_chat:
        return

    state = context.application.bot_data[
        "state"
    ]

    if (
        update.effective_chat.type
        == "private"
    ):
        user_id = (
            update.effective_chat.id
        )

        user_key = str(
            user_id
        )

        first_name = get_user_display_name(update.effective_user)

        username = (
            update.effective_user.username
            if update.effective_user
            else ""
        )

        user_data = {
            "id": user_id,
            "first_name": (
                first_name or ""
            ),
            "username": (
                username or ""
            ),
        }

        if state["users"].get(user_key) != user_data:
            state["users"][user_key] = user_data
            await save_state(context)

    first_name = get_user_display_name(update.effective_user)

    bot_username = (
        context.bot.username
        or "ReactionBot"
    )

    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    welcome_text, reply_markup = (
        get_main_menu(
            first_name,
            bot_username,
            user_id
        )
    )

    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=IMAGE_URL,
            caption=welcome_text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.warning(
            "Photo send failed: %s",
            e
        )

        if update.message:
            await update.message.reply_text(
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )


# =========================================================
# HOW TO USE — ORIGINAL UI PRESERVED
# =========================================================

async def how_to_use_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    how_to_text = (
        "📘 <b>HOW TO USE — JR AUTO REACTION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1. ADD THE BOT</b>\n"
        "• Add me to your Telegram Channel or Group.\n"
        "• Give me the required admin permissions.\n"
        "• Make sure reactions are allowed in the chat.\n\n"
        "<b>2. SUPPORTED REACTIONS</b>\n"
        "👍 👎 ❤️ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢\n"
        "🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳\n"
        "❤️‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓\n"
        "🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈\n"
        "😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒\n"
        "💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂️ 🤷 🤷‍♀️ 😡\n\n"
        "<b>3. AUTOMATIC REACTION</b>\n"
        "After setup, I automatically choose a random supported reaction for new messages. ⚡\n\n"
        "<b>⚠️ NOT REACTING?</b>\n"
        "Check that the bot is still an admin and that the selected reactions are enabled in your Channel or Group.\n\n"
        "✨ <i>That's it — add me, allow reactions, and you're ready.</i>"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "‹  BACK TO MENU",
                callback_data="back_to_menu",
                style="primary"
            )
        ]
    ]

    markup = InlineKeyboardMarkup(
        keyboard
    )

    if (
        query.message
        and query.message.photo
    ):
        try:
            await query.message.edit_caption(
                caption=how_to_text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        except Exception as e:
            logger.warning(
                "Guide caption edit failed: %s",
                e
            )

    if query.message:
        await query.message.edit_text(
            text=how_to_text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )


# =========================================================
# ALL CHANNELS
# =========================================================

def truncate_text(
    text,
    max_length
):
    text = str(
        text or ""
    )

    if len(text) <= max_length:
        return text

    return (
        text[:max_length - 3]
        + "..."
    )


def build_all_channels_page(
    state,
    page
):
    channels = list(
        state.get(
            "channels",
            {}
        ).values()
    )

    channels.sort(
        key=lambda x: str(
            x.get(
                "title",
                ""
            )
        ).lower()
    )

    per_page = 5
    total = len(
        channels
    )

    total_pages = max(
        1,
        (
            total
            + per_page
            - 1
        ) // per_page
    )

    page = max(
        0,
        min(
            page,
            total_pages - 1
        )
    )

    start = (
        page * per_page
    )

    page_channels = channels[
        start:start + per_page
    ]

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>📢 ALL CHANNELS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    if not channels:
        text += (
            "❌ <b>No channels recorded yet.</b>\n\n"
            "The bot will automatically save a "
            "channel when it receives a new "
            "channel update."
        )

    else:
        text += (
            f"📊 <b>Total:</b> "
            f"<code>{total}</code>\n\n"
        )

        for number, channel in enumerate(
            page_channels,
            start=start + 1
        ):
            title = html.escape(
                truncate_text(
                    channel.get(
                        "title",
                        "Unknown Channel"
                    ),
                    45
                )
            )

            username = channel.get(
                "username",
                ""
            )

            channel_id = channel.get(
                "id",
                ""
            )

            if username:
                username_text = (
                    "@"
                    + html.escape(
                        truncate_text(
                            username,
                            35
                        )
                    )
                )
            else:
                username_text = (
                    "🔒 Private / No Username"
                )

            text += (
                f"<b>{number}. {title}</b>\n"
                f"   👤 <code>{username_text}</code>\n"
                f"   🆔 <code>{channel_id}</code>\n\n"
            )

        text += (
            f"📄 <b>Page:</b> "
            f"{page + 1}/{total_pages}"
        )

    keyboard = []
    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "‹ Previous",
                callback_data=(
                    f"all_channels:{page - 1}"
                )
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "Next ›",
                callback_data=(
                    f"all_channels:{page + 1}"
                )
            )
        )

    if navigation:
        keyboard.append(
            navigation
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "‹  BACK TO MENU",
                callback_data="back_to_menu"
            )
        ]
    )

    markup = InlineKeyboardMarkup(
        keyboard
    )

    return text, markup


async def all_channels_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    if (
        not query.from_user
        or query.from_user.id != OWNER_ID
    ):
        await query.answer(
            "This section is owner only.",
            show_alert=True
        )
        return

    await query.answer()

    try:
        page = int(
            query.data.split(":")[1]
        )
    except Exception:
        page = 0

    state = context.application.bot_data[
        "state"
    ]

    text, markup = build_all_channels_page(
        state,
        page
    )

    if (
        query.message
        and query.message.photo
    ):
        try:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        except Exception as e:
            logger.warning(
                "All Channels caption edit failed: %s",
                e
            )

    if query.message:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )


# =========================================================
# ALL GROUPS
# =========================================================

def build_all_groups_page(
    state,
    page
):
    groups = list(
        state.get(
            "groups",
            {}
        ).values()
    )

    groups.sort(
        key=lambda x: str(
            x.get(
                "title",
                ""
            )
        ).lower()
    )

    per_page = 5
    total = len(groups)
    total_pages = max(
        1,
        (total + per_page - 1) // per_page
    )

    page = max(
        0,
        min(page, total_pages - 1)
    )

    start = page * per_page
    page_groups = groups[start:start + per_page]

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "     ✦ <b>👥 ALL GROUPS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    if not groups:
        text += (
            "❌ <b>No groups recorded yet.</b>\n\n"
            "The bot will automatically save a group when it receives a new group update."
        )
    else:
        text += f"📊 <b>Total:</b> <code>{total}</code>\n\n"

        for number, group in enumerate(
            page_groups,
            start=start + 1
        ):
            title = html.escape(
                truncate_text(
                    group.get(
                        "title",
                        "Unknown Group"
                    ),
                    45
                )
            )

            username = str(group.get("username") or "").strip()
            group_id = group.get("id", "")

            username_text = (
                "@" + html.escape(truncate_text(username, 35))
                if username
                else "🔒 Private / No Username"
            )

            text += (
                f"<b>{number}. {title}</b>\n"
                f"   👤 <code>{username_text}</code>\n"
                f"   🆔 <code>{group_id}</code>\n\n"
            )

        text += f"📄 <b>Page:</b> {page + 1}/{total_pages}"

    keyboard = []
    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "‹ Previous",
                callback_data=f"all_groups:{page - 1}"
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "Next ›",
                callback_data=f"all_groups:{page + 1}"
            )
        )

    if navigation:
        keyboard.append(navigation)

    keyboard.append([
        InlineKeyboardButton(
            "‹ BACK TO MENU",
            callback_data="back_to_menu"
        )
    ])

    return text, InlineKeyboardMarkup(keyboard)


async def all_groups_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query:
        return

    if not query.from_user or query.from_user.id != OWNER_ID:
        await query.answer(
            "This section is owner only.",
            show_alert=True
        )
        return

    await query.answer()

    try:
        page = int(query.data.split(":")[-1])
    except Exception:
        page = 0

    state = context.application.bot_data.get(
        "state",
        create_empty_state(
            getattr(context.bot, "id", None),
            getattr(context.bot, "username", None)
        )
    )

    text, markup = build_all_groups_page(
        state,
        page
    )

    msg = query.message
    if not msg:
        return

    if msg.photo:
        try:
            await msg.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
        except Exception as e:
            logger.warning("Groups caption edit failed: %s", e)

    await msg.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True
    )


# =========================================================
# BACK TO MENU
# =========================================================

async def back_to_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    first_name = get_user_display_name(query.from_user)

    bot_username = (
        context.bot.username
        or "ReactionBot"
    )

    user_id = (
        query.from_user.id
        if query.from_user
        else 0
    )

    welcome_text, reply_markup = (
        get_main_menu(
            first_name,
            bot_username,
            user_id
        )
    )

    if (
        query.message
        and query.message.photo
    ):
        try:
            await query.message.edit_caption(
                caption=welcome_text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            return

        except Exception as e:
            logger.warning(
                "Menu caption edit failed: %s",
                e
            )

    if query.message:
        await query.message.edit_text(
            text=welcome_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )


# =========================================================
# /STATS
# =========================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user:
        return

    if update.effective_user.id != OWNER_ID:
        return

    state = context.application.bot_data[
        "state"
    ]

    stats_text = (
        "📊 <b><u>PREMIUM BOT STATISTICS</u></b> 📊\n\n"
        "<blockquote>"
        "👥 <b>Audience & Reach:</b>\n"
        f"👤 <b>Total Users:</b> "
        f"<code>{len(state['users'])}</code>\n"
        f"📢 <b>Channels Added:</b> "
        f"<code>{len(state['channels'])}</code>\n"
        f"👥 <b>Groups Added:</b> "
        f"<code>{len(state['groups'])}</code>\n"
        "</blockquote>\n"
        "<blockquote>"
        "⚙️ <b>System Information:</b>\n"
        f"⏱ <b>Uptime:</b> "
        f"<code>{get_uptime(context)}</code>\n"
        "⚡ <b>Server Status:</b> "
        "<code>Ultra Fast 🟢</code>\n"
        "🤖 <b>Bot Version:</b> "
        "<code>v2.0.1 (Pro)</code>\n"
        "</blockquote>"
    )

    if update.message:
        await update.message.reply_text(
            text=stats_text,
            parse_mode="HTML"
        )


# =========================================================
# BOT ADDED / REMOVED TRACKING
# =========================================================

async def my_chat_member_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat = update.effective_chat
    member_update = update.my_chat_member

    if not chat or not member_update:
        return

    state = context.application.bot_data[
        "state"
    ]

    new_status = (
        member_update.new_chat_member.status
    )

    if new_status in (
        "member",
        "administrator",
        "creator"
    ):
        changed = record_chat(
            state,
            chat
        )

        if changed:
            await save_state(context)

        logger.info(
            "Bot active in %s | %s | %s",
            chat.type,
            getattr(chat, "title", "Unknown"),
            chat.id
        )

    elif new_status in (
        "left",
        "kicked"
    ):
        changed = remove_chat(
            state,
            chat
        )

        if changed:
            await save_state(context)

        logger.info(
            "Bot removed from %s | %s | %s",
            chat.type,
            getattr(chat, "title", "Unknown"),
            chat.id
        )


# =========================================================
# PRIVATE NON-COMMAND MESSAGE REACTION (WORKER BOTS ONLY)
# =========================================================

INVALID_MESSAGE_REACTIONS = ["🤔", "👀", "🤷", "❓", "🙃"]


async def handle_private_non_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently try the fixed fallback emoji list on ordinary private messages."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type != "private":
        return
    if not message.text or message.text.startswith("/"):
        return

    # Only managed worker apps use this handler; the separate Manager Bot does not.
    for emoji in INVALID_MESSAGE_REACTIONS:
        try:
            await context.bot.set_message_reaction(
                chat_id=chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
                is_big=False,
            )
            return
        except RetryAfter as exc:
            # Respect Telegram's flood limit, then continue to the next emoji.
            try:
                await asyncio.sleep(max(1, int(exc.retry_after)))
            except Exception:
                pass
            continue
        except Exception as exc:
            logger.info(
                "Private fallback reaction %s failed for message %s: %s",
                emoji,
                message.message_id,
                exc,
            )
            continue


# =========================================================
# INCOMING MESSAGE / AUTOMATIC REACTION
# =========================================================

async def handle_incoming(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    target = update.channel_post or update.message

    if not target or not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in (
        "channel",
        "group",
        "supergroup"
    ):
        return

    state = context.application.bot_data[
        "state"
    ]

    increment_metric(context, "messages_seen")

    changed = record_chat(
        state,
        chat
    )

    if changed:
        await schedule_state_save(context)

    await asyncio.sleep(
        random.uniform(0.1, 1.0)
    )

    reaction_semaphore = context.application.bot_data.get(
        "reaction_semaphore"
    )
    if reaction_semaphore is None:
        reaction_semaphore = asyncio.Semaphore(3)
        context.application.bot_data["reaction_semaphore"] = reaction_semaphore

    async with reaction_semaphore:
        await _perform_reaction(context, target)


async def _perform_reaction(context, target):
    # Try a different random reaction after a normal failure.
    # Keep the original reaction behavior while bounding simultaneous
    # reaction requests per bot via the semaphore in handle_incoming().
    reactions_to_try = random.sample(
        REACTION_EMOJIS,
        k=len(REACTION_EMOJIS)
    )

    for reaction in reactions_to_try:
        try:
            await context.bot.set_message_reaction(
                chat_id=target.chat_id,
                message_id=target.message_id,
                reaction=reaction,
                is_big=True
            )
            increment_metric(context, "reactions_sent")
            return

        except RetryAfter as e:
            wait_seconds = max(
                1,
                int(e.retry_after)
            )

            logger.warning(
                "Reaction rate limited; waiting %ss before retrying %s.",
                wait_seconds,
                reaction
            )

            await asyncio.sleep(
                wait_seconds
            )

            # A rate limit is not an unsupported-reaction error, so retry
            # the same emoji once after Telegram's requested wait.
            try:
                await context.bot.set_message_reaction(
                    chat_id=target.chat_id,
                    message_id=target.message_id,
                    reaction=reaction,
                    is_big=True
                )
                increment_metric(context, "reactions_sent")
                return
            except Exception as retry_error:
                logger.warning(
                    "Reaction retry failed for %s: %s",
                    reaction,
                    retry_error
                )
                continue

        except Exception as e:
            logger.warning(
                "Reaction failed for %s, trying another emoji: %s",
                reaction,
                e
            )
            continue

    increment_metric(context, "reaction_failures")

# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Unhandled Telegram error: %s",
        context.error,
        exc_info=context.error
    )


# =========================================================
# INITIAL FIREBASE SAVE
# =========================================================

async def save_state_for_app(app):
    firebase_ref = app.bot_data.get(
        "firebase_ref"
    )

    state = app.bot_data.get(
        "state"
    )

    if firebase_ref is None or state is None:
        return

    try:
        await asyncio.to_thread(
            firebase_ref.set,
            copy.deepcopy(state)
        )
    except Exception:
        logger.exception(
            "Initial Firebase state save failed."
        )


# =========================================================
# BUILD ONE BOT
# =========================================================

async def build_bot(
    token_index,
    token
):
    app = (
        Application.builder()
        .token(token)
        # Keep update handling bounded instead of allowing a burst to spawn
        # hundreds of simultaneous handler tasks for one bot.
        .concurrent_updates(4)
        # Reaction bots mostly make lightweight Bot API calls. Smaller pools
        # reduce idle per-bot resource usage while still allowing concurrency.
        .connection_pool_size(8)
        .pool_timeout(2.0)
        .get_updates_connection_pool_size(1)
        .build()
    )

    await app.initialize()

    me = await app.bot.get_me()

    bot_id = me.id
    bot_username = me.username or f"bot_{bot_id}"

    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("Could not register Telegram command menu for @%s", bot_username)

    # Each bot gets its own Firebase branch.
    firebase_ref = db.reference(
        f"reaction_bots/{bot_id}"
    )

    state = await load_state(
        firebase_ref,
        bot_id,
        bot_username
    )

    # Refresh current identity on every startup.
    state["bot_id"] = bot_id
    state["bot_username"] = bot_username

    app.bot_data["firebase_ref"] = firebase_ref
    app.bot_data["state"] = state
    app.bot_data["bot_id"] = int(bot_id)
    app.bot_data["save_lock"] = asyncio.Lock()
    app.bot_data["start_time"] = time.time()
    app.bot_data["token_index"] = token_index
    app.bot_data["reaction_semaphore"] = asyncio.Semaphore(3)
    app.bot_data["metrics_dirty"] = False
    app.bot_data["metrics_revision"] = 0
    state.setdefault("metrics", {})
    state["metrics"].setdefault("messages_seen", 0)
    state["metrics"].setdefault("reactions_sent", 0)
    state["metrics"].setdefault("reaction_failures", 0)

    await save_state_for_app(
        app
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("howtouse", howtouse_command))
    app.add_handler(CommandHandler("bots", bots_command))
    app.add_handler(CommandHandler("contact", contact_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    # Callback buttons
    app.add_handler(
        CallbackQueryHandler(
            how_to_use_callback,
            pattern=r"^how_to_use$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            all_channels_callback,
            pattern=r"^all_channels:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            all_groups_callback,
            pattern=r"^all_groups:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back_to_menu_callback,
            pattern=r"^back_to_menu$"
        )
    )


    app.add_handler(
        CallbackQueryHandler(
            more_bots_callback,
            pattern=r"^more_bots$"
        )
    )

    # Bot added/removed from a chat.
    app.add_handler(
        ChatMemberHandler(
            my_chat_member_handler,
            ChatMemberHandler.MY_CHAT_MEMBER
        )
    )

    # Channel posts + group/supergroup messages.
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL
            | filters.ChatType.GROUPS,
            handle_incoming
        )
    )

    # Private non-command messages use the fixed fallback reaction list.
    # Commands and existing channel/group auto-reactions are unaffected.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_private_non_command,
        )
    )

    app.add_error_handler(
        error_handler
    )

    return app


# =========================================================
# MANAGER BOT
# =========================================================


# =========================================================
# BROADCAST SYSTEM
# =========================================================

BROADCAST_DELETE_JOBS_PATH = "broadcast_delete_jobs"


def broadcast_delete_jobs_ref():
    return db.reference(BROADCAST_DELETE_JOBS_PATH)


def broadcast_delete_job_key(bot_id, chat_id, message_id):
    # Firebase keys cannot contain '/', '.', '#', '$', '[' or ']'.
    return f"{int(bot_id)}_{int(chat_id)}_{int(message_id)}"


async def save_broadcast_delete_job(bot_id, chat_id, message_id, delete_at):
    job = {
        "bot_id": int(bot_id),
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "delete_at": float(delete_at),
        "attempts": 0,
    }
    key = broadcast_delete_job_key(bot_id, chat_id, message_id)
    await asyncio.to_thread(
        broadcast_delete_jobs_ref().child(key).set,
        job,
    )
    return key


def _retry_after_seconds(exc):
    value = getattr(exc, "retry_after", 1)
    if hasattr(value, "total_seconds"):
        value = value.total_seconds()
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return 1.0


def _seen_signature(bot_id, chat_id, message_id):
    payload = f"{int(bot_id)}:{int(chat_id)}:{int(message_id)}".encode("utf-8")
    digest = hmac.new(
        TOKEN_ENCRYPTION_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


def make_seen_confirmation_url(bot_id, chat_id, message_id):
    if not MANAGER_BOT_USERNAME or not TOKEN_ENCRYPTION_SECRET:
        raise RuntimeError("Manager bot username or signing secret is unavailable.")
    signature = _seen_signature(bot_id, chat_id, message_id)
    payload = f"read_{int(bot_id)}_{int(chat_id)}_{int(message_id)}_{signature}"
    if len(payload) > 64:
        raise ValueError("Seen-confirmation deep-link payload exceeds Telegram's limit.")
    return f"https://t.me/{MANAGER_BOT_USERNAME}?start={payload}"


async def handle_seen_confirmation_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a recipient's signed 'I've Read' deep link in the Manager Bot."""
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return False
    if not context.args:
        return False

    match = re.fullmatch(r"read_(\d+)_(\d+)_(\d+)_([a-f0-9]{16})", context.args[0])
    if not match:
        return False

    bot_id, intended_chat_id, message_id = map(int, match.groups()[:3])
    supplied_signature = match.group(4)
    actual_chat_id = int(update.effective_user.id)
    if actual_chat_id != intended_chat_id:
        if update.message:
            await update.message.reply_text("This read-confirmation link belongs to another user.")
        return True

    expected_signature = _seen_signature(bot_id, intended_chat_id, message_id)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        if update.message:
            await update.message.reply_text("This read-confirmation link is invalid or expired.")
        return True

    try:
        await save_broadcast_delete_job(
            bot_id,
            intended_chat_id,
            message_id,
            time.time() + 60,
        )
        if update.message:
            await update.message.reply_text(
                "✅ Read confirmation received. The original message will be deleted in about 1 minute."
            )
    except Exception:
        logger.exception("Could not save read-confirmation deletion job from Manager Bot.")
        if update.message:
            await update.message.reply_text("Could not schedule deletion right now. Please try again.")
    return True


async def _send_one_bot_broadcast(bot, bot_id, users, message_text, kind, delay):
    """Send one bot's copy to only the users registered with that exact bot."""
    sent_count = 0
    failed_count = 0
    schedule_failed = 0
    seen_button_failed = 0
    valid_user_count = 0

    if not isinstance(users, dict):
        users = {}

    for user_key in list(users.keys()):
        try:
            chat_id = int(user_key)
            if chat_id <= 0:
                continue
            valid_user_count += 1
        except (TypeError, ValueError):
            continue

        delivered_message = None
        for attempt in range(4):
            try:
                delivered_message = await bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    disable_web_page_preview=True,
                )
                break
            except RetryAfter as exc:
                wait_for = _retry_after_seconds(exc)
                logger.warning(
                    "Broadcast rate limited by bot %s; waiting %.1fs (attempt %s).",
                    bot_id, wait_for, attempt + 1,
                )
                await asyncio.sleep(wait_for + 0.25)
            except Exception as exc:
                logger.info(
                    "Broadcast failed from bot %s to user %s: %s",
                    bot_id, chat_id, exc,
                )
                break

        if delivered_message is None:
            failed_count += 1
            continue

        sent_count += 1
        if kind == "seen":
            try:
                read_url = make_seen_confirmation_url(bot_id, chat_id, delivered_message.message_id)
                read_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👁 I've Read", url=read_url)]
                ])
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=delivered_message.message_id,
                    reply_markup=read_markup,
                )
            except Exception as exc:
                seen_button_failed += 1
                logger.info(
                    "Could not add Seen confirmation button for bot %s/user %s: %s",
                    bot_id, chat_id, exc,
                )

        if kind == "timed":
            try:
                await save_broadcast_delete_job(
                    bot_id,
                    chat_id,
                    delivered_message.message_id,
                    time.time() + max(1, int(delay)),
                )
            except Exception:
                schedule_failed += 1
                logger.exception(
                    "Broadcast delivered but auto-delete job could not be saved for bot %s/user %s.",
                    bot_id, chat_id,
                )

        # Conservative per-bot pacing; each worker has its own Telegram bot limits.
        await asyncio.sleep(0.05)

    return {
        "users": valid_user_count,
        "sent": sent_count,
        "failed": failed_count,
        "schedule_failed": schedule_failed,
        "seen_button_failed": seen_button_failed,
    }


async def send_broadcast_to_all_bots(message_text, kind, delay=0):
    """Broadcast separately from EVERY registered bot, including stopped workers.

    Recipient lists are read from each bot's existing reaction_bots/<id>/users
    Firebase data. Lists are deliberately not merged or deduplicated across bots.
    No legacy user/stat counter is modified.
    """
    registry = await get_managed_bots(force=True)
    result = {
        "bots_total": len(registry),
        "bots_processed": 0,
        "bots_failed": 0,
        "users_found": 0,
        "sent": 0,
        "failed": 0,
        "schedule_failed": 0,
        "seen_button_failed": 0,
        "bots_without_users": 0,
        "running_bots": 0,
        "stopped_bots": 0,
    }

    # Preserve the existing numbered-bot order where possible.
    records = []
    for registry_key, record in registry.items():
        if not isinstance(record, dict):
            result["bots_failed"] += 1
            continue
        try:
            bot_id = int(record.get("bot_id", registry_key))
        except (TypeError, ValueError):
            result["bots_failed"] += 1
            continue
        records.append((bot_id, registry_key, record))

    records.sort(key=lambda item: (
        reaction_bot_serial(item[2].get("bot_username"))
        if reaction_bot_serial(item[2].get("bot_username")) is not None else 10**9,
        str(item[2].get("bot_username", "")).lower(),
    ))

    for bot_id, registry_key, record in records:
        running_app = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
        if running_app:
            result["running_bots"] += 1
        else:
            result["stopped_bots"] += 1

        # Read only this bot's existing user map. Do not rewrite state or metrics.
        try:
            users = await asyncio.to_thread(
                db.reference(f"reaction_bots/{bot_id}/users").get
            )
        except Exception:
            logger.exception("Could not read user list for bot %s.", bot_id)
            result["bots_failed"] += 1
            continue

        if not isinstance(users, dict) or not users:
            result["bots_without_users"] += 1
            result["bots_processed"] += 1
            continue

        token = None
        temporary_bot = None
        try:
            if running_app:
                telegram_bot = running_app.bot
            else:
                encrypted_token = record.get("token")
                if not encrypted_token:
                    raise RuntimeError("No encrypted token is stored for this bot.")
                token = decrypt_token(encrypted_token)
                temporary_bot = Bot(token=token)
                await temporary_bot.initialize()
                telegram_bot = temporary_bot

            per_bot = await _send_one_bot_broadcast(
                telegram_bot,
                bot_id,
                users,
                message_text,
                kind,
                delay,
            )
            result["users_found"] += per_bot["users"]
            result["sent"] += per_bot["sent"]
            result["failed"] += per_bot["failed"]
            result["schedule_failed"] += per_bot["schedule_failed"]
            result["seen_button_failed"] += per_bot["seen_button_failed"]
            result["bots_processed"] += 1

        except Exception as exc:
            logger.exception("Broadcast setup failed for bot %s: %s", bot_id, exc)
            result["bots_failed"] += 1
        finally:
            if temporary_bot is not None:
                try:
                    await temporary_bot.shutdown()
                except Exception:
                    logger.exception("Temporary Bot API client shutdown failed for bot %s.", bot_id)

    return result


async def broadcast_delete_scheduler():
    """Retry due deletions from Firebase so Render restarts do not lose timers."""
    while True:
        try:
            jobs = await asyncio.to_thread(broadcast_delete_jobs_ref().get)
            if isinstance(jobs, dict) and jobs:
                registry = await get_managed_bots(force=True)
                now = time.time()
                for job_key, job in list(jobs.items()):
                    if not isinstance(job, dict):
                        await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        continue
                    try:
                        delete_at = float(job.get("delete_at", 0))
                        bot_id = int(job.get("bot_id"))
                        chat_id = int(job.get("chat_id"))
                        message_id = int(job.get("message_id"))
                    except (TypeError, ValueError):
                        await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        continue

                    next_attempt_at = float(job.get("next_attempt_at", 0) or 0)
                    if now < max(delete_at, next_attempt_at):
                        continue

                    record = registry.get(str(bot_id))
                    if not isinstance(record, dict):
                        # The managed bot was removed; there is no safe token left to use.
                        await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        continue

                    temp_bot = None
                    try:
                        running_app = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
                        if running_app:
                            telegram_bot = running_app.bot
                        else:
                            encrypted_token = record.get("token")
                            if not encrypted_token:
                                raise RuntimeError("No encrypted token for deletion job.")
                            temp_bot = Bot(token=decrypt_token(encrypted_token))
                            await temp_bot.initialize()
                            telegram_bot = temp_bot

                        await telegram_bot.delete_message(
                            chat_id=chat_id,
                            message_id=message_id,
                        )
                        await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).delete)

                    except Exception as exc:
                        error_text = str(exc).lower()
                        if any(term in error_text for term in (
                            "message to delete not found",
                            "message can't be deleted",
                            "message_id_invalid",
                            "chat not found",
                            "bot was blocked",
                        )):
                            await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        else:
                            attempts = int(job.get("attempts", 0) or 0) + 1
                            if attempts >= 12:
                                logger.error("Giving up broadcast deletion job %s after %s attempts: %s", job_key, attempts, exc)
                                await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                            else:
                                job["attempts"] = attempts
                                job["next_attempt_at"] = time.time() + min(300, 15 * attempts)
                                job["last_error"] = str(exc)[:300]
                                await asyncio.to_thread(broadcast_delete_jobs_ref().child(str(job_key)).update, job)
                                logger.warning("Broadcast deletion job %s will retry: %s", job_key, exc)
                    finally:
                        if temp_bot is not None:
                            try:
                                await temp_bot.shutdown()
                            except Exception:
                                pass

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Broadcast delete scheduler iteration failed.")

        await asyncio.sleep(10)


def manager_broadcast_menu():
    keyboard = [
        [InlineKeyboardButton("1️⃣ Normal Message", callback_data="mgr:bc:normal", style="success")],
        [InlineKeyboardButton("2️⃣ Auto Delete Message", callback_data="mgr:bc:timed", style="primary")],
        [InlineKeyboardButton("3️⃣ Seen & Delete Message", callback_data="mgr:bc:seen", style="primary")],
        [InlineKeyboardButton("‹ Main Menu", callback_data="mgr:home", style="primary")],
    ]
    return InlineKeyboardMarkup(keyboard)


def manager_broadcast_delay_menu():
    keyboard = [
        [InlineKeyboardButton("1 Hour", callback_data="mgr:bcdelay:3600"), InlineKeyboardButton("6 Hours", callback_data="mgr:bcdelay:21600")],
        [InlineKeyboardButton("12 Hours", callback_data="mgr:bcdelay:43200"), InlineKeyboardButton("24 Hours", callback_data="mgr:bcdelay:86400")],
        [InlineKeyboardButton("‹ Cancel", callback_data="mgr:home")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def manager_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    kind = query.data.rsplit(":", 1)[-1]
    if kind == "timed":
        MANAGER_BROADCAST_PENDING[OWNER_ID] = {"kind": "timed_wait_delay"}
        await query.message.edit_text(
            "⏱ <b>Choose auto-delete time</b>\n\nEvery managed Reaction Bot sends its own copy to its own users. Each copy is deleted after the selected time.",
            parse_mode="HTML",
            reply_markup=manager_broadcast_delay_menu(),
        )
        return

    if kind not in ("normal", "seen"):
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING[OWNER_ID] = {"kind": kind}
    description = (
        "Permanent message: it stays in each recipient's chat."
        if kind == "normal"
        else "Recipient presses ‘I've Read’; that bot deletes its copy 1 minute later. Telegram does not provide true read receipts."
    )
    await query.message.edit_text(
        f"📨 <b>{'NORMAL' if kind == 'normal' else 'SEEN & DELETE'} BROADCAST</b>\n\n{description}\n\nNow send the message text. Send /cancel to stop.",
        parse_mode="HTML",
    )


async def manager_broadcast_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    try:
        seconds = int(query.data.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return
    if seconds not in (3600, 21600, 43200, 86400):
        return
    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING[OWNER_ID] = {"kind": "timed", "delay": seconds}
    label = {3600: "1 hour", 21600: "6 hours", 43200: "12 hours", 86400: "24 hours"}[seconds]
    await query.message.edit_text(
        f"⏱ <b>Auto-delete: {label}</b>\n\nNow send the message text. Send /cancel to stop.",
        parse_mode="HTML",
    )


def manager_menu():
    keyboard = [
        [
            InlineKeyboardButton("➕ 𝐀𝐃𝐃 𝐑𝐄𝐀𝐂𝐓𝐈𝐎𝐍 𝐁𝐎𝐓", callback_data="mgr:add", style="success"),
            InlineKeyboardButton("🤖 𝐌𝐘 𝐁𝐎𝐓𝐒", callback_data="mgr:list:0", style="primary"),
        ],
        [
            InlineKeyboardButton("🔄 𝐑𝐄𝐒𝐓𝐀𝐑𝐓 𝐀𝐋𝐋", callback_data="mgr:restartall", style="primary"),
            InlineKeyboardButton("📊 𝐒𝐓𝐀𝐓𝐒", callback_data="mgr:stats", style="success"),
        ],
        [
            InlineKeyboardButton("📢 𝐁𝐑𝐎𝐀𝐃𝐂𝐀𝐒𝐓", callback_data="jrbc:open", style="success"),
        ],
        [
            InlineKeyboardButton("♻️ 𝐑𝐄𝐅𝐑𝐄𝐒𝐇", callback_data="mgr:home", style="primary"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def manager_home_text(first_name):
    safe_name = html.escape(first_name or "Owner")
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "      🌹 <b>⟨ 𝐉𝐑 ⟩ 𝐌𝐀𝐍𝐀𝐆𝐄𝐑</b> 🌹\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"𝐖𝐞𝐥𝐜𝐨𝐦𝐞, <b>{safe_name}</b> 👋\n\n"
        "⚡ <b>One Manager • Multiple Reaction Bots</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "➕ Add & launch Reaction Bots\n"
        "▶️ Start • ⏸ Stop • 🔄 Restart\n"
        "🗑 Remove bots from the manager\n"
        "📌 Each bot keeps its own identity & Firebase state.\n\n"
        "╰─ 𝐉𝐢𝐬𝐚𝐧𝐗 𝐑𝐞𝐚𝐜𝐭𝐢𝐨𝐧 𝐁𝐨𝐭 𝐒𝐲𝐬𝐭𝐞𝐦 🤖"
    )


def manager_is_owner(update):
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


async def manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # A signed read-confirmation link may come from any broadcast recipient,
    # including the Manager owner if their ID is also in a worker's user list.
    if await handle_seen_confirmation_start(update, context):
        return

    if not manager_is_owner(update):
        if update.message:
            await update.message.reply_text("⛔ This Manager Bot is owner only.")
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    first_name = update.effective_user.first_name if update.effective_user else "Owner"
    if update.message:
        await update.message.reply_text(
            manager_home_text(first_name),
            parse_mode="HTML",
            reply_markup=manager_menu(),
        )


async def manager_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    first_name = update.effective_user.first_name if update.effective_user else "Owner"
    msg = update.callback_query.message
    if not msg:
        return

    text = manager_home_text(first_name)
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=manager_menu())
    except Exception:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=manager_menu())


async def manager_broadcast_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the Broadcast Center immediately, without waiting for Firebase."""
    query = update.callback_query
    if query is None:
        return

    if not manager_is_owner(update):
        try:
            await query.answer("Owner only.", show_alert=True)
        except Exception:
            logger.exception("Could not answer non-owner broadcast callback.")
        return

    # Answer the callback first so Telegram does not leave the button spinning.
    try:
        await query.answer("Opening Broadcast Center…")
    except Exception:
        # Continue to display the menu even if Telegram considers the callback
        # query too old to acknowledge.
        logger.warning("Could not acknowledge Broadcast callback; displaying menu anyway.")

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>JR BROADCAST CENTER</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Choose how you want to send the announcement.\n\n"
        "🤖 Every registered Reaction Bot sends its own separate copy "
        "to the users saved for that bot.\n"
        "If someone has started 30 different bots, each of those 30 bots "
        "will send one separate message.\n\n"
        "Choose a message type below."
    )

    try:
        # Send a fresh message instead of editing the old menu. This avoids
        # edit-message failures on old/inaccessible Telegram messages.
        if query.message is not None:
            await query.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=manager_broadcast_menu(),
            )
        else:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=manager_broadcast_menu(),
            )
        logger.info("Broadcast Center opened by owner %s", OWNER_ID)
    except Exception as exc:
        logger.exception("Could not send Broadcast Center menu: %s", exc)
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "❌ Could not open Broadcast Center.\n"
                    "Please check the Render logs. You can also send /broadcast."
                ),
            )
        except Exception:
            logger.exception("Could not send Broadcast Center error notice.")


async def manager_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command fallback for opening the Broadcast Center."""
    if not manager_is_owner(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Owner only.")
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>JR BROADCAST CENTER</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Choose how you want to send the announcement.\n\n"
        "Each registered Reaction Bot sends its own separate copy "
        "to the users saved for that bot.\n\n"
        "Choose a message type below."
    )
    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=manager_broadcast_menu(),
        )


async def manager_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    MANAGER_PENDING_ADD.add(OWNER_ID)

    msg = update.callback_query.message
    if msg:
        await msg.edit_text(
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ✦ <b>ADD REACTION BOT</b> ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Send the <b>BotFather Bot Token</b> of the Reaction Bot you want to add.\n\n"
            "🔐 The token will be encrypted before it is stored.\n"
            "🗑️ After receiving it, the Manager will try to delete your token message.\n\n"
            "‹ Use /start to cancel."
            ,
            parse_mode="HTML",
        )


async def manager_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    message = update.effective_message
    if not message or not message.text:
        return

    incoming_text = message.text.strip()
    if not incoming_text:
        await message.reply_text("Please send a non-empty text message.")
        return

    if incoming_text.lower() == "/cancel":
        MANAGER_PENDING_ADD.discard(OWNER_ID)
        MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
        await message.reply_text("❎ Cancelled.", reply_markup=manager_menu())
        return

    pending_broadcast = MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    if pending_broadcast:
        kind = pending_broadcast.get("kind")
        if kind == "timed_wait_delay":
            MANAGER_BROADCAST_PENDING[OWNER_ID] = pending_broadcast
            await message.reply_text(
                "Choose the auto-delete time using the buttons above, or send /cancel."
            )
            return
        if kind not in ("normal", "timed", "seen"):
            await message.reply_text("Broadcast setup expired. Open Broadcast again.", reply_markup=manager_menu())
            return
        if len(incoming_text) > 4096:
            MANAGER_BROADCAST_PENDING[OWNER_ID] = pending_broadcast
            await message.reply_text("Telegram messages can contain up to 4096 characters. Please send a shorter message.")
            return

        delay = int(pending_broadcast.get("delay", 0) or 0)
        status_message = await message.reply_text(
            "⏳ Sending a separate copy from every registered Reaction Bot to that bot's own saved users..."
        )
        try:
            result = await send_broadcast_to_all_bots(incoming_text, kind, delay)
        except Exception:
            logger.exception("Broadcast operation failed.")
            await status_message.edit_text(
                "❌ Broadcast could not be completed. Check Render logs and Firebase connection."
            )
            return

        delay_label = {
            3600: "1 hour", 21600: "6 hours", 43200: "12 hours", 86400: "24 hours"
        }.get(delay, "Off")
        summary = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ✦ <b>BROADCAST FINISHED</b> ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🤖 <b>Registered bots:</b> <code>{result['bots_total']}</code>\n"
            f"🟢 <b>Bots processed:</b> <code>{result['bots_processed']}</code>\n"
            f"⏸ <b>Stopped bots included:</b> <code>{result['stopped_bots']}</code>\n"
            f"👥 <b>Recipient entries found:</b> <code>{result['users_found']}</code>\n"
            f"📨 <b>Messages delivered:</b> <code>{result['sent']}</code>\n"
            f"⚠️ <b>Failed deliveries:</b> <code>{result['failed']}</code>\n"
            f"🧩 <b>Bots with no saved users:</b> <code>{result['bots_without_users']}</code>\n"
            f"🔴 <b>Bot setup/read failures:</b> <code>{result['bots_failed']}</code>\n"
        )
        if kind == "timed":
            summary += f"⏱ <b>Auto-delete:</b> {html.escape(delay_label)}\n"
            summary += f"⚠️ <b>Delete-job save failures:</b> <code>{result['schedule_failed']}</code>\n"
        elif kind == "seen":
            summary += f"👁 <b>Read buttons unavailable:</b> <code>{result['seen_button_failed']}</code>\n"
            summary += (
                "\nℹ️ Seen & Delete uses the recipient's ‘I've Read’ button, not an automatic Telegram read receipt. "
                "The button opens the Manager Bot, so it also works when the sending Reaction Bot is stopped."
            )
        summary += "\n\nEach user's copy is sent by each individual bot they have started. Existing user counts and stats were not changed."
        try:
            await status_message.edit_text(summary, parse_mode="HTML", reply_markup=manager_menu())
        except Exception:
            await context.bot.send_message(OWNER_ID, summary, parse_mode="HTML", reply_markup=manager_menu())
        return

    if OWNER_ID not in MANAGER_PENDING_ADD:
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    token = incoming_text

    # Remove the secret from the Manager chat as soon as possible.
    try:
        await message.delete()
    except Exception:
        logger.warning("Could not delete the submitted token message.")

    if not token or ":" not in token:
        await context.bot.send_message(
            OWNER_ID,
            "❌ That does not look like a valid BotFather token. Try again with ➕ Add Reaction Bot."
        )
        return

    await context.bot.send_message(
        OWNER_ID,
        "⏳ Validating the bot token and preparing its Reaction Bot instance..."
    )

    worker_app = None
    bot_id = None
    try:
        worker_app = await build_bot("dynamic", token)
        bot_id = worker_app.bot.id
        bot_username = worker_app.bot.username or f"bot_{bot_id}"

        registry = await get_managed_bots()
        existing = registry.get(str(bot_id))
        if existing:
            await stop_application(worker_app)
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ @{bot_username} is already registered in the Manager."
            )
            return

        record = {
            "bot_id": bot_id,
            "bot_username": bot_username,
            "token": encrypt_token(token),
            "enabled": True,
            "added_by": OWNER_ID,
            "added_at": int(time.time()),
        }
        await save_managed_bot(bot_id, record)

        try:
            await start_application(worker_app)
            RUNNING_BOTS[bot_id] = worker_app
        except Exception:
            record["enabled"] = False
            await save_managed_bot(bot_id, record)
            await stop_application(worker_app)
            raise

        await context.bot.send_message(
            OWNER_ID,
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ✦ <b>BOT ADDED SUCCESSFULLY</b> ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🤖 <b>Bot:</b> @{html.escape(bot_username)}\n"
            f"🆔 <b>ID:</b> <code>{bot_id}</code>\n"
            "🟢 <b>Status:</b> Running\n\n"
            "Now add this bot to your Channel/Group and give it the same permissions required by your original Reaction Bot.",
            parse_mode="HTML",
            reply_markup=manager_menu(),
        )

    except Exception:
        if worker_app is not None:
            try:
                await stop_application(worker_app)
            except Exception:
                pass
        logger.exception("Failed to add managed bot.")
        await context.bot.send_message(
            OWNER_ID,
            "❌ Failed to add this bot. Make sure the BotFather token is correct and the Manager's Firebase/encryption settings are configured."
        )


async def build_manager_bot():
    if not MANAGER_BOT_TOKEN:
        raise RuntimeError("Missing environment variable: MANAGER_BOT_TOKEN")
    if not TOKEN_ENCRYPTION_SECRET:
        raise RuntimeError("Missing environment variable: TOKEN_ENCRYPTION_SECRET")

    app = (
        Application.builder()
        .token(MANAGER_BOT_TOKEN)
        .concurrent_updates(4)
        .connection_pool_size(8)
        .pool_timeout(2.0)
        .get_updates_connection_pool_size(1)
        .build()
    )

    await app.initialize()
    me = await app.bot.get_me()
    global MANAGER_BOT_USERNAME
    MANAGER_BOT_USERNAME = me.username or ""
    logger.info("Manager bot ready: @%s", me.username)

    app.add_handler(CommandHandler("start", manager_start))
    app.add_handler(CallbackQueryHandler(manager_add_callback, pattern=r"^mgr:add$"))
    app.add_handler(CallbackQueryHandler(manager_home_callback, pattern=r"^mgr:home$"))
    app.add_handler(CallbackQueryHandler(manager_list_callback, pattern=r"^mgr:list:\d+$"))
    app.add_handler(CallbackQueryHandler(manager_stats_callback, pattern=r"^mgr:stats$"))
    # Register the broadcast opener in an early handler group. Keep the old
    # callback value supported so already-sent Manager menus continue to work.
    app.add_handler(
        CallbackQueryHandler(
            manager_broadcast_home_callback,
            pattern=r"^(?:mgr:broadcast|jrbc:open)$",
        ),
        group=-1,
    )
    app.add_handler(CommandHandler("broadcast", manager_broadcast_command))
    app.add_handler(CallbackQueryHandler(manager_broadcast_callback, pattern=r"^mgr:bc:(normal|timed|seen)$"))
    app.add_handler(CallbackQueryHandler(manager_broadcast_delay_callback, pattern=r"^mgr:bcdelay:\d+$"))
    app.add_handler(CallbackQueryHandler(manager_restart_all_callback, pattern=r"^mgr:restartall$"))
    app.add_handler(CallbackQueryHandler(manager_bot_action_callback, pattern=r"^mgrbot:\d+:(start|stop|restart|remove)$"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, manager_text_handler))
    app.add_error_handler(error_handler)
    return app


async def start_application(app):
    await app.start()
    if app.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    await app.updater.start_polling(drop_pending_updates=True)


async def stop_application(app):
    pending_save = app.bot_data.get("state_save_task") if hasattr(app, "bot_data") else None
    if pending_save and not pending_save.done():
        pending_save.cancel()
        try:
            await pending_save
        except asyncio.CancelledError:
            pass

    # Preserve the latest analytics/state snapshot before stopping a bot.
    try:
        await save_state_for_app(app)
    except Exception:
        logger.exception("Final Firebase state save failed during shutdown.")

    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
    finally:
        if app.running:
            await app.stop()
        await app.shutdown()


async def flush_metrics_once(force=False):
    """Persist each bot's analytics separately, using one Firebase multi-path update.

    The data remains under reaction_bots/<bot_id>/metrics for each bot.  Nothing is
    aggregated into the Manager Bot.  A single root update reduces HTTP/Firebase
    overhead when many reaction bots are running on the same Render service.
    """
    updates = {}
    revisions = {}
    apps = []

    for bot_id, bot_app in list(RUNNING_BOTS.items()):
        if not getattr(bot_app, "bot_data", None):
            continue

        dirty = bool(bot_app.bot_data.get("metrics_dirty", False))
        if not force and not dirty:
            continue

        state = bot_app.bot_data.get("state")
        if not isinstance(state, dict):
            continue

        firebase_ref = bot_app.bot_data.get("firebase_ref")
        if firebase_ref is None:
            continue

        actual_bot_id = state.get("bot_id", bot_id)
        try:
            actual_bot_id = int(actual_bot_id)
        except (TypeError, ValueError):
            actual_bot_id = bot_id

        metrics = copy.deepcopy(state.get("metrics") or {})
        # Keep only numeric counters here; extra state remains untouched.
        clean_metrics = {}
        for key in ("messages_seen", "reactions_sent", "reaction_failures"):
            try:
                clean_metrics[key] = max(0, int(metrics.get(key, 0)))
            except (TypeError, ValueError):
                clean_metrics[key] = 0

        updates[f"reaction_bots/{actual_bot_id}/metrics"] = clean_metrics
        revisions[actual_bot_id] = int(
            bot_app.bot_data.get("metrics_revision", 0)
        )
        apps.append((actual_bot_id, bot_app))

    if not updates:
        return

    try:
        await asyncio.to_thread(
            db.reference("/").update,
            updates,
        )
    except Exception:
        logger.exception(
            "Periodic analytics Firebase flush failed for %d bot(s).",
            len(updates),
        )
        return

    for actual_bot_id, bot_app in apps:
        current_revision = int(
            bot_app.bot_data.get("metrics_revision", 0)
        )
        if current_revision == revisions.get(actual_bot_id):
            bot_app.bot_data["metrics_dirty"] = False

    logger.info(
        "Flushed separate analytics for %d reaction bot(s) to Firebase.",
        len(updates),
    )


async def metrics_flush_loop():
    """Flush changed bot metrics every 60 seconds without using the user's phone."""
    try:
        while True:
            await asyncio.sleep(METRICS_SAVE_INTERVAL)
            await flush_metrics_once()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Analytics flush loop stopped unexpectedly.")


async def start_managed_bot_from_record(record):
    token_value = record.get("token")
    if not token_value:
        raise RuntimeError("Managed bot record has no encrypted token.")

    token = decrypt_token(token_value)
    app = await build_bot("dynamic", token)
    bot_id = app.bot.id

    await start_application(app)
    RUNNING_BOTS[bot_id] = app
    return app


async def start_all_managed_bots():
    registry = await get_managed_bots(force=True)
    if not registry:
        logger.info("No managed reaction bots registered yet.")
        return

    records = []
    for bot_key, record in registry.items():
        if not isinstance(record, dict) or not record.get("enabled", False):
            continue
        record = copy.deepcopy(record)
        record.setdefault("bot_id", bot_key)
        records.append(record)

    def startup_sort_key(record):
        serial = reaction_bot_serial(record.get("bot_username"))
        if serial is None:
            return (1, str(record.get("bot_username", "")).lower())
        return (0, serial, str(record.get("bot_username", "")).lower())

    records.sort(key=startup_sort_key)
    semaphore = asyncio.Semaphore(MANAGED_BOT_START_CONCURRENCY)

    async def start_one(record):
        bot_id = record.get("bot_id")
        username = record.get("bot_username") or f"bot_{bot_id}"
        async with semaphore:
            try:
                app = await start_managed_bot_from_record(record)
                actual_id = app.bot.id
                actual_username = app.bot.username or f"bot_{actual_id}"
                if str(actual_id) != str(bot_id):
                    record["bot_id"] = actual_id
                    record["bot_username"] = actual_username
                    await save_managed_bot(actual_id, record)
                    if str(bot_id) != str(actual_id):
                        try:
                            await delete_managed_bot(bot_id)
                        except Exception:
                            pass
                logger.info("Managed reaction bot started: @%s", actual_username)
            except Exception:
                logger.exception("Failed to start managed bot %s", username)
                record["enabled"] = False
                try:
                    await save_managed_bot(bot_id, record)
                except Exception:
                    logger.exception("Failed to disable broken managed bot %s", bot_id)
            finally:
                await asyncio.sleep(MANAGED_BOT_START_DELAY)

    # Small bounded batches: faster than purely serial startup, but avoids
    # a large CPU/network spike on one Render instance.
    await asyncio.gather(*(start_one(record) for record in records))


async def stop_managed_bot(bot_id):
    app = RUNNING_BOTS.pop(int(bot_id), None)
    if app is None:
        app = RUNNING_BOTS.pop(str(bot_id), None)
    if app is not None:
        await stop_application(app)


async def render_bot_list(page=0):
    registry = await get_managed_bots()
    items = []
    for key, record in registry.items():
        if not isinstance(record, dict):
            continue
        bot_id = int(record.get("bot_id", key))
        username = record.get("bot_username") or f"bot_{bot_id}"
        enabled = bool(record.get("enabled", False))
        running = bot_id in RUNNING_BOTS
        items.append({
            "id": bot_id,
            "username": username,
            "enabled": enabled,
            "running": running,
        })

    def manager_sort_key(item):
        serial = reaction_bot_serial(item["username"])
        if serial is None:
            return (1, item["username"].lower())
        return (0, serial, item["username"].lower())

    items.sort(key=manager_sort_key)
    per_page = 5
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_items = items[page * per_page:(page + 1) * per_page]

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>MY REACTION BOTS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )
    if not items:
        text += "No Reaction Bots have been added yet."
    else:
        text += f"📊 <b>Total:</b> <code>{len(items)}</code>\n\n"
        for index, item in enumerate(page_items, start=page * per_page + 1):
            status = "🟢 Running" if item["running"] else ("🟡 Stopped" if item["enabled"] else "⚪ Disabled")
            serial = reaction_bot_serial(item["username"])
            serial_label = f"#{serial} • " if serial is not None else ""
            text += (
                f"<b>{index}. {serial_label}@{html.escape(item['username'])}</b>\n"
                f"   🆔 <code>{item['id']}</code> • {status}\n\n"
            )

    keyboard = []
    for item in page_items:
        action = "stop" if item["running"] else "start"
        label = "⏸ Stop" if item["running"] else "▶️ Start"
        keyboard.append([
            InlineKeyboardButton(
                f"@{item['username']} • {label}",
                callback_data=f"mgrbot:{item['id']}:{action}"
            )
        ])
        keyboard.append([
            InlineKeyboardButton("🔄 Restart", callback_data=f"mgrbot:{item['id']}:restart"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"mgrbot:{item['id']}:remove"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"mgr:list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"mgr:list:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([
        InlineKeyboardButton("➕ Add Bot", callback_data="mgr:add"),
        InlineKeyboardButton("‹ Main Menu", callback_data="mgr:home"),
    ])
    return text + (f"\n📄 <b>Page:</b> {page + 1}/{total_pages}" if items else ""), InlineKeyboardMarkup(keyboard)


async def manager_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    await update.callback_query.answer()
    try:
        page = int(update.callback_query.data.split(":")[-1])
    except Exception:
        page = 0
    text, markup = await render_bot_list(page)
    msg = update.callback_query.message
    if msg:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def manager_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    await update.callback_query.answer()

    registry = await get_managed_bots()
    total = len(registry)
    running = sum(1 for key, r in registry.items() if isinstance(r, dict) and int(r.get("bot_id", key)) in RUNNING_BOTS)
    enabled = sum(1 for r in registry.values() if isinstance(r, dict) and r.get("enabled", False))

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>MANAGER STATISTICS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🤖 <b>Total Bots:</b> <code>{total}</code>\n"
        f"🟢 <b>Running:</b> <code>{running}</code>\n"
        f"🟡 <b>Enabled:</b> <code>{enabled}</code>\n"
        f"⚪ <b>Stopped/Disabled:</b> <code>{max(0, total - running)}</code>\n"
    )
    msg = update.callback_query.message
    if msg:
        await msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("‹ Main Menu", callback_data="mgr:home")]
            ]),
        )


async def manager_restart_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer("Refreshing all managed bots…")
    msg = update.callback_query.message
    if not msg:
        return

    registry = await get_managed_bots(force=True)
    records = [
        record for record in registry.values()
        if isinstance(record, dict)
        and record.get("enabled", False)
        and record.get("token")
    ]
    records.sort(
        key=lambda record: (
            reaction_bot_serial(record.get("bot_username"))
            if reaction_bot_serial(record.get("bot_username")) is not None
            else 10**9,
            str(record.get("bot_username", "")).lower(),
        )
    )

    if not records:
        await msg.reply_text(
            "ℹ️ No enabled managed bots to restart.",
            reply_markup=manager_menu(),
        )
        return

    await msg.reply_text(
        f"🔄 <b>Refreshing {len(records)} managed bot(s)…</b>\n"
        "The manager will do this one-by-one so the Render service is not hit by a startup burst.",
        parse_mode="HTML",
    )

    success = 0
    failed = []
    for record in records:
        bot_id = int(record.get("bot_id"))
        username = record.get("bot_username") or f"bot_{bot_id}"
        try:
            await stop_managed_bot(bot_id)
            app = await start_managed_bot_from_record(record)
            RUNNING_BOTS[bot_id] = app
            success += 1
        except Exception as exc:
            logger.exception("Restart-all failed for %s", username)
            failed.append(username)
            record["enabled"] = False
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                pass

        await asyncio.sleep(MANAGED_BOT_START_DELAY)

    result = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>ALL BOTS REFRESHED</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🟢 <b>Ready:</b> <code>{success}</code>\n"
        f"🔴 <b>Failed:</b> <code>{len(failed)}</code>"
    )
    if failed:
        result += "\n\n❌ " + ", ".join("@" + str(x) for x in failed)

    await msg.reply_text(
        result,
        parse_mode="HTML",
        reply_markup=manager_menu(),
    )


async def manager_bot_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    bot_id = int(parts[1])
    action = parts[2]
    registry = await get_managed_bots()
    record = registry.get(str(bot_id))
    if not record:
        await update.callback_query.message.reply_text("❌ Bot record not found.")
        return

    username = record.get("bot_username") or f"bot_{bot_id}"

    try:
        if action == "start":
            if bot_id in RUNNING_BOTS:
                await update.callback_query.message.reply_text(f"ℹ️ @{username} is already running.")
            else:
                app = await start_managed_bot_from_record(record)
                RUNNING_BOTS[bot_id] = app
                record["enabled"] = True
                await save_managed_bot(bot_id, record)
                await update.callback_query.message.reply_text(f"🟢 @{username} started.")

        elif action == "stop":
            await stop_managed_bot(bot_id)
            record["enabled"] = False
            await save_managed_bot(bot_id, record)
            await update.callback_query.message.reply_text(f"⏸ @{username} stopped.")

        elif action == "restart":
            await stop_managed_bot(bot_id)
            app = await start_managed_bot_from_record(record)
            RUNNING_BOTS[bot_id] = app
            record["enabled"] = True
            await save_managed_bot(bot_id, record)
            await update.callback_query.message.reply_text(f"🔄 @{username} restarted.")

        elif action == "remove":
            await stop_managed_bot(bot_id)
            await delete_managed_bot(bot_id)
            await update.callback_query.message.reply_text(
                f"🗑 @{username} removed from the Manager.\n\nFirebase reaction state is kept under reaction_bots/{bot_id}."
            )

    except Exception:
        logger.exception("Manager action failed for bot %s", bot_id)
        if action in ("start", "restart"):
            record["enabled"] = False
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                pass
        await update.callback_query.message.reply_text(
            f"❌ Could not {action} @{username}. Check the Render logs for the exact Telegram/Firebase error."
        )


# =========================================================
# MAIN
# =========================================================

async def main():
    Thread(
        target=run_server,
        daemon=True
    ).start()

    try:
        init_firebase()
    except Exception:
        logger.exception("Firebase initialization failed.")
        return

    if not MANAGER_BOT_TOKEN:
        logger.error("Missing MANAGER_BOT_TOKEN.")
        return

    if not TOKEN_ENCRYPTION_SECRET:
        logger.error("Missing TOKEN_ENCRYPTION_SECRET.")
        return

    startup_task = None
    metrics_flush_task = None
    broadcast_scheduler_task = None
    try:
        manager = await build_manager_bot()
        # Start the Manager first so it stays usable while existing Reaction Bots
        # are restored in the background with a small concurrency limit.
        await start_application(manager)
        startup_task = asyncio.create_task(
            start_all_managed_bots(),
            name="managed-bot-startup"
        )
        metrics_flush_task = asyncio.create_task(
            metrics_flush_loop(),
            name="metrics-flush-loop"
        )
        broadcast_scheduler_task = asyncio.create_task(
            broadcast_delete_scheduler(),
            name="broadcast-delete-scheduler"
        )

        logger.info(
            "Manager running; restoring managed reaction bots in background."
        )

        await asyncio.Event().wait()

    finally:
        if metrics_flush_task and not metrics_flush_task.done():
            metrics_flush_task.cancel()
            try:
                await metrics_flush_task
            except asyncio.CancelledError:
                pass

        if broadcast_scheduler_task and not broadcast_scheduler_task.done():
            broadcast_scheduler_task.cancel()
            try:
                await broadcast_scheduler_task
            except asyncio.CancelledError:
                pass

        # Best-effort final per-bot analytics flush before workers stop.
        try:
            await flush_metrics_once(force=True)
        except Exception:
            logger.exception("Final analytics flush failed.")
        if startup_task and not startup_task.done():
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass

        for bot_id in list(RUNNING_BOTS.keys()):
            try:
                await stop_managed_bot(bot_id)
            except Exception:
                logger.exception(
                    "Failed to stop managed bot %s",
                    bot_id
                )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
