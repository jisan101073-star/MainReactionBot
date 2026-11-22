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
import math
import signal
from threading import Thread

from flask import Flask

import firebase_admin
from firebase_admin import credentials, db

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, Update
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    TypeHandler,
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

# Optional Group Manager (separate file: group_manager.py). If that file is
# missing or broken, the Manager Bot just runs without the GROUP MANAGER button.
try:
    import group_manager
except Exception:
    group_manager = None
    logger.exception("group_manager.py could not be imported; Group Manager is disabled.")
GROUP_MANAGER_READY = False

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "").strip() or "8223664417")
except ValueError:
    OWNER_ID = 8223664417

IMAGE_URL = "https://telegra.ph/file/07f45aef0cc6323c21c78.jpg"
SUPPORT_URL = "https://t.me/Jr_Auto_ReactionBot"


# =========================================================
# REACTIONS
# =========================================================

REACTION_EMOJIS = [
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
    "🤷", "🤷‍♀", "😡"
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
MANAGER_APP = None

# Per-worker lifecycle locks prevent two concurrent Manager/startup actions
# from creating duplicate polling sessions for the same Telegram bot.
BOT_LIFECYCLE_LOCKS = {}
# A second lock is keyed by the bot ID returned by Telegram itself. This also
# prevents duplicate polling if malformed legacy records refer to the same
# token under different registry keys.
BOT_START_LOCKS = {}
DETACHED_SHUTDOWN_TASKS = set()

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
SHUTDOWN_STOP_TIMEOUT = 20.0
SHUTDOWN_MANAGER_RESERVE = 4.0
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


async def get_managed_bots(force=False, require_fresh=False):
    global MANAGED_BOTS_CACHE, MANAGED_BOTS_CACHE_EXPIRES

    now = time.monotonic()
    if (
        not force
        and MANAGED_BOTS_CACHE is not None
        and now < MANAGED_BOTS_CACHE_EXPIRES
    ):
        return copy.deepcopy(MANAGED_BOTS_CACHE)

    try:
        data = await _firebase_call_with_retry(managed_bots_ref().get)
    except Exception:
        logger.exception("Failed to read managed bot registry after retries.")
        if MANAGED_BOTS_CACHE is not None and not require_fresh:
            logger.warning("Using the last cached managed-bot registry because Firebase is unavailable.")
            return copy.deepcopy(MANAGED_BOTS_CACHE)
        raise

    if data is not None and not isinstance(data, dict):
        raise RuntimeError("Firebase managed_bots registry is not a JSON object.")
    if data is None:
        data = {}

    MANAGED_BOTS_CACHE = copy.deepcopy(data)
    MANAGED_BOTS_CACHE_EXPIRES = now + MANAGED_BOTS_CACHE_TTL
    return copy.deepcopy(data)


def invalidate_managed_bots_cache():
    global MANAGED_BOTS_CACHE_EXPIRES
    MANAGED_BOTS_CACHE_EXPIRES = 0.0


def get_bot_lifecycle_lock(bot_id):
    """Return one stable asyncio lock for each managed registry id."""
    normalized_id = int(bot_id)
    lock = BOT_LIFECYCLE_LOCKS.get(normalized_id)
    if lock is None:
        lock = asyncio.Lock()
        BOT_LIFECYCLE_LOCKS[normalized_id] = lock
    return lock


def get_bot_start_lock(bot_id):
    """Serialize actual Telegram bot startup by the identity returned by getMe."""
    normalized_id = int(bot_id)
    lock = BOT_START_LOCKS.get(normalized_id)
    if lock is None:
        lock = asyncio.Lock()
        BOT_START_LOCKS[normalized_id] = lock
    return lock


async def save_managed_bot(bot_id, data):
    await _firebase_call_with_retry(
        managed_bots_ref().child(str(bot_id)).set,
        data,
    )
    invalidate_managed_bots_cache()


async def delete_managed_bot(bot_id):
    await _firebase_call_with_retry(
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
        "reaction_chat_pauses": {},
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

    reaction_chat_pauses = data.get("reaction_chat_pauses", {})

    if not isinstance(users, dict):
        users = {}

    if not isinstance(channels, dict):
        channels = {}

    if not isinstance(groups, dict):
        groups = {}

    if not isinstance(reaction_chat_pauses, dict):
        reaction_chat_pauses = {}

    metrics = data.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    def metric_int(name):
        try:
            value = int(metrics.get(name, 0))
            return max(0, value)
        except (TypeError, ValueError, OverflowError):
            return 0

    # Keep unknown top-level and metric keys for forward compatibility. The
    # current bot updates only its known fields, but an older/newer deployment
    # may have stored additional metadata that must not be silently discarded.
    normalized = copy.deepcopy(data)
    normalized_metrics = copy.deepcopy(metrics)
    normalized.update({
        "bot_id": data.get("bot_id", bot_id),
        "bot_username": data.get("bot_username", bot_username),
        "users": users,
        "channels": channels,
        "groups": groups,
        "reaction_chat_pauses": reaction_chat_pauses,
    })
    normalized_metrics.update({
        "messages_seen": metric_int("messages_seen"),
        "reactions_sent": metric_int("reactions_sent"),
        "reaction_failures": metric_int("reaction_failures"),
    })
    normalized["metrics"] = normalized_metrics
    return normalized


async def load_state(
    firebase_ref,
    bot_id,
    bot_username
):
    try:
        data = await _firebase_call_with_retry(firebase_ref.get)
    except Exception as exc:
        logger.exception(
            "Firebase state read failed for bot @%s; refusing to start with an empty state.",
            bot_username or "unknown",
        )
        raise RuntimeError(
            f"Could not safely load Firebase state for @{bot_username or 'unknown'}; refusing to overwrite it."
        ) from exc

    if data is not None and not isinstance(data, dict):
        raise RuntimeError(
            f"Firebase state for @{bot_username or 'unknown'} is not a JSON object; refusing to overwrite it."
        )
    if isinstance(data, dict):
        for collection_name in ("users", "channels", "groups", "reaction_chat_pauses"):
            collection_value = data.get(collection_name)
            if collection_value is not None and not isinstance(collection_value, dict):
                raise RuntimeError(
                    f"Firebase state field '{collection_name}' for @{bot_username or 'unknown'} is malformed; refusing to overwrite it."
                )
        metrics_value = data.get("metrics")
        if metrics_value is not None and not isinstance(metrics_value, dict):
            raise RuntimeError(
                f"Firebase state field 'metrics' for @{bot_username or 'unknown'} is malformed; refusing to overwrite it."
            )

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


async def _run_thread_to_completion(func, *args, **kwargs):
    """Keep caller-held locks until an asyncio.to_thread call really ends."""
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    was_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop the OS thread. Keep waiting
            # even if shutdown sends cancellation more than once, otherwise
            # a second cancellation could release the lock before Firebase
            # finishes its write.
            was_cancelled = True
        except Exception:
            # The worker task is finished with an exception; task.result()
            # below will propagate it unless the caller was cancelled.
            break

    if was_cancelled:
        try:
            task.result()
        except Exception:
            logger.exception("Firebase worker thread failed during cancellation.")
        raise asyncio.CancelledError
    return task.result()


async def _firebase_call_with_retry(func, *args, attempts=3, base_delay=0.5, **kwargs):
    """Run an idempotent Firebase operation in a thread with bounded retries."""
    attempts = max(1, int(attempts))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await _run_thread_to_completion(func, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            try:
                configured_delay = float(base_delay)
                if not math.isfinite(configured_delay):
                    configured_delay = 0.5
            except (TypeError, ValueError, OverflowError):
                configured_delay = 0.5
            wait_seconds = min(4.0, max(0.1, configured_delay) * (2 ** (attempt - 1)))
            logger.warning(
                "Firebase operation failed (attempt %s/%s); retrying in %.1fs: %s",
                attempt, attempts, wait_seconds, exc,
            )
            await asyncio.sleep(wait_seconds)
    raise RuntimeError(f"Firebase operation failed after {attempts} attempts: {last_error}") from last_error


async def save_state(context):
    bot_data = context.application.bot_data
    firebase_ref = bot_data.get("firebase_ref")
    state = bot_data.get("state")
    lock = bot_data.get("save_lock")

    if firebase_ref is None or state is None or lock is None:
        logger.error("Cannot save state: Firebase reference, state, or save lock is missing.")
        return False

    try:
        async with lock:
            payload = copy.deepcopy(state)
            await _firebase_call_with_retry(firebase_ref.set, payload)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Firebase save failed.")
        return False


async def schedule_state_save(context, delay=1.25):
    """Debounce writes, while guaranteeing changes during a write get another flush."""
    bot_data = context.application.bot_data
    bot_data["state_save_revision"] = int(bot_data.get("state_save_revision", 0)) + 1
    existing = bot_data.get("state_save_task")
    if existing and not existing.done():
        return

    async def _flush():
        retry_delay = max(0.5, float(delay))
        try:
            failed_attempts = 0
            while True:
                requested_revision = int(bot_data.get("state_save_revision", 0))
                await asyncio.sleep(retry_delay)
                saved = await save_state(context)
                current_revision = int(bot_data.get("state_save_revision", 0))
                if saved and current_revision == requested_revision:
                    return
                if saved:
                    # New state arrived during the preceding Firebase write.
                    failed_attempts = 0
                    retry_delay = max(0.5, float(delay))
                else:
                    failed_attempts += 1
                    if failed_attempts >= 5:
                        logger.error("Debounced Firebase save stopped after 5 failed attempts; a later state change can retry it.")
                        return
                    retry_delay = min(10.0, max(2.0, retry_delay * 2.0))
                    logger.warning("Retrying debounced Firebase state save in %.1fs.", retry_delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Debounced Firebase save loop failed.")
        finally:
            if bot_data.get("state_save_task") is asyncio.current_task():
                bot_data["state_save_task"] = None

    bot_data["state_save_task"] = asyncio.create_task(
        _flush(), name="firebase-state-save"
    )


def increment_metric(context, name, amount=1):
    """Update per-bot analytics in RAM; a shared 60-second flusher persists it."""
    state = context.application.bot_data.get("state")
    if not isinstance(state, dict):
        return

    metrics = state.setdefault("metrics", {})
    try:
        metrics[name] = max(0, int(metrics.get(name, 0)) + int(amount))
    except (TypeError, ValueError, OverflowError):
        try:
            metrics[name] = max(0, int(amount))
        except (TypeError, ValueError, OverflowError):
            metrics[name] = 0

    # Revision lets the periodic flusher detect updates that happen while a
    # Firebase write is in progress, so no fresh metric increment is lost.
    context.application.bot_data["metrics_dirty"] = True
    context.application.bot_data["metrics_revision"] = int(
        context.application.bot_data.get("metrics_revision", 0)
    ) + 1


def get_metric_int(state, name):
    try:
        return max(0, int((state.get("metrics") or {}).get(name, 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def format_compact_number(value):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
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
    now = time.time()
    start_time = context.application.bot_data.get("start_time", now)
    try:
        uptime_seconds = max(0, int(now - float(start_time)))
    except (TypeError, ValueError, OverflowError):
        # Runtime metadata is local to this process; malformed or stale values
        # should not make the owner's /stats command fail.
        uptime_seconds = 0

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

    firebase_ok = True
    try:
        registry = await get_managed_bots(force=True, require_fresh=True)
    except Exception:
        firebase_ok = False
        logger.exception("Could not load a fresh registry for /status; showing cached data if available.")
        registry = copy.deepcopy(MANAGED_BOTS_CACHE) if isinstance(MANAGED_BOTS_CACHE, dict) else {}

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
    running_ids = {
        str(bot_id) for bot_id, worker_app in RUNNING_BOTS.items()
        if _worker_is_running(worker_app)
    }
    configured_enabled_ids = set(enabled_records.keys())
    running_count = len(running_ids & configured_enabled_ids)
    stopped_count = max(0, len(configured_enabled_ids) - running_count)
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
            if not await save_state(context):
                await schedule_state_save(context, delay=0.5)

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
    channels_data = state.get("channels", {})
    channels = [
        record for record in channels_data.values()
        if isinstance(record, dict)
    ] if isinstance(channels_data, dict) else []

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
    groups_data = state.get("groups", {})
    groups = [
        record for record in groups_data.values()
        if isinstance(record, dict)
    ] if isinstance(groups_data, dict) else []

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

    state = context.application.bot_data.get("state", {})
    app = context.application
    updater = getattr(app, "updater", None)
    polling_is_active = bool(
        getattr(app, "running", False)
        and getattr(updater, "running", False)
    )
    server_status = (
        "Polling Active 🟢" if polling_is_active
        else "Polling Inactive ⚠️"
    )

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
        f"<code>{server_status}</code>\n"
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

    # Telegram sends MY_CHAT_MEMBER when this worker's access changes. Clear
    # any long permission pause so the next eligible message can be evaluated
    # immediately instead of waiting for the old pause to expire.
    pause_map = state.setdefault("reaction_chat_pauses", {})
    pause_removed = pause_map.pop(str(chat.id), None) is not None
    reaction_cache = context.application.bot_data.get("chat_reactions_cache", {})
    reaction_cache.pop(chat.id, None)

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

        if (changed or pause_removed) and not await save_state(context):
            await schedule_state_save(context, delay=0.5)

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

        if (changed or pause_removed) and not await save_state(context):
            await schedule_state_save(context, delay=0.5)

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
    """Try one fallback emoji at a time; rate limits and permission errors are bounded."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type != "private":
        return
    if not message.text or message.text.startswith("/"):
        return

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
            wait_seconds = _retry_after_seconds(exc)
            _set_reaction_global_cooldown(context, wait_seconds + 0.25)
            logger.warning(
                "Private fallback reaction was rate limited; pausing this worker for %.1fs.",
                wait_seconds,
            )
            return
        except Exception as exc:
            if _is_fatal_reaction_error(exc):
                logger.info("Private fallback reaction is not permitted: %s", exc)
                return
            logger.info(
                "Private fallback reaction %s failed for message %s: %s",
                emoji, message.message_id, exc,
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
        reaction_semaphore = asyncio.Semaphore(1)
        context.application.bot_data["reaction_semaphore"] = reaction_semaphore

    async with reaction_semaphore:
        await _perform_reaction(context, target)


REACTION_CHAT_CACHE_TTL = 600.0
REACTION_CHAT_CACHE_FALLBACK_TTL = 30.0
REACTION_TRANSIENT_COOLDOWN = 12.0
REACTION_PERMISSION_PAUSE_SECONDS = 3 * 24 * 60 * 60
REACTION_MAX_TRIES_PER_MESSAGE = 8


def _strip_vs(value):
    return str(value or "").replace("\ufe0f", "")


async def _allowed_reactions_for_chat(context, chat_id):
    """Return supported standard emoji based on Telegram chat metadata.

    Telegram documents that an omitted ``available_reactions`` means all
    standard emoji reactions are allowed. python-telegram-bot 22.x converts an
    omitted optional list to an empty tuple during JSON deserialization, so an
    empty tuple/list is treated as that default. PTB does not preserve enough
    information here to distinguish an omitted field from an explicit empty
    list; Telegram remains the final authority and rejected reactions are
    handled by the normal permission/error path.
    """
    cache = context.application.bot_data.setdefault("chat_reactions_cache", {})
    now = time.monotonic()
    cached = cache.get(chat_id)
    if cached and now < cached[0]:
        return list(cached[1])

    try:
        chat = await context.bot.get_chat(chat_id)
        available = getattr(chat, "available_reactions", None)

        if available is None or (
            isinstance(available, (list, tuple)) and not available
        ):
            # PTB's de_list_optional() maps an omitted API field to ().
            allowed = list(REACTION_EMOJIS)
        elif isinstance(available, (list, tuple)):
            allowed_by_normalized = {}
            for item in available:
                advertised_emoji = getattr(item, "emoji", None)
                if not advertised_emoji and isinstance(item, str):
                    advertised_emoji = item
                if advertised_emoji:
                    allowed_by_normalized.setdefault(
                        _strip_vs(advertised_emoji), advertised_emoji
                    )
            allowed = []
            seen_allowed = set()
            for candidate in REACTION_EMOJIS:
                exact_emoji = allowed_by_normalized.get(_strip_vs(candidate))
                if exact_emoji and exact_emoji not in seen_allowed:
                    allowed.append(exact_emoji)
                    seen_allowed.add(exact_emoji)
        else:
            # An unknown metadata shape is not a verified empty allow-list.
            logger.warning(
                "Unexpected available_reactions type for chat %s: %s; using a short-lived fallback.",
                chat_id,
                type(available).__name__,
            )
            allowed = list(REACTION_EMOJIS)
            cache[chat_id] = (now + REACTION_CHAT_CACHE_FALLBACK_TTL, allowed)
            return allowed
    except Exception as exc:
        logger.info("Could not read available reactions for chat %s: %s", chat_id, exc)
        if len(cache) > 2000:
            cache.clear()
        fallback = list(REACTION_EMOJIS)
        cache[chat_id] = (now + REACTION_CHAT_CACHE_FALLBACK_TTL, fallback)
        return fallback

    if len(cache) > 2000:
        cache.clear()
    cache[chat_id] = (now + REACTION_CHAT_CACHE_TTL, list(allowed))
    return allowed


REACTION_CHAT_PERMISSION_ERROR_TERMS = (
    "not enough rights",
    "no rights",
    "administrator rights",
    "not a member",
    "was kicked",
    "bot was blocked",
    "have no rights",
    "chat_admin_required",
    "chat_write_forbidden",
    "bot is not a member",
    "not allowed to send reactions",
    "not allowed to react",
    "reactions are disabled",
    "reactions disabled",
    "reactions not allowed",
    "reactions are not allowed",
    "chat_reactions_forbidden",
    "message_reactions_forbidden",
    "reaction not allowed",
)

REACTION_MESSAGE_ERROR_TERMS = (
    "chat not found",
    "message to react not found",
    "message not found",
    "message can't be reacted",
    "message cannot be reacted",
    "message_id_invalid",
)


def _is_chat_permission_reaction_error(error):
    if isinstance(error, Forbidden):
        return True
    text = str(error).lower()
    return any(term in text for term in REACTION_CHAT_PERMISSION_ERROR_TERMS)


def _is_message_target_reaction_error(error):
    text = str(error).lower()
    return any(term in text for term in REACTION_MESSAGE_ERROR_TERMS)


def _is_fatal_reaction_error(error):
    """Classify permission/target errors that cannot be fixed by another emoji."""
    return (
        _is_chat_permission_reaction_error(error)
        or _is_message_target_reaction_error(error)
    )


async def _pause_chat_reactions(context, chat_id, seconds, reason, error=None):
    """Pause one chat; long permission pauses survive a worker restart."""
    bot_data = context.application.bot_data
    expires_at = time.time() + max(1.0, float(seconds))
    bot_data.setdefault("reaction_cooldowns", {})[chat_id] = time.monotonic() + max(1.0, float(seconds))

    if float(seconds) >= REACTION_PERMISSION_PAUSE_SECONDS:
        state = bot_data.get("state")
        if isinstance(state, dict):
            pauses = state.setdefault("reaction_chat_pauses", {})
            pauses[str(chat_id)] = {
                "until": expires_at,
                "reason": str(reason)[:80],
                "last_error": str(error or "")[:240],
            }
            await schedule_state_save(context, delay=0.5)


def _set_reaction_global_cooldown(context, seconds):
    context.application.bot_data["reaction_global_cooldown_until"] = (
        time.monotonic() + max(1.0, float(seconds))
    )


async def _perform_reaction(context, target):
    """Try permitted emoji candidates while respecting chat and bot-level errors."""
    bot_data = context.application.bot_data
    chat_id = int(target.chat_id)
    state = bot_data.get("state") or {}

    global_pause = float(bot_data.get("reaction_global_cooldown_until", 0) or 0)
    if time.monotonic() < global_pause:
        increment_metric(context, "reaction_failures")
        return

    # Persistent permission pause; it expires automatically, and chat-member
    # updates clear it immediately if Telegram reports the bot's access changed.
    pauses = state.setdefault("reaction_chat_pauses", {}) if isinstance(state, dict) else {}
    pause_key = str(chat_id)
    pause_record = pauses.get(pause_key)
    if isinstance(pause_record, dict):
        try:
            pause_until = float(pause_record.get("until", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            pause_until = 0.0
        if pause_until > time.time():
            increment_metric(context, "reaction_failures")
            return
        pauses.pop(pause_key, None)
        await schedule_state_save(context, delay=0.5)

    cooldowns = bot_data.setdefault("reaction_cooldowns", {})
    cooldown_until = cooldowns.get(chat_id)
    if cooldown_until and time.monotonic() < cooldown_until:
        increment_metric(context, "reaction_failures")
        return
    if len(cooldowns) > 2000:
        cooldowns.clear()

    allowed_emojis = await _allowed_reactions_for_chat(context, chat_id)
    if not allowed_emojis:
        # Explicit empty list (or a list containing only custom emoji types)
        # means no supported standard emoji is currently available.
        await _pause_chat_reactions(
            context,
            chat_id,
            REACTION_PERMISSION_PAUSE_SECONDS,
            "no_standard_reactions_allowed",
            "Telegram returned an empty/unsupported standard reaction allow-list.",
        )
        increment_metric(context, "reaction_failures")
        return

    reactions_to_try = random.sample(
        allowed_emojis,
        k=min(len(allowed_emojis), REACTION_MAX_TRIES_PER_MESSAGE),
    )

    for reaction in reactions_to_try:
        try:
            await context.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=target.message_id,
                reaction=[ReactionTypeEmoji(emoji=reaction)],
                is_big=True,
            )
            increment_metric(context, "reactions_sent")
            return
        except RetryAfter as exc:
            # RetryAfter is a bot/API rate limit, not an unsupported emoji.
            # Honor Telegram's exact wait without holding this update handler.
            wait_seconds = _retry_after_seconds(exc)
            _set_reaction_global_cooldown(context, wait_seconds + 0.25)
            logger.warning(
                "Reaction rate limited for worker %s; pausing this bot's reaction requests for %.1fs.",
                bot_data.get("bot_id", "unknown"),
                wait_seconds,
            )
            increment_metric(context, "reaction_failures")
            return
        except Exception as exc:
            if _is_chat_permission_reaction_error(exc):
                logger.warning(
                    "Chat-level reaction permission denied in chat %s; pausing this chat for %s hours: %s",
                    chat_id,
                    REACTION_PERMISSION_PAUSE_SECONDS / 3600,
                    exc,
                )
                await _pause_chat_reactions(
                    context,
                    chat_id,
                    REACTION_PERMISSION_PAUSE_SECONDS,
                    "telegram_chat_permission_denied",
                    exc,
                )
                increment_metric(context, "reaction_failures")
                return

            if _is_message_target_reaction_error(exc):
                # A missing/unsupported message is not a chat-wide permission error.
                logger.info("Message %s in chat %s cannot be reacted to: %s", target.message_id, chat_id, exc)
                increment_metric(context, "reaction_failures")
                return

            if isinstance(exc, BadRequest):
                # A normal invalid/unsupported emoji response can be fixed by
                # trying a different candidate from the allowed list.
                logger.info("Emoji %s was rejected in chat %s; trying another allowed emoji: %s", reaction, chat_id, exc)
                continue

            # Network/unknown errors are not solved by swapping emoji.
            logger.warning("Reaction request failed for chat %s; applying a short bot-wide pause: %s", chat_id, exc)
            _set_reaction_global_cooldown(context, REACTION_TRANSIENT_COOLDOWN)
            increment_metric(context, "reaction_failures")
            return

    # All sampled candidates were rejected. Invalidate cached chat metadata so
    # the next eligible message fetches Telegram's current allow-list instead
    # of retrying the same stale list for the full cache TTL.
    bot_data.get("chat_reactions_cache", {}).pop(chat_id, None)
    await _pause_chat_reactions(
        context,
        chat_id,
        REACTION_TRANSIENT_COOLDOWN,
        "all_sampled_emoji_candidates_rejected",
        "All sampled allowed emoji candidates were rejected.",
    )
    increment_metric(context, "reaction_failures")

# =========================================================
# ERROR HANDLER
# =========================================================

async def _persist_worker_runtime_status(bot_id, status, error_text=None):
    """Persist only runtime metadata; never replace the worker's full record."""
    payload = {
        "runtime_status": str(status)[:32],
        "runtime_checked_at": int(time.time()),
        "runtime_last_error": str(error_text or "")[:300],
    }
    try:
        await _firebase_call_with_retry(
            managed_bots_ref().child(str(int(bot_id))).update,
            payload,
        )
        invalidate_managed_bots_cache()
        return True
    except Exception:
        logger.exception("Could not persist runtime status for worker %s.", bot_id)
        return False


async def _notify_worker_health(bot_id, app, status, error_text=None, recovered=False, bot_username=None):
    """Send an owner-only alert naming the exact worker that needs attention."""
    manager = MANAGER_APP
    if manager is None or not getattr(manager, "running", False):
        logger.warning("Worker %s health changed to %s, but Manager alerts are unavailable.", bot_id, status)
        return False

    username = str(bot_username or "")
    if not username and app is not None:
        try:
            username = str(getattr(app.bot, "username", "") or "")
        except Exception:
            pass
    display_name = "@" + username if username else f"Worker ID {bot_id}"
    if recovered:
        title = "<b>✅ WORKER RECOVERED</b>"
        detail = "Polling has resumed and the worker is processing updates again."
        keyboard = None
    else:
        title = "<b>⚠️ WORKER HEALTH ALERT</b>"
        detail = "The worker is not polling normally. Check the error and restart it if needed."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Restart this bot", callback_data=f"mgrbot:{int(bot_id)}:restart")],
            [InlineKeyboardButton("🤖 My Bots", callback_data="mgr:list:0")],
        ])

    text = (
        f"{title}\n\n"
        f"🤖 <b>Bot:</b> <code>{html.escape(display_name)}</code>\n"
        f"🆔 <b>Bot ID:</b> <code>{int(bot_id)}</code>\n"
        f"📍 <b>Status:</b> <code>{html.escape(str(status))}</code>\n"
        f"🕒 <b>Time:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>\n"
        + (f"⚠️ <b>Last error:</b> <code>{html.escape(str(error_text)[:700])}</code>\n" if error_text else "")
        + f"\n{detail}"
    )
    try:
        await manager.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return True
    except Exception:
        logger.exception("Could not send worker health alert to the Manager owner chat.")
        return False


async def _set_worker_runtime_status(app, status, error_text=None, alert=False, recovered=False):
    bot_id = app.bot_data.get("bot_id")
    if bot_id is None or app.bot_data.get("is_manager_app"):
        return
    old_status = app.bot_data.get("runtime_status")
    app.bot_data["runtime_status"] = status
    if error_text:
        app.bot_data["runtime_last_error"] = str(error_text)[:300]
    elif recovered or status == "running":
        app.bot_data["runtime_last_error"] = ""

    if old_status != status:
        await _persist_worker_runtime_status(
            bot_id,
            status,
            app.bot_data.get("runtime_last_error", ""),
        )
    # Send a new alert when the failure status escalates (for example,
    # degraded polling later becomes fully stopped), but never every monitor tick.
    last_alert_status = app.bot_data.get("health_alert_status")
    if alert and last_alert_status != status:
        sent = await _notify_worker_health(
            bot_id,
            app,
            status,
            app.bot_data.get("runtime_last_error", ""),
            recovered=False,
        )
        if sent:
            app.bot_data["health_alert_sent"] = True
            app.bot_data["health_alert_status"] = status
    if recovered and app.bot_data.get("health_alert_sent", False):
        await _notify_worker_health(bot_id, app, "Running", recovered=True)
        app.bot_data["health_alert_sent"] = False
        app.bot_data["health_alert_status"] = None


def _make_polling_error_callback(app):
    """PTB requires a synchronous callback for errors raised by getUpdates."""
    def _callback(error):
        loop = app.bot_data.get("event_loop")
        if loop is None or not loop.is_running():
            logger.warning("Polling error for bot %s outside its active event loop: %s", app.bot_data.get("bot_id"), error)
            return

        def _record():
            app.bot_data["poll_error_count"] = int(app.bot_data.get("poll_error_count", 0)) + 1
            app.bot_data["runtime_last_error"] = str(error)[:300]
            app.bot_data["last_poll_error_at"] = time.time()
            count = app.bot_data["poll_error_count"]
            logger.warning(
                "Polling error for worker %s (%s consecutive): %s",
                app.bot_data.get("bot_id", "unknown"),
                count,
                error,
            )
            if count == 3 and not app.bot_data.get("is_manager_app"):
                asyncio.create_task(
                    _set_worker_runtime_status(
                        app,
                        "degraded",
                        str(error),
                        alert=True,
                    )
                )

        try:
            loop.call_soon_threadsafe(_record)
        except RuntimeError:
            logger.warning("Could not queue polling error for worker %s: %s", app.bot_data.get("bot_id"), error)
    return _callback


async def track_worker_update(update, context):
    """Mark an actual received update as activity; silence alone is not failure."""
    app = context.application
    if app.bot_data.get("is_manager_app"):
        return
    app.bot_data["last_update_at"] = time.time()
    had_poll_errors = int(app.bot_data.get("poll_error_count", 0)) > 0
    was_degraded = app.bot_data.get("runtime_status") == "degraded"
    app.bot_data["poll_error_count"] = 0
    app.bot_data["last_poll_error_at"] = 0.0
    if had_poll_errors or was_degraded:
        await _set_worker_runtime_status(app, "running", recovered=True)


async def worker_health_monitor_loop():
    """Check runtime state without treating a quiet channel as a dead worker."""
    while True:
        for bot_id, app in list(RUNNING_BOTS.items()):
            if getattr(app, "bot_data", {}).get("stopping", False):
                continue
            updater = getattr(app, "updater", None)
            healthy = bool(getattr(app, "running", False) and updater and getattr(updater, "running", False))
            if not healthy:
                err = app.bot_data.get("runtime_last_error") or "Application or Telegram polling is no longer running."
                await _set_worker_runtime_status(
                    app,
                    "stopped_unexpectedly",
                    err,
                    alert=True,
                )
            elif int(app.bot_data.get("poll_error_count", 0)) >= 3:
                last_error_at = float(app.bot_data.get("last_poll_error_at", 0) or 0)
                if time.time() - last_error_at <= 180:
                    await _set_worker_runtime_status(
                        app,
                        "degraded",
                        app.bot_data.get("runtime_last_error") or "Repeated polling errors.",
                        alert=True,
                    )
        await asyncio.sleep(20)

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
    """Save a consistent state snapshot using the same lock as normal saves."""
    firebase_ref = app.bot_data.get("firebase_ref")
    state = app.bot_data.get("state")

    if firebase_ref is None or state is None:
        return False

    lock = app.bot_data.get("save_lock")
    if lock is None:
        lock = asyncio.Lock()
        app.bot_data["save_lock"] = lock

    try:
        async with lock:
            payload = copy.deepcopy(state)
            await _firebase_call_with_retry(firebase_ref.set, payload)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Firebase state save failed.")
        return False



async def _cleanup_failed_application_init(app):
    """Best-effort close of Bot API sessions when Application initialization fails."""
    try:
        if getattr(app, "initialized", False):
            await app.shutdown()
        else:
            bot = getattr(app, "bot", None)
            if bot is not None:
                await bot.shutdown()
    except BaseException:
        logger.exception("Cleanup after Telegram Application initialization failure failed.")

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
        .pool_timeout(10.0)
        .get_updates_connection_pool_size(1)
        .build()
    )

    try:
        await app.initialize()
        me = await app.bot.get_me()
    except BaseException:
        await _cleanup_failed_application_init(app)
        raise

    try:
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
            bot_username,
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
        # Serialize this worker's own reaction requests. Separate worker
        # identities remain independent; Telegram RetryAfter is also respected
        # through a per-worker cooldown in _perform_reaction().
        app.bot_data["reaction_semaphore"] = asyncio.Semaphore(1)
        app.bot_data["is_manager_app"] = False
        app.bot_data["runtime_status"] = "starting"
        app.bot_data["poll_error_count"] = 0
        app.bot_data["health_alert_sent"] = False
        app.bot_data["metrics_dirty"] = False
        app.bot_data["metrics_revision"] = 0
        app.bot_data["state_save_revision"] = 0
        app.bot_data["state_save_task"] = None
        state.setdefault("metrics", {})
        state["metrics"].setdefault("messages_seen", 0)
        state["metrics"].setdefault("reactions_sent", 0)
        state["metrics"].setdefault("reaction_failures", 0)

        if not await save_state_for_app(app):
            raise RuntimeError(
                f"Could not persist the loaded Firebase state for @{bot_username}; worker startup was aborted safely."
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

        # Record actual worker activity separately from incoming-message
        # metrics. Lack of messages is never interpreted as a failed worker.
        app.add_handler(TypeHandler(Update, track_worker_update), group=-1)

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
    except BaseException:
        await _cleanup_failed_application_init(app)
        raise


# =========================================================
# MANAGER BOT
# =========================================================


# =========================================================
# BROADCAST SYSTEM
# =========================================================

BROADCAST_DELETE_JOBS_PATH = "broadcast_delete_jobs"

# Users who blocked a bot (or deleted their account) are removed from that
# bot's user list after a broadcast. Set to False to keep the old behavior.
BROADCAST_PRUNE_BLOCKED_USERS = False
# Never hold the Manager's broadcast handler for a long RetryAfter interval.
# Short waits can be honoured inline; longer waits are reported as deferred.
BROADCAST_INLINE_RETRY_MAX_WAIT = 10.0

# If the indexed query for due delete jobs fails (for example the Firebase
# index is not added yet), fall back to the old full read and retry later.
BROADCAST_JOBS_QUERY_RETRY_AT = 0.0
BROADCAST_JOBS_QUERY_RETRY_SECONDS = 600.0
BROADCAST_JOBS_QUERY_LIMIT = 1000

# While the indexed query is unavailable, a full read of the jobs node is
# allowed only this often (instead of every 10 seconds) to protect bandwidth.
BROADCAST_JOBS_FULLREAD_NEXT_AT = 0.0
BROADCAST_JOBS_FULLREAD_INTERVAL = 120.0


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
    await _firebase_call_with_retry(
        broadcast_delete_jobs_ref().child(key).set,
        job,
    )
    return key


def _is_dead_user_error(exc):
    """True when Telegram says this user can never receive this bot's messages."""
    if isinstance(exc, Forbidden):
        return True
    return isinstance(exc, BadRequest) and "chat not found" in str(exc).lower()


async def _prune_dead_users(bot_id, running_app, user_keys):
    """Remove blocked/deleted users from one bot without racing a full-state save."""
    if not user_keys:
        return 0

    normalized_keys = {str(key) for key in user_keys}
    ref = db.reference(f"reaction_bots/{int(bot_id)}/users")

    if running_app is not None:
        bot_data = running_app.bot_data
        state = bot_data.get("state") or {}
        lock = bot_data.get("save_lock")
        if isinstance(state, dict) and isinstance(state.get("users"), dict) and lock is not None:
            try:
                async with lock:
                    # Update Firebase first while holding the same lock used by
                    # full snapshots; then mirror the committed change in RAM.
                    await _run_thread_to_completion(
                        ref.update,
                        {key: None for key in normalized_keys},
                    )
                    users = state["users"]
                    for key in normalized_keys:
                        users.pop(key, None)
                return len(normalized_keys)
            except Exception:
                logger.exception("Could not remove blocked users for bot %s.", bot_id)
                return 0

    try:
        await _firebase_call_with_retry(
            ref.update,
            {key: None for key in normalized_keys},
        )
        return len(normalized_keys)
    except Exception:
        logger.exception("Could not remove blocked users for bot %s.", bot_id)
        return 0


def _retry_after_seconds(exc):
    value = getattr(exc, "retry_after", 1)
    if hasattr(value, "total_seconds"):
        value = value.total_seconds()
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return 1.0
        return max(1.0, seconds)
    except (TypeError, ValueError, OverflowError):
        return 1.0


def _seen_signing_key(legacy=False):
    secret = TOKEN_ENCRYPTION_SECRET.encode("utf-8")
    if legacy:
        # Links already sent before this update were signed with the secret itself.
        return secret
    return hashlib.sha256(b"jr-seen-link-v2:" + secret).digest()


def _seen_signature(bot_id, chat_id, message_id, legacy=False):
    payload = f"{int(bot_id)}:{int(chat_id)}:{int(message_id)}".encode("utf-8")
    digest = hmac.new(
        _seen_signing_key(legacy),
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
    legacy_signature = _seen_signature(bot_id, intended_chat_id, message_id, legacy=True)
    if not (
        hmac.compare_digest(supplied_signature, expected_signature)
        or hmac.compare_digest(supplied_signature, legacy_signature)
    ):
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
                "✅ Read confirmation received. The original message will be deleted in about 1 minute.\n"
                "(Telegram may refuse to delete messages older than 48 hours.)"
            )
    except Exception:
        logger.exception("Could not save read-confirmation deletion job from Manager Bot.")
        if update.message:
            await update.message.reply_text("Could not schedule deletion right now. Please try again.")
    return True


async def _send_one_bot_broadcast(bot, bot_id, users, message_text, kind, delay):
    """Send this bot's copy to its own opt-in list with explicit per-recipient outcomes.

    A RetryAfter is treated as a bot-wide send pause: the current and remaining
    recipients are reported as deferred rather than sleeping for an unbounded
    interval inside the Manager update handler.
    """
    stats = {
        "users": 0,
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "forbidden": 0,
        "not_reachable": 0,
        "bad_request": 0,
        "other": 0,
        "rate_limited": 0,
        "deferred": 0,
        "invalid": 0,
        "schedule_failed": 0,
        "seen_button_failed": 0,
        "dead_users": [],
        "rate_limit_seconds": 0.0,
    }

    if not isinstance(users, dict):
        stats["invalid"] = 1
        stats["failed"] = 1
        return stats

    recipients = []
    seen_chat_ids = set()
    for user_key in list(users.keys()):
        try:
            chat_id = int(user_key)
            if chat_id <= 0:
                raise ValueError("chat id must be positive")
        except (TypeError, ValueError, OverflowError):
            stats["invalid"] += 1
            continue
        if chat_id in seen_chat_ids:
            # Firebase keys such as "123" and "00123" represent the same
            # Telegram chat. Never send duplicate copies to that chat.
            stats["invalid"] += 1
            continue
        seen_chat_ids.add(chat_id)
        recipients.append((str(user_key), chat_id))

    stats["users"] = len(recipients)

    for index, (user_key, chat_id) in enumerate(recipients):
        stats["attempted"] += 1
        delivered_message = None
        retry_after_attempts = 0
        while True:
            try:
                delivered_message = await bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    disable_web_page_preview=True,
                )
                break
            except RetryAfter as exc:
                wait_for = _retry_after_seconds(exc)
                retry_after_attempts += 1
                if wait_for <= BROADCAST_INLINE_RETRY_MAX_WAIT and retry_after_attempts <= 2:
                    logger.warning(
                        "Broadcast from worker %s was rate limited; honoring Telegram's %.1fs retry_after (retry %s/2).",
                        bot_id, wait_for, retry_after_attempts,
                    )
                    await asyncio.sleep(wait_for + 0.25)
                    continue

                stats["rate_limited"] += 1
                stats["rate_limit_seconds"] = wait_for
                remaining = len(recipients) - index
                stats["deferred"] += remaining
                logger.warning(
                    "Broadcast from worker %s is rate limited for %.1fs; deferring %s remaining recipient(s) so the Manager can continue.",
                    bot_id, wait_for, remaining,
                )
                break
            except Forbidden as exc:
                stats["forbidden"] += 1
                stats["failed"] += 1
                if BROADCAST_PRUNE_BLOCKED_USERS and _is_dead_user_error(exc):
                    stats["dead_users"].append(user_key)
                logger.info("Broadcast forbidden for worker %s (recipient entry not reachable).", bot_id)
                break
            except BadRequest as exc:
                stats["failed"] += 1
                if _is_dead_user_error(exc):
                    stats["not_reachable"] += 1
                    if BROADCAST_PRUNE_BLOCKED_USERS:
                        stats["dead_users"].append(user_key)
                else:
                    stats["bad_request"] += 1
                logger.info("Broadcast BadRequest for worker %s: %s", bot_id, exc)
                break
            except Exception as exc:
                # Do not blindly retry ambiguous send failures: Telegram may have
                # accepted the message even if the response was lost, creating duplicates.
                stats["other"] += 1
                stats["failed"] += 1
                logger.warning("Broadcast failed for worker %s: %s", bot_id, exc)
                break

        if delivered_message is None:
            if stats["deferred"]:
                break
            continue

        stats["sent"] += 1
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
                stats["seen_button_failed"] += 1
                logger.info("Seen-confirmation button could not be added for worker %s: %s", bot_id, exc)

        if kind == "timed":
            try:
                await save_broadcast_delete_job(
                    bot_id,
                    chat_id,
                    delivered_message.message_id,
                    time.time() + max(1, int(delay)),
                )
            except Exception:
                stats["schedule_failed"] += 1
                logger.exception("Broadcast delivered but delete job could not be saved for worker %s.", bot_id)

        await asyncio.sleep(0.05)

    return stats


async def send_broadcast_to_all_bots(message_text, kind, delay=0):
    """Send separately through each bot, only to that bot's own saved users.

    Telegram only permits a bot to initiate private chats with users who have
    started that particular bot. Per-bot recipient lists must not be merged.
    """
    registry = await get_managed_bots(force=True, require_fresh=True)
    result = {
        "bots_total": len(registry),
        "bots_processed": 0,
        "bots_failed": 0,
        "users_found": 0,
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "forbidden": 0,
        "not_reachable": 0,
        "other_errors": 0,
        "rate_limited": 0,
        "deferred": 0,
        "invalid_entries": 0,
        "schedule_failed": 0,
        "seen_button_failed": 0,
        "bots_without_users": 0,
        "running_bots": 0,
        "stopped_bots": 0,
        "pruned": 0,
        "duplicate_registry_entries": 0,
        "per_bot": [],
    }

    records = []
    seen_bot_ids = set()
    for registry_key, record in registry.items():
        if not isinstance(record, dict):
            result["bots_failed"] += 1
            result["per_bot"].append({
                "bot": str(registry_key), "bot_id": str(registry_key),
                "status": "invalid_registry_record", "sent": 0, "failed": 0,
            })
            continue
        try:
            bot_id = int(record.get("bot_id", registry_key))
            if bot_id <= 0:
                raise ValueError("bot id must be positive")
        except (TypeError, ValueError, OverflowError):
            result["bots_failed"] += 1
            result["per_bot"].append({
                "bot": str(record.get("bot_username") or registry_key),
                "bot_id": str(registry_key),
                "status": "invalid_bot_id", "sent": 0, "failed": 0,
            })
            continue
        if bot_id in seen_bot_ids:
            result["duplicate_registry_entries"] += 1
            logger.warning("Skipping duplicate managed-bot registry entry for bot %s.", bot_id)
            continue
        seen_bot_ids.add(bot_id)
        records.append((bot_id, registry_key, record))

    result["bots_total"] = len(records) + len(result["per_bot"])
    records.sort(key=lambda item: (
        reaction_bot_serial(item[2].get("bot_username"))
        if reaction_bot_serial(item[2].get("bot_username")) is not None else 10**9,
        str(item[2].get("bot_username", "")).lower(),
    ))

    for bot_id, registry_key, record in records:
        bot_name = str(record.get("bot_username") or f"bot_{bot_id}")
        running_app = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
        if running_app and _worker_is_running(running_app):
            result["running_bots"] += 1
        else:
            running_app = None
            result["stopped_bots"] += 1

        per_bot_result = {
            "bot": bot_name,
            "bot_id": bot_id,
            "status": "pending",
            "users": 0,
            "attempted": 0,
            "sent": 0,
            "failed": 0,
            "forbidden": 0,
            "not_reachable": 0,
            "bad_request": 0,
            "other": 0,
            "rate_limited": 0,
            "rate_limit_seconds": 0.0,
            "deferred": 0,
            "invalid": 0,
            "schedule_failed": 0,
            "seen_button_failed": 0,
        }

        try:
            users = await _firebase_call_with_retry(
                db.reference(f"reaction_bots/{bot_id}/users").get
            )
        except Exception as exc:
            logger.exception("Could not read user list for bot %s.", bot_id)
            result["bots_failed"] += 1
            per_bot_result.update(status="user_list_read_failed", error=str(exc)[:180])
            result["per_bot"].append(per_bot_result)
            continue

        if users is None:
            users = {}
        if not isinstance(users, dict):
            result["bots_failed"] += 1
            per_bot_result.update(status="malformed_user_list", error="Firebase users node is not an object")
            result["per_bot"].append(per_bot_result)
            continue
        per_bot_result["users"] = len(users)
        result["users_found"] += len(users)
        if not users:
            result["bots_without_users"] += 1
            result["bots_processed"] += 1
            per_bot_result["status"] = "no_saved_users"
            result["per_bot"].append(per_bot_result)
            continue

        temporary_bot = None
        try:
            if running_app is not None:
                telegram_bot = running_app.bot
            else:
                encrypted_token = record.get("token")
                if not encrypted_token:
                    raise RuntimeError("No encrypted token is stored for this bot.")
                temporary_bot = Bot(token=decrypt_token(encrypted_token))
                await temporary_bot.initialize()
                telegram_bot = temporary_bot

            per_bot = await _send_one_bot_broadcast(
                telegram_bot, bot_id, users, message_text, kind, delay
            )
            for key in (
                "attempted", "sent", "failed", "forbidden", "not_reachable",
                "bad_request", "other", "rate_limited", "rate_limit_seconds", "deferred", "invalid",
                "schedule_failed", "seen_button_failed",
            ):
                per_bot_result[key] = per_bot.get(key, 0)
            per_bot_result["status"] = "rate_limited_deferred" if per_bot.get("deferred") else (
                "completed_with_failures"
                if per_bot.get("failed") or per_bot.get("invalid")
                else "completed"
            )
            result["attempted"] += per_bot.get("attempted", 0)
            result["sent"] += per_bot["sent"]
            result["failed"] += per_bot["failed"]
            result["forbidden"] += per_bot.get("forbidden", 0)
            result["not_reachable"] += per_bot.get("not_reachable", 0)
            result["other_errors"] += per_bot.get("other", 0)
            result["rate_limited"] += per_bot.get("rate_limited", 0)
            result["deferred"] += per_bot.get("deferred", 0)
            result["invalid_entries"] += per_bot.get("invalid", 0)
            result["schedule_failed"] += per_bot["schedule_failed"]
            result["seen_button_failed"] += per_bot["seen_button_failed"]
            result["bots_processed"] += 1
            result["pruned"] += await _prune_dead_users(
                bot_id, running_app, per_bot.get("dead_users") or []
            )
        except Exception as exc:
            logger.exception("Broadcast setup failed for bot %s: %s", bot_id, exc)
            result["bots_failed"] += 1
            per_bot_result.update(status="bot_setup_failed", error=str(exc)[:180])
        finally:
            if temporary_bot is not None:
                try:
                    await temporary_bot.shutdown()
                except Exception:
                    logger.exception("Temporary Bot API client shutdown failed for bot %s.", bot_id)
            result["per_bot"].append(per_bot_result)

    return result


async def _fetch_due_delete_jobs(now):
    """Return only the delete jobs that are due.

    Uses an indexed Firebase query on delete_at so future jobs are not
    downloaded every 10 seconds. If the query fails (index missing, network
    hiccup), it falls back to the old full read and tries the query again later.
    """
    global BROADCAST_JOBS_QUERY_RETRY_AT, BROADCAST_JOBS_FULLREAD_NEXT_AT

    ref = broadcast_delete_jobs_ref()

    if time.monotonic() >= BROADCAST_JOBS_QUERY_RETRY_AT:
        try:
            jobs = await _firebase_call_with_retry(
                ref.order_by_child("delete_at")
                .end_at(now)
                .limit_to_first(BROADCAST_JOBS_QUERY_LIMIT)
                .get
            )
            return dict(jobs) if isinstance(jobs, dict) else {}
        except Exception as exc:
            BROADCAST_JOBS_QUERY_RETRY_AT = (
                time.monotonic() + BROADCAST_JOBS_QUERY_RETRY_SECONDS
            )
            logger.warning(
                "Due-job query failed (%s). Using full read for now. "
                "Add \".indexOn\": [\"delete_at\"] for broadcast_delete_jobs in "
                "Firebase Realtime Database rules.",
                exc,
            )

    # Fallback: rate-limited full read (deletions may be late by up to the interval).
    if time.monotonic() < BROADCAST_JOBS_FULLREAD_NEXT_AT:
        return {}
    BROADCAST_JOBS_FULLREAD_NEXT_AT = time.monotonic() + BROADCAST_JOBS_FULLREAD_INTERVAL

    jobs = await _firebase_call_with_retry(ref.get)
    if not isinstance(jobs, dict):
        return {}

    due = {}
    for job_key, job in jobs.items():
        if not isinstance(job, dict):
            due[job_key] = job
            continue
        try:
            delete_at = float(job.get("delete_at", 0))
            if not math.isfinite(delete_at) or delete_at <= now:
                due[job_key] = job
        except (TypeError, ValueError, OverflowError):
            due[job_key] = job
    return due


async def broadcast_delete_scheduler():
    """Retry due deletions from Firebase so Render restarts do not lose timers."""
    bot_blocked_until = {}

    while True:
        # One temporary Bot per stopped worker per pass, reused for all its jobs.
        temp_bots = {}
        try:
            now = time.time()
            jobs = await _fetch_due_delete_jobs(now)
            if jobs:
                registry = await get_managed_bots(force=True, require_fresh=True)
                for job_key, job in list(jobs.items()):
                    if not isinstance(job, dict):
                        await _firebase_call_with_retry(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        continue
                    try:
                        delete_at = float(job.get("delete_at", 0))
                        bot_id = int(job.get("bot_id"))
                        chat_id = int(job.get("chat_id"))
                        message_id = int(job.get("message_id"))
                    except (TypeError, ValueError, OverflowError):
                        await _firebase_call_with_retry(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        continue

                    now = time.time()
                    try:
                        next_attempt_at = float(job.get("next_attempt_at", 0) or 0)
                        attempts_so_far = int(job.get("attempts", 0) or 0)
                        if not math.isfinite(delete_at) or not math.isfinite(next_attempt_at):
                            raise ValueError("non-finite deletion time")
                        if attempts_so_far < 0:
                            raise ValueError("negative deletion attempt count")
                    except (TypeError, ValueError, OverflowError):
                        logger.warning("Removing malformed broadcast deletion job %s.", job_key)
                        await _firebase_call_with_retry(
                            broadcast_delete_jobs_ref().child(str(job_key)).delete
                        )
                        continue
                    if bot_id <= 0 or chat_id == 0 or message_id <= 0:
                        logger.warning("Removing invalid broadcast deletion job %s.", job_key)
                        await _firebase_call_with_retry(
                            broadcast_delete_jobs_ref().child(str(job_key)).delete
                        )
                        continue
                    if now < max(delete_at, next_attempt_at):
                        if delete_at <= now and next_attempt_at > now:
                            job["delete_at"] = next_attempt_at
                            await _firebase_call_with_retry(
                                broadcast_delete_jobs_ref().child(str(job_key)).update,
                                job,
                            )
                        continue

                    # If an earlier job rate-limited this bot, move every
                    # remaining due job for the same bot out of the indexed
                    # query window too; otherwise the first 1,000 jobs could
                    # repeatedly occupy the page and starve other bots.
                    blocked_until = bot_blocked_until.get(bot_id, 0)
                    if blocked_until > now:
                        if delete_at <= now or next_attempt_at < blocked_until:
                            job["delete_at"] = blocked_until
                            job["next_attempt_at"] = blocked_until
                            await _firebase_call_with_retry(
                                broadcast_delete_jobs_ref().child(str(job_key)).update,
                                job,
                            )
                        continue

                    record = registry.get(str(bot_id))
                    if not isinstance(record, dict):
                        # The managed bot was removed; there is no safe token left to use.
                        await _firebase_call_with_retry(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                        continue

                    initializing_temp_bot = False
                    try:
                        running_app = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
                        if running_app is not None and _worker_is_running(running_app):
                            telegram_bot = running_app.bot
                        else:
                            telegram_bot = temp_bots.get(bot_id)
                            if telegram_bot is None:
                                encrypted_token = record.get("token")
                                if not encrypted_token:
                                    raise RuntimeError("No encrypted token for deletion job.")
                                telegram_bot = Bot(token=decrypt_token(encrypted_token))
                                temp_bots[bot_id] = telegram_bot
                                initializing_temp_bot = True
                                await telegram_bot.initialize()
                                initializing_temp_bot = False

                        await telegram_bot.delete_message(
                            chat_id=chat_id,
                            message_id=message_id,
                        )
                        await _firebase_call_with_retry(broadcast_delete_jobs_ref().child(str(job_key)).delete)

                    except RetryAfter as exc:
                        wait_for = _retry_after_seconds(exc)
                        retry_at = time.time() + wait_for + 0.25
                        bot_blocked_until[bot_id] = retry_at
                        # The indexed query is ordered by delete_at. Move this
                        # job beyond the current due window so it cannot occupy
                        # the first 1,000 results and starve other due jobs.
                        job["delete_at"] = retry_at
                        job["next_attempt_at"] = retry_at
                        job["last_error"] = str(exc)[:300]
                        await _firebase_call_with_retry(
                            broadcast_delete_jobs_ref().child(str(job_key)).update,
                            job,
                        )
                        logger.warning(
                            "Delete rate limited for bot %s; rescheduled in %.1fs.",
                            bot_id, wait_for,
                        )

                    except Exception as exc:
                        if initializing_temp_bot:
                            # Do not reuse a partially initialized Bot, and do
                            # not let its pending jobs occupy the due-query page.
                            retry_at = time.time() + 30
                            bot_blocked_until[bot_id] = retry_at
                            job["delete_at"] = retry_at
                            job["next_attempt_at"] = retry_at
                            job["last_error"] = ("Bot initialization failed: " + str(exc))[:300]
                            await _firebase_call_with_retry(
                                broadcast_delete_jobs_ref().child(str(job_key)).update,
                                job,
                            )
                            logger.warning(
                                "Temporary Bot initialization failed for %s; retrying in 30s: %s",
                                bot_id, exc,
                            )
                        else:
                            error_text = str(exc).lower()
                            if any(term in error_text for term in (
                                "message to delete not found",
                                "message can't be deleted",
                                "message_id_invalid",
                                "chat not found",
                                "bot was blocked",
                            )):
                                await _firebase_call_with_retry(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                            else:
                                attempts = attempts_so_far + 1
                                if attempts >= 12:
                                    logger.error("Giving up broadcast deletion job %s after %s attempts: %s", job_key, attempts, exc)
                                    await _firebase_call_with_retry(broadcast_delete_jobs_ref().child(str(job_key)).delete)
                                else:
                                    job["attempts"] = attempts
                                    retry_at = time.time() + min(300, 15 * attempts)
                                    # Keep delete_at aligned with retry scheduling so
                                    # retried jobs leave the current indexed due batch.
                                    job["delete_at"] = retry_at
                                    job["next_attempt_at"] = retry_at
                                    job["last_error"] = str(exc)[:300]
                                    await _firebase_call_with_retry(
                                        broadcast_delete_jobs_ref().child(str(job_key)).update,
                                        job,
                                    )
                                    logger.warning("Broadcast deletion job %s will retry: %s", job_key, exc)

                    # Gentle pacing so a big batch stays under Telegram's limits.
                    await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Broadcast delete scheduler iteration failed.")
        finally:
            for temp_bot in temp_bots.values():
                try:
                    await temp_bot.shutdown()
                except Exception:
                    pass

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
    kind = query.data.rsplit(":", 1)[-1]
    if kind == "timed":
        MANAGER_PENDING_ADD.discard(OWNER_ID)
        MANAGER_BROADCAST_PENDING[OWNER_ID] = {"kind": "timed_wait_delay"}
        await query.answer()
        await query.message.edit_text(
            "⏱ <b>Choose auto-delete time</b>\n\nEvery managed Reaction Bot sends its own copy to its own users. Each copy is deleted after the selected time.",
            parse_mode="HTML",
            reply_markup=manager_broadcast_delay_menu(),
        )
        return

    if kind not in ("normal", "seen"):
        await query.answer("Invalid broadcast option.", show_alert=True)
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING[OWNER_ID] = {"kind": kind}
    await query.answer()
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
    try:
        seconds = int(query.data.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        await query.answer("Invalid delete interval.", show_alert=True)
        return
    if seconds not in (3600, 21600, 43200, 86400):
        await query.answer("Unsupported delete interval.", show_alert=True)
        return
    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING[OWNER_ID] = {"kind": "timed", "delay": seconds}
    await query.answer()
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
    if GROUP_MANAGER_READY:
        keyboard.insert(-1, [
            InlineKeyboardButton("🗂 𝐆𝐑𝐎𝐔𝐏 𝐌𝐀𝐍𝐀𝐆𝐄𝐑", callback_data="gm:home", style="primary"),
        ])
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

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    await update.callback_query.answer()
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

    # Change the conversation state before the first await. This reduces
    # interleaving with concurrent owner updates while still acknowledging
    # the callback immediately afterwards.
    MANAGER_PENDING_ADD.discard(OWNER_ID)
    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)

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

    MANAGER_BROADCAST_PENDING.pop(OWNER_ID, None)
    MANAGER_PENDING_ADD.add(OWNER_ID)
    await update.callback_query.answer()

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
        broadcast_lock = context.application.bot_data.setdefault("broadcast_lock", asyncio.Lock())
        if broadcast_lock.locked():
            MANAGER_BROADCAST_PENDING[OWNER_ID] = pending_broadcast
            await status_message.edit_text(
                "⚠️ Another broadcast is already running. This message was not sent. "
                "Please wait for the current broadcast to finish and send the message again."
            )
            return
        await broadcast_lock.acquire()
        try:
            result = await send_broadcast_to_all_bots(incoming_text, kind, delay)
        except Exception:
            logger.exception("Broadcast operation failed.")
            await status_message.edit_text(
                "❌ Broadcast could not be completed. Check Render logs and Firebase connection."
            )
            return
        finally:
            broadcast_lock.release()

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
            f"🔎 <b>Send attempts:</b> <code>{result['attempted']}</code>\n"
            f"⚠️ <b>Failed attempts:</b> <code>{result['failed']}</code>\n"
            f"⏳ <b>Deferred (not attempted yet):</b> <code>{result['deferred']}</code>\n"
            f"🚦 <b>Rate-limit events:</b> <code>{result['rate_limited']}</code>\n"
            f"🧾 <b>Invalid/duplicate recipient entries:</b> <code>{result['invalid_entries']}</code>\n"
            + (
                f"🧹 <b>Blocked users removed:</b> <code>{result['pruned']}</code>\n"
                if result.get("pruned") else ""
            ) +
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
        if result.get("pruned"):
            summary += "\n\nBlocked/unreachable entries selected for cleanup were removed only from their own bot list; other stats were not changed."
        else:
            summary += "\n\nExisting user counts and stats were not changed."
        summary += (
            "\n\n<b>Important:</b> Each worker can message only people who have started that exact worker bot. "
            "Starting the Manager or a different worker does not subscribe a person to every worker."
        )
        try:
            await status_message.edit_text(summary, parse_mode="HTML", reply_markup=manager_menu())
        except Exception:
            await context.bot.send_message(OWNER_ID, summary, parse_mode="HTML", reply_markup=manager_menu())

        # A compact per-worker breakdown makes partial delivery visible without
        # exposing recipient IDs or putting thousands of characters in one edit.
        detail_lines = []
        for item in result.get("per_bot", []):
            name = html.escape(str(item.get("bot") or item.get("bot_id") or "Unknown"))
            detail_lines.append(
                f"• <b>@{name.lstrip('@')}</b> — {html.escape(str(item.get('status', 'unknown')))} | "
                f"sent {int(item.get('sent', 0) or 0)}/{int(item.get('users', 0) or 0)} | "
                f"forbidden {int(item.get('forbidden', 0) or 0)} | "
                f"not reachable {int(item.get('not_reachable', 0) or 0)} | "
                f"other errors {int(item.get('other', 0) or 0)} | "
                f"rate limited {int(item.get('rate_limited', 0) or 0)} | "
                f"deferred {int(item.get('deferred', 0) or 0)} | "
                f"invalid entries {int(item.get('invalid', 0) or 0)}"
            )
            if item.get("error"):
                detail_lines[-1] += f" | {html.escape(str(item['error'])[:100])}"
        if detail_lines:
            chunks = []
            current = "<b>Broadcast delivery by bot</b>\n\n"
            for line in detail_lines:
                if len(current) + len(line) + 1 > 3500:
                    chunks.append(current)
                    current = "<b>Broadcast delivery by bot (continued)</b>\n\n"
                current += line + "\n"
            if current.strip():
                chunks.append(current)
            for chunk in chunks:
                try:
                    await context.bot.send_message(OWNER_ID, chunk, parse_mode="HTML")
                except Exception:
                    logger.exception("Could not send per-worker broadcast delivery report.")
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
        # Check for an already-registered bot BEFORE building a worker, so a
        # duplicate add cannot overwrite the running bot's saved state.
        precheck_bot = Bot(token=token)
        try:
            await precheck_bot.initialize()
            precheck_me = await precheck_bot.get_me()
        finally:
            try:
                await precheck_bot.shutdown()
            except Exception:
                pass

        precheck_registry = await get_managed_bots(force=True, require_fresh=True)
        if precheck_registry.get(str(precheck_me.id)) or RUNNING_BOTS.get(precheck_me.id):
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ @{precheck_me.username or f'bot_{precheck_me.id}'} is already registered in the Manager."
            )
            return

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
        except Exception as exc:
            record["enabled"] = True
            record["last_start_error"] = str(exc)[:300]
            record["last_start_failed_at"] = int(time.time())
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                logger.exception("Could not persist the failed Add Bot startup state for %s.", bot_id)
            await _persist_worker_runtime_status(bot_id, "start_failed", str(exc))
            await stop_application(worker_app)
            worker_app = None
            await context.bot.send_message(
                OWNER_ID,
                "⚠️ <b>Bot saved, but it could not start yet.</b>\n\n"
                f"🤖 @{html.escape(bot_username)} remains enabled. Use <b>MY BOTS → Start</b> to retry.\n"
                f"<code>{html.escape(str(exc)[:250])}</code>",
                parse_mode="HTML",
                reply_markup=manager_menu(),
            )
            return

        RUNNING_BOTS[bot_id] = worker_app
        await _persist_worker_runtime_status(bot_id, "running", "")
        worker_app = None  # ownership now belongs to RUNNING_BOTS

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
        if bot_id is not None and bot_id in RUNNING_BOTS:
            logger.exception("Managed bot %s is running, but the final Manager notification failed.", bot_id)
            try:
                await context.bot.send_message(
                    OWNER_ID,
                    f"🟢 @{html.escape(bot_username)} was added and is running. The final confirmation message had a delivery problem."
                )
            except Exception:
                logger.exception("Could not send Add Bot recovery confirmation.")
            return
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
        # A broadcast runs inside its handler and can take minutes. Processing
        # Manager updates strictly one-by-one would freeze the whole Manager
        # (menus and users' "I've Read" links) until it finished.
        .concurrent_updates(4)
        .connection_pool_size(8)
        .pool_timeout(10.0)
        .get_updates_connection_pool_size(1)
        .build()
    )

    try:
        await app.initialize()
        me = await app.bot.get_me()
    except BaseException:
        await _cleanup_failed_application_init(app)
        raise
    try:
        global MANAGER_BOT_USERNAME, GROUP_MANAGER_READY
        GROUP_MANAGER_READY = False
        app.bot_data["is_manager_app"] = True
        app.bot_data["runtime_status"] = "starting"
        app.bot_data["broadcast_lock"] = asyncio.Lock()
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
        if group_manager is not None:
            try:
                group_manager.register(
                    app,
                    owner_id=OWNER_ID,
                    get_managed_bots=get_managed_bots,
                    running_bots=RUNNING_BOTS,
                    decrypt_token=decrypt_token,
                    save_delete_job=save_broadcast_delete_job,
                    firebase_call=_firebase_call_with_retry,
                    db=db,
                    bot_class=Bot,
                    is_worker_running=_worker_is_running,
                )
                GROUP_MANAGER_READY = True
            except Exception:
                logger.exception("Group Manager could not be registered; the Manager continues without it.")
        app.add_error_handler(error_handler)
        return app
    except BaseException:
        await _cleanup_failed_application_init(app)
        raise


async def start_application(app):
    app.bot_data["event_loop"] = asyncio.get_running_loop()
    await app.start()
    if app.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    await app.updater.start_polling(
        drop_pending_updates=True,
        bootstrap_retries=1,
        error_callback=_make_polling_error_callback(app),
    )
    app.bot_data["runtime_status"] = "running"


async def build_and_start_manager_bot_with_retry(attempts=3):
    """Retry Manager startup without reusing a half-started Application."""
    last_error = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        manager_app = None
        try:
            manager_app = await build_manager_bot()
            await start_application(manager_app)
            return manager_app
        except asyncio.CancelledError:
            if manager_app is not None:
                await stop_application(manager_app)
            raise
        except Exception as exc:
            last_error = exc
            if manager_app is not None:
                try:
                    await stop_application(manager_app)
                except Exception:
                    logger.exception("Manager cleanup failed after startup attempt %s.", attempt)
            if attempt < attempts:
                wait_seconds = 5.0 * attempt
                logger.warning(
                    "Manager startup failed (attempt %s/%s); retrying in %.1fs: %s",
                    attempt, attempts, wait_seconds, exc,
                )
                await asyncio.sleep(wait_seconds)
    raise RuntimeError(f"Manager Bot could not start after {attempts} attempts: {last_error}") from last_error


def _retain_shutdown_task(task):
    """Keep timed-out shutdown work alive long enough to finish/consume errors."""
    if task is None:
        return
    DETACHED_SHUTDOWN_TASKS.add(task)

    def _finished(done_task):
        DETACHED_SHUTDOWN_TASKS.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("A detached shutdown task failed.")

    task.add_done_callback(_finished)


async def _await_shutdown_task(task, deadline, label, cancel_on_timeout=True):
    """Wait only until the shared deadline; never await a timed-out thread task."""
    if task is None:
        return True
    if task.done():
        try:
            result = task.result()
            return result is not False
        except asyncio.CancelledError:
            return False
        except Exception:
            logger.exception("Shutdown step failed: %s", label)
            return False

    remaining = max(0.0, float(deadline) - time.monotonic())
    if remaining <= 0:
        if cancel_on_timeout:
            task.cancel()
        _retain_shutdown_task(task)
        logger.warning("Shutdown deadline reached before step completed: %s", label)
        return False

    done, _ = await asyncio.wait({task}, timeout=remaining)
    if task in done:
        try:
            result = task.result()
            return result is not False
        except asyncio.CancelledError:
            return False
        except Exception:
            logger.exception("Shutdown step failed: %s", label)
            return False

    if cancel_on_timeout:
        task.cancel()
    _retain_shutdown_task(task)
    logger.warning("Timed out during shutdown step: %s", label)
    return False


async def _run_shutdown_step(coro, deadline, label):
    task = asyncio.create_task(coro, name=f"shutdown-{label.replace(' ', '-')}")
    return await _await_shutdown_task(task, deadline, label)


async def stop_application(app, deadline=None):
    """Best-effort graceful stop under a shared monotonic deadline.

    A timed-out asyncio.to_thread Firebase call cannot be force-killed. In that
    case the task is detached and no competing final snapshot is started, so an
    older in-flight write cannot race a newer final write.
    """
    if app is None:
        return True
    if deadline is None:
        deadline = time.monotonic() + SHUTDOWN_STOP_TIMEOUT
    if hasattr(app, "bot_data") and not app.bot_data.get("is_manager_app"):
        app.bot_data["stopping"] = True

    all_steps_ok = True
    updater_stopped = True
    application_stopped = True

    updater = getattr(app, "updater", None)
    if updater is not None and getattr(updater, "running", False):
        updater_stopped = await _run_shutdown_step(
            updater.stop(), deadline, "stop Telegram polling"
        )
        all_steps_ok = all_steps_ok and updater_stopped

    if getattr(app, "running", False):
        application_stopped = await _run_shutdown_step(
            app.stop(), deadline, "stop Telegram application"
        )
        all_steps_ok = all_steps_ok and application_stopped

    pending_save = getattr(app, "bot_data", {}).get("state_save_task")
    pending_save_finished = True
    if pending_save is not None and not pending_save.done():
        pending_save_finished = await _await_shutdown_task(
            pending_save, deadline, "finish pending Firebase state save",
            cancel_on_timeout=False,
        )
        all_steps_ok = all_steps_ok and pending_save_finished

    # Only take a final snapshot once polling/handlers are stopped and there is
    # no older save still in flight. This avoids out-of-order Firebase writes.
    has_persisted_state = (
        getattr(app, "bot_data", {}).get("firebase_ref") is not None
        and getattr(app, "bot_data", {}).get("state") is not None
    )
    if updater_stopped and application_stopped and pending_save_finished and has_persisted_state:
        final_task = asyncio.create_task(
            save_state_for_app(app), name="shutdown-final-state-save"
        )
        final_saved = await _await_shutdown_task(
            final_task, deadline, "final Firebase state save"
        )
        all_steps_ok = all_steps_ok and final_saved
    elif has_persisted_state:
        logger.warning(
            "Skipping final Firebase snapshot because the application or a prior save did not stop within the deadline."
        )
        all_steps_ok = False

    if hasattr(app, "bot_data") and app.bot_data.get("state_save_task") is pending_save:
        app.bot_data["state_save_task"] = None

    shutdown_ok = await _run_shutdown_step(
        app.shutdown(), deadline, "shutdown Telegram client"
    )
    stopped_cleanly = bool(all_steps_ok and shutdown_ok)
    if hasattr(app, "bot_data") and not app.bot_data.get("is_manager_app") and not stopped_cleanly:
        app.bot_data["stopping"] = False
    return stopped_cleanly


async def flush_metrics_once(force=False):
    """Persist per-bot analytics without racing full state snapshots."""
    candidates = []

    for bot_id, bot_app in list(RUNNING_BOTS.items()):
        if not getattr(bot_app, "bot_data", None):
            continue
        if not force and not bot_app.bot_data.get("metrics_dirty", False):
            continue
        state = bot_app.bot_data.get("state")
        if not isinstance(state, dict) or bot_app.bot_data.get("firebase_ref") is None:
            continue

        actual_bot_id = state.get("bot_id", bot_id)
        try:
            actual_bot_id = int(actual_bot_id)
        except (TypeError, ValueError):
            actual_bot_id = bot_id
        candidates.append((actual_bot_id, bot_app))

    if not candidates:
        return

    # A full state save writes reaction_bots/<id> as one object, while this
    # flush updates only its /metrics child. Acquire the same per-bot save locks
    # first, then snapshot metrics, so waiting for a lock cannot make this write
    # stale relative to a completed full-state save.
    acquired_locks = []
    updates = {}
    revisions = {}
    try:
        for _, bot_app in sorted(candidates, key=lambda item: item[0]):
            lock = bot_app.bot_data.get("save_lock")
            if lock is None:
                raise RuntimeError("Managed bot is missing its Firebase save lock.")
            await lock.acquire()
            acquired_locks.append(lock)

        for actual_bot_id, bot_app in candidates:
            if not force and not bot_app.bot_data.get("metrics_dirty", False):
                continue
            state = bot_app.bot_data.get("state")
            if not isinstance(state, dict):
                continue

            metrics = copy.deepcopy(state.get("metrics") or {})
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

        if not updates:
            return

        await _firebase_call_with_retry(db.reference("/").update, updates)
    except Exception:
        logger.exception(
            "Periodic analytics Firebase flush failed for %d bot(s).",
            len(candidates),
        )
        return
    finally:
        for lock in reversed(acquired_locks):
            lock.release()

    for actual_bot_id, bot_app in candidates:
        current_revision = int(bot_app.bot_data.get("metrics_revision", 0))
        if actual_bot_id in revisions and current_revision == revisions[actual_bot_id]:
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


def _worker_is_running(app):
    updater = getattr(app, "updater", None) if app is not None else None
    return bool(
        app is not None
        and getattr(app, "running", False)
        and updater is not None
        and getattr(updater, "running", False)
    )


async def start_managed_bot_from_record(record):
    token_value = record.get("token")
    if not token_value:
        raise RuntimeError("Managed bot record has no encrypted token.")

    token = decrypt_token(token_value)
    app = await build_bot("dynamic", token)
    bot_id = int(app.bot.id)

    # The registry id can be malformed or duplicated in old Firebase data.
    # Lock by the identity Telegram itself returns, not only by registry key.
    async with get_bot_start_lock(bot_id):
        existing = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
        if existing is not None:
            if _worker_is_running(existing):
                await _cleanup_failed_application_init(app)
                logger.warning(
                    "A second registry record resolved to already-running Telegram bot %s; reusing the existing worker.",
                    bot_id,
                )
                return existing

            stopped = await stop_application(existing)
            if not stopped:
                await _cleanup_failed_application_init(app)
                raise RuntimeError(
                    f"Worker {bot_id} has a previous Application that did not stop cleanly; refusing to create a duplicate polling session."
                )
            RUNNING_BOTS.pop(bot_id, None)
            RUNNING_BOTS.pop(str(bot_id), None)

        try:
            await start_application(app)
        except BaseException:
            # Also clean up on task cancellation; otherwise a half-started poller
            # could survive a cancelled startup sequence. Keep this cleanup bounded.
            cleanup_task = asyncio.create_task(
                stop_application(app, deadline=time.monotonic() + 2.0),
                name=f"cleanup-failed-worker-{bot_id}",
            )
            await _await_shutdown_task(
                cleanup_task, time.monotonic() + 2.0, "cleanup failed worker startup",
                cancel_on_timeout=True,
            )
            raise

        RUNNING_BOTS[bot_id] = app
        await _persist_worker_runtime_status(bot_id, "running", "")
        return app


MANAGED_BOT_START_ATTEMPTS = 3
MANAGED_BOT_START_RETRY_DELAY = 5.0


async def start_managed_bot_with_retry(record, username):
    """A short network/Telegram hiccup must not permanently disable a bot."""
    last_exc = None
    for attempt in range(1, MANAGED_BOT_START_ATTEMPTS + 1):
        try:
            return await start_managed_bot_from_record(record)
        except Exception as exc:
            last_exc = exc
            if attempt < MANAGED_BOT_START_ATTEMPTS:
                logger.warning(
                    "Start of managed bot %s failed (attempt %s/%s): %s. Retrying...",
                    username, attempt, MANAGED_BOT_START_ATTEMPTS, exc,
                )
                await asyncio.sleep(MANAGED_BOT_START_RETRY_DELAY * attempt)
    raise last_exc


async def start_all_managed_bots():
    registry = None
    for attempt in range(1, 4):
        try:
            registry = await get_managed_bots(force=True, require_fresh=True)
            break
        except Exception as exc:
            logger.exception("Could not load managed-bot registry (startup attempt %s/3).", attempt)
            if attempt < 3:
                await asyncio.sleep(5.0 * attempt)
    if registry is None:
        logger.error("Managed Reaction Bots were not restored because Firebase registry remained unavailable.")
        return
    if not registry:
        logger.info("No managed reaction bots registered yet.")
        return

    records = []
    seen_ids = set()
    for bot_key, record in registry.items():
        if not isinstance(record, dict) or not record.get("enabled", False):
            continue
        record = copy.deepcopy(record)
        record.setdefault("bot_id", bot_key)
        try:
            numeric_id = int(record.get("bot_id"))
        except (TypeError, ValueError):
            logger.error("Skipping managed bot with invalid id under registry key %r.", bot_key)
            continue
        if numeric_id in seen_ids:
            logger.warning("Skipping duplicate managed-bot record for id %s during startup.", numeric_id)
            continue
        seen_ids.add(numeric_id)
        record["bot_id"] = numeric_id
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
            async with get_bot_lifecycle_lock(bot_id):
                if _worker_is_running(RUNNING_BOTS.get(bot_id)):
                    return
                try:
                    app = await start_managed_bot_with_retry(record, username)
                    actual_id = app.bot.id
                    actual_username = app.bot.username or f"bot_{actual_id}"
                    record_changed = False
                    if str(actual_id) != str(bot_id):
                        record["bot_id"] = actual_id
                        record["bot_username"] = actual_username
                        record_changed = True
                    if record.pop("last_start_error", None) is not None:
                        record_changed = True
                    if record.pop("last_start_failed_at", None) is not None:
                        record_changed = True
                    if record_changed:
                        try:
                            await save_managed_bot(actual_id, record)
                            if str(bot_id) != str(actual_id):
                                await delete_managed_bot(bot_id)
                        except Exception:
                            logger.exception(
                                "Worker @%s is running, but its registry status could not be refreshed.",
                                actual_username,
                            )
                    logger.info("Managed reaction bot started: @%s", actual_username)
                except Exception as exc:
                    logger.exception("Failed to start managed bot %s after all retries", username)
                    # Exhausted automatic startup retries are recorded, but
                    # the enabled flag is retained so a transient failure does
                    # not silently disable the worker for the next deployment.
                    record["enabled"] = True
                    record["last_start_error"] = str(exc)[:300]
                    record["last_start_failed_at"] = int(time.time())
                    try:
                        await save_managed_bot(bot_id, record)
                    except Exception:
                        logger.exception("Could not persist startup error for managed bot %s", bot_id)
                    await _persist_worker_runtime_status(bot_id, "start_failed", str(exc))
                    await _notify_worker_health(
                        bot_id,
                        None,
                        "start_failed",
                        str(exc),
                        bot_username=username,
                    )
                finally:
                    await asyncio.sleep(MANAGED_BOT_START_DELAY)

    # Small bounded batches: faster than purely serial startup, but avoids
    # a large CPU/network spike on one Render instance.
    await asyncio.gather(*(start_one(record) for record in records))


async def stop_managed_bot(bot_id, deadline=None):
    normalized_id = int(bot_id)
    app = RUNNING_BOTS.get(normalized_id)
    if app is None:
        app = RUNNING_BOTS.get(str(normalized_id))
    if app is not None:
        stopped = await stop_application(app, deadline=deadline)
        if not stopped:
            return False
        RUNNING_BOTS.pop(normalized_id, None)
        RUNNING_BOTS.pop(str(normalized_id), None)
    if deadline is None:
        await _persist_worker_runtime_status(normalized_id, "stopped", "")
    else:
        status_task = asyncio.create_task(
            _persist_worker_runtime_status(normalized_id, "stopped", ""),
            name=f"persist-worker-{normalized_id}-stopped",
        )
        await _await_shutdown_task(
            status_task, deadline, f"persist worker {normalized_id} stopped status",
            cancel_on_timeout=True,
        )
    return True


async def render_bot_list(page=0):
    try:
        registry = await get_managed_bots()
    except Exception as exc:
        logger.exception("Could not load the managed-bot list from Firebase.")
        text = (
            "❌ <b>Could not load My Bots</b>\n\n"
            "Firebase is temporarily unavailable. No bot data was changed. "
            "Please press Refresh and try again.\n"
            f"<code>{html.escape(str(exc)[:250])}</code>"
        )
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Refresh", callback_data="mgr:list:0")],
            [InlineKeyboardButton("‹ Main Menu", callback_data="mgr:home")],
        ])

    items = []
    seen_bot_ids = set()
    for key, record in registry.items():
        if not isinstance(record, dict):
            continue
        try:
            bot_id = int(record.get("bot_id", key))
        except (TypeError, ValueError):
            logger.warning("Skipping malformed managed-bot registry key %r.", key)
            continue
        if bot_id in seen_bot_ids:
            logger.warning("Skipping duplicate bot id %s in Manager list.", bot_id)
            continue
        seen_bot_ids.add(bot_id)
        username = str(record.get("bot_username") or f"bot_{bot_id}")
        enabled = bool(record.get("enabled", False))
        runtime_app = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
        running = _worker_is_running(runtime_app)
        runtime_status = (
            runtime_app.bot_data.get("runtime_status", "running")
            if runtime_app is not None
            else str(record.get("runtime_status") or "")
        )
        last_error = str(
            (runtime_app.bot_data.get("runtime_last_error") if runtime_app else "")
            or record.get("runtime_last_error")
            or record.get("last_start_error")
            or ""
        ).strip()
        items.append({
            "id": bot_id,
            "username": username,
            "enabled": enabled,
            "running": running,
            "runtime_status": runtime_status,
            "last_error": last_error,
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
            if item["runtime_status"] == "degraded":
                status = "🟠 Degraded / Reconnecting" if item["running"] else "🟠 Degraded / Not polling"
            elif item["runtime_status"] == "stopped_unexpectedly":
                status = "🔴 Stopped unexpectedly"
            elif item["runtime_status"] == "start_failed":
                status = "🔴 Start failed / Enabled"
            elif item["running"]:
                status = "🟢 Running"
            elif item["enabled"]:
                status = "🟡 Enabled / Not running"
            else:
                status = "⚪ Disabled"
            serial = reaction_bot_serial(item["username"])
            serial_label = f"#{serial} • " if serial is not None else ""
            text += (
                f"<b>{index}. {serial_label}@{html.escape(item['username'])}</b>\n"
                f"   🆔 <code>{item['id']}</code> • {status}\n"
                + (f"   ⚠️ <i>{html.escape(item['last_error'][:120])}</i>\n" if item["last_error"] and (not item["running"] or item["runtime_status"] == "degraded") else "")
                + "\n"
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

    try:
        registry = await get_managed_bots()
    except Exception as exc:
        logger.exception("Could not load Manager statistics from Firebase.")
        msg = update.callback_query.message
        if msg:
            await msg.reply_text(
                "❌ Firebase is temporarily unavailable. Statistics were not changed.\n"
                f"<code>{html.escape(str(exc)[:250])}</code>",
                parse_mode="HTML",
                reply_markup=manager_menu(),
            )
        return

    valid_records = {}
    for key, record in registry.items():
        if not isinstance(record, dict):
            continue
        try:
            record_bot_id = int(record.get("bot_id", key))
        except (TypeError, ValueError):
            continue
        valid_records.setdefault(record_bot_id, record)
    total = len(valid_records)
    healthy_running = 0
    degraded_running = 0
    for bot_id in valid_records:
        runtime_app = RUNNING_BOTS.get(bot_id) or RUNNING_BOTS.get(str(bot_id))
        if not _worker_is_running(runtime_app):
            continue
        runtime_status = runtime_app.bot_data.get("runtime_status", "running")
        if runtime_status == "degraded":
            degraded_running += 1
        else:
            healthy_running += 1
    enabled = sum(1 for record in valid_records.values() if record.get("enabled", False))
    stopped = max(0, total - healthy_running - degraded_running)

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>MANAGER STATISTICS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🤖 <b>Total Bots:</b> <code>{total}</code>\n"
        f"🟢 <b>Running:</b> <code>{healthy_running}</code>\n"
        f"🟠 <b>Degraded:</b> <code>{degraded_running}</code>\n"
        f"🟡 <b>Enabled:</b> <code>{enabled}</code>\n"
        f"⚪ <b>Stopped/Not running:</b> <code>{stopped}</code>\n"
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

    try:
        registry = await get_managed_bots(force=True, require_fresh=True)
    except Exception as exc:
        logger.exception("Restart All could not read the Firebase registry.")
        await msg.reply_text(
            "❌ Firebase registry is temporarily unavailable. No bots were restarted. "
            "Please try again after the connection recovers.\n"
            f"<code>{html.escape(str(exc)[:250])}</code>",
            parse_mode="HTML",
            reply_markup=manager_menu(),
        )
        return

    records = []
    seen_bot_ids = set()
    for registry_key, record in registry.items():
        if not isinstance(record, dict) or not record.get("enabled", False) or not record.get("token"):
            continue
        try:
            record_bot_id = int(record.get("bot_id", registry_key))
        except (TypeError, ValueError):
            logger.warning("Skipping malformed Restart All entry %r.", registry_key)
            continue
        if record_bot_id in seen_bot_ids:
            continue
        seen_bot_ids.add(record_bot_id)
        record = copy.deepcopy(record)
        record["bot_id"] = record_bot_id
        records.append(record)
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
        async with get_bot_lifecycle_lock(bot_id):
            try:
                stopped = await stop_managed_bot(bot_id)
                if not stopped:
                    raise RuntimeError("Previous worker session did not stop cleanly; refusing a duplicate start.")
                app = await start_managed_bot_with_retry(record, username)
                RUNNING_BOTS[bot_id] = app
                record.pop("last_start_error", None)
                record.pop("last_start_failed_at", None)
                try:
                    await save_managed_bot(bot_id, record)
                except Exception:
                    logger.exception(
                        "Restart All started @%s, but could not clear its old registry error marker.",
                        username,
                    )
                success += 1
            except Exception as exc:
                # A temporary error must not switch the bot off permanently.
                logger.exception("Restart-all failed for %s", username)
                record["enabled"] = True
                record["last_start_error"] = str(exc)[:300]
                record["last_start_failed_at"] = int(time.time())
                try:
                    await save_managed_bot(bot_id, record)
                except Exception:
                    logger.exception("Could not save Restart All failure for %s", username)
                failed.append(username)

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


async def _manager_bot_action_callback_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    bot_id = int(parts[1])
    action = parts[2]
    try:
        registry = await get_managed_bots(force=True, require_fresh=True)
    except Exception as exc:
        logger.exception("Could not read fresh registry for Manager action on bot %s.", bot_id)
        await update.callback_query.message.reply_text(
            "❌ Firebase is temporarily unavailable. The bot action was not performed.\n"
            f"<code>{html.escape(str(exc)[:250])}</code>",
            parse_mode="HTML",
        )
        return

    record = registry.get(str(bot_id))
    if not isinstance(record, dict):
        await update.callback_query.message.reply_text("❌ Bot record not found.")
        return

    username = record.get("bot_username") or f"bot_{bot_id}"

    try:
        if action == "start":
            existing_app = RUNNING_BOTS.get(bot_id)
            if _worker_is_running(existing_app):
                if not record.get("enabled", False):
                    record["enabled"] = True
                    await save_managed_bot(bot_id, record)
                await update.callback_query.message.reply_text(f"ℹ️ @{username} is already running.")
            else:
                if existing_app is not None and not await stop_managed_bot(bot_id):
                    raise RuntimeError("The previous worker session did not stop safely; no second polling session was started.")
                # A manual Start explicitly enables the bot before attempting
                # startup. If all retries fail, it remains enabled for the next
                # manual attempt or automatic service restart.
                record["enabled"] = True
                await save_managed_bot(bot_id, record)
                app = await start_managed_bot_with_retry(record, username)
                RUNNING_BOTS[bot_id] = app
                record.pop("last_start_error", None)
                record.pop("last_start_failed_at", None)
                try:
                    await save_managed_bot(bot_id, record)
                except Exception:
                    logger.exception("@%s started, but the old registry error marker could not be cleared.", username)
                await update.callback_query.message.reply_text(f"🟢 @{username} started.")

        elif action == "stop":
            if not await stop_managed_bot(bot_id):
                raise RuntimeError("The worker did not stop cleanly. Its enabled state was not changed.")
            record["enabled"] = False
            await save_managed_bot(bot_id, record)
            await update.callback_query.message.reply_text(f"⏸ @{username} stopped.")

        elif action == "restart":
            if not await stop_managed_bot(bot_id):
                raise RuntimeError("The previous worker session did not stop safely; restart was aborted.")
            # A manual Restart is also an explicit request to keep this bot
            # enabled, even when the subsequent Telegram startup retries fail.
            record["enabled"] = True
            await save_managed_bot(bot_id, record)
            app = await start_managed_bot_with_retry(record, username)
            RUNNING_BOTS[bot_id] = app
            record.pop("last_start_error", None)
            record.pop("last_start_failed_at", None)
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                logger.exception("@%s restarted, but the old registry error marker could not be cleared.", username)
            await update.callback_query.message.reply_text(f"🔄 @{username} restarted.")

        elif action == "remove":
            if not await stop_managed_bot(bot_id):
                raise RuntimeError("The worker did not stop cleanly; its registry record was not removed.")
            await delete_managed_bot(bot_id)
            await update.callback_query.message.reply_text(
                f"🗑 @{username} removed from the Manager.\n\nFirebase reaction state is kept under reaction_bots/{bot_id}."
            )

    except Exception as exc:
        logger.exception("Manager action failed for bot %s", bot_id)
        if action in ("start", "restart") and _worker_is_running(RUNNING_BOTS.get(bot_id)):
            await update.callback_query.message.reply_text(
                f"🟢 @{username} is running. The worker started, but a follow-up Manager status step failed.\n"
                f"<code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
            )
            return
        if action in ("start", "restart"):
            record["enabled"] = True
            record["last_start_error"] = str(exc)[:300]
            record["last_start_failed_at"] = int(time.time())
            await _persist_worker_runtime_status(bot_id, "start_failed", str(exc))
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                logger.exception("Could not save manual start failure for bot %s", bot_id)
        if action == "stop" and not _worker_is_running(RUNNING_BOTS.get(bot_id)):
            await update.callback_query.message.reply_text(
                f"⏸ @{username} is stopped in this Render process, but Firebase could not save Disabled. "
                "It may start again after a service restart; please retry Stop when Firebase recovers."
            )
            return
        if action == "remove" and not _worker_is_running(RUNNING_BOTS.get(bot_id)):
            await update.callback_query.message.reply_text(
                f"⚠️ @{username} was stopped here, but its Manager record could not be removed from Firebase. "
                "It may return after a service restart; please retry Remove when Firebase recovers."
            )
            return
        await update.callback_query.message.reply_text(
            f"❌ Could not {action} @{username}.\n"
            f"<code>{html.escape(str(exc)[:250])}</code>",
            parse_mode="HTML",
        )


async def manager_bot_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Serialize Start/Stop/Restart/Remove per worker to prevent duplicate sessions."""
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    try:
        parts = update.callback_query.data.split(":")
        bot_id = int(parts[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        await update.callback_query.answer("Invalid bot action.", show_alert=True)
        return
    async with get_bot_lifecycle_lock(bot_id):
        await _manager_bot_action_callback_unlocked(update, context)


# =========================================================
# MAIN
# =========================================================

async def main():
    global MANAGER_APP

    Thread(target=run_server, daemon=True).start()

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

    manager = None
    startup_task = None
    metrics_flush_task = None
    broadcast_scheduler_task = None
    health_monitor_task = None

    stop_event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
    except Exception:
        logger.exception("Could not install shutdown signal handlers.")

    try:
        manager = await build_and_start_manager_bot_with_retry(attempts=3)
        MANAGER_APP = manager

        health_monitor_task = asyncio.create_task(
            worker_health_monitor_loop(), name="worker-health-monitor"
        )
        startup_task = asyncio.create_task(
            start_all_managed_bots(), name="managed-bot-startup"
        )
        metrics_flush_task = asyncio.create_task(
            metrics_flush_loop(), name="metrics-flush-loop"
        )
        broadcast_scheduler_task = asyncio.create_task(
            broadcast_delete_scheduler(), name="broadcast-delete-scheduler"
        )

        logger.info("Manager running; restoring managed reaction bots in background.")
        await stop_event.wait()
        logger.info("Shutdown signal received; stopping gracefully.")
    finally:
        # One deadline covers the entire sequence. Reserve the last seconds for
        # the Manager Bot so a stuck worker or Firebase thread cannot prevent it
        # from reaching its own stop/shutdown stage.
        shutdown_deadline = time.monotonic() + SHUTDOWN_STOP_TIMEOUT
        manager_reserve = min(
            SHUTDOWN_MANAGER_RESERVE,
            max(1.0, SHUTDOWN_STOP_TIMEOUT * 0.25),
        )
        worker_deadline = max(time.monotonic(), shutdown_deadline - manager_reserve)

        background_tasks = (
            (health_monitor_task, "worker health monitor"),
            (broadcast_scheduler_task, "broadcast delete scheduler"),
            (metrics_flush_task, "metrics flush loop"),
            (startup_task, "managed bot startup"),
        )
        for task, label in background_tasks:
            if task is None or task.done():
                continue
            task.cancel()
            await _await_shutdown_task(task, worker_deadline, label, cancel_on_timeout=True)

        # Final metrics flush is bounded by the worker portion of the shared
        # deadline. A stuck Firebase thread is detached; worker/Manager shutdown
        # still proceeds and no concurrent final snapshot races that write.
        final_metrics_task = asyncio.create_task(
            flush_metrics_once(force=True), name="shutdown-final-metrics-flush"
        )
        await _await_shutdown_task(
            final_metrics_task, worker_deadline, "final analytics flush", cancel_on_timeout=True
        )

        running_ids = list(RUNNING_BOTS.keys())
        if running_ids and time.monotonic() < worker_deadline:
            async def _stop_all_workers():
                return await asyncio.gather(
                    *(stop_managed_bot(bot_id, deadline=worker_deadline) for bot_id in running_ids),
                    return_exceptions=True,
                )

            workers_task = asyncio.create_task(
                _stop_all_workers(), name="shutdown-managed-workers"
            )
            completed = await _await_shutdown_task(
                workers_task, worker_deadline, "managed worker shutdown", cancel_on_timeout=True
            )
            if not completed:
                logger.warning("Some managed workers did not stop before the worker shutdown deadline.")
        elif running_ids:
            logger.warning("No worker shutdown time remained; proceeding directly to Manager shutdown.")

        if manager is not None:
            try:
                await stop_application(manager, deadline=shutdown_deadline)
            except BaseException:
                logger.exception("Failed to stop the Manager Bot cleanly.")
        MANAGER_APP = None


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
