"""Isolated tests for selected real functions in bot.py.

The deployment dependencies are intentionally not imported: functions are
compiled from the project's source AST and run against small test doubles.
These tests do not contact Telegram or Firebase.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
import html
import random
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "bot.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
FUNCTIONS = {
    node.name: node
    for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


class BadRequest(Exception):
    pass


class Forbidden(Exception):
    pass


class RetryAfter(Exception):
    def __init__(self, retry_after=1):
        self.retry_after = retry_after
        super().__init__(f"retry after {retry_after}")


class ReactionTypeEmoji:
    def __init__(self, emoji):
        self.emoji = emoji


class DummyInlineKeyboardButton:
    def __init__(self, *args, **kwargs):
        pass


class DummyInlineKeyboardMarkup:
    def __init__(self, *args, **kwargs):
        pass


def load_functions(*names):
    namespace = {
        "asyncio": asyncio,
        "copy": copy,
        "logging": logging,
        "html": html,
        "Update": object,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "OWNER_ID": 8223664417,
        "logger": logging.getLogger("jr-tests"),
        "time": time,
        "random": random,
        "math": __import__("math"),
        "BadRequest": BadRequest,
        "Forbidden": Forbidden,
        "RetryAfter": RetryAfter,
        "ReactionTypeEmoji": ReactionTypeEmoji,
        "InlineKeyboardButton": DummyInlineKeyboardButton,
        "InlineKeyboardMarkup": DummyInlineKeyboardMarkup,
        "BROADCAST_PRUNE_BLOCKED_USERS": False,
        "BROADCAST_INLINE_RETRY_MAX_WAIT": 10.0,
        "REACTION_EMOJIS": ["👍", "❤", "🔥", "🎉"],
        "REACTION_CHAT_CACHE_TTL": 600.0,
        "REACTION_CHAT_CACHE_FALLBACK_TTL": 30.0,
        "REACTION_TRANSIENT_COOLDOWN": 12.0,
        "REACTION_ALL_REJECTED_PAUSE_SECONDS": 15 * 60.0,
        "REACTION_PERMISSION_PAUSE_SECONDS": 3 * 24 * 60 * 60,
        "REACTION_MAX_TRIES_PER_MESSAGE": 8,
        "REACTION_CHAT_PERMISSION_ERROR_TERMS": (
            "not enough rights", "no rights", "administrator rights",
            "not a member", "was kicked", "bot was blocked",
            "have no rights", "chat_write_forbidden",
            "not allowed to send reactions", "reactions are disabled",
            "message_reactions_forbidden", "reaction not allowed",
        ),
        "REACTION_MESSAGE_ERROR_TERMS": (
            "chat not found", "message to react not found", "message not found",
        ),
        "DETACHED_SHUTDOWN_TASKS": set(),
        "BOT_START_LOCKS": {},
        "RUNNING_BOTS": {},
        "SHUTDOWN_STOP_TIMEOUT": 20.0,
        "save_broadcast_delete_job": _noop_async,
        "make_seen_confirmation_url": lambda *args: "https://example.invalid/read",
        "_prune_dead_users": _noop_async,
        "increment_metric": lambda *args, **kwargs: None,
    }
    for name in names:
        node = FUNCTIONS[name]
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


async def _noop_async(*args, **kwargs):
    return None


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_and_group_pages_skip_malformed_child_records(self):
        ns = load_functions(
            "truncate_text", "build_all_channels_page", "build_all_groups_page"
        )
        state = {
            "channels": {
                "good": {"title": "Valid Channel", "username": "valid_channel", "id": -1001},
                "null": None,
                "string": "corrupt-record",
                "number": 123,
            },
            "groups": {
                "good": {"title": "Valid Group", "username": "valid_group", "id": -2001},
                "null": None,
                "list": ["corrupt-record"],
            },
        }

        channels_text, _ = ns["build_all_channels_page"](state, 0)
        groups_text, _ = ns["build_all_groups_page"](state, 0)
        self.assertIn("Valid Channel", channels_text)
        self.assertIn("Total:</b> <code>1", channels_text)
        self.assertIn("Valid Group", groups_text)
        self.assertIn("Total:</b> <code>1", groups_text)

    async def test_uptime_handles_malformed_or_future_start_time(self):
        ns = load_functions("get_uptime")
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"start_time": "not-a-timestamp"})
        )
        self.assertEqual(ns["get_uptime"](context), "0h 0m 0s")
        context.application.bot_data["start_time"] = time.time() + 60
        self.assertEqual(ns["get_uptime"](context), "0h 0m 0s")

    async def test_stats_shows_real_polling_state(self):
        ns = load_functions("get_uptime", "stats_command")

        class FakeMessage:
            text = ""

            async def reply_text(self, text, **kwargs):
                self.text = text

        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ns["OWNER_ID"]),
            message=message,
        )
        app = SimpleNamespace(
            bot_data={
                "state": {"users": {}, "channels": {}, "groups": {}},
                "start_time": time.time(),
            },
            running=False,
            updater=SimpleNamespace(running=False),
        )
        context = SimpleNamespace(application=app)

        await ns["stats_command"](update, context)
        self.assertIn("Polling Inactive", message.text)
        self.assertNotIn("Ultra Fast", message.text)

        app.running = True
        app.updater.running = True
        await ns["stats_command"](update, context)
        self.assertIn("Polling Active", message.text)

    async def test_ptb_empty_sequence_uses_telegram_default_reactions(self):
        ns = load_functions("_strip_vs", "_allowed_reactions_for_chat")

        class FakeBot:
            available = ()  # PTB 22.x representation of an omitted API field.

            async def get_chat(self, chat_id):
                return SimpleNamespace(available_reactions=self.available)

        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}),
            bot=FakeBot(),
        )
        expected = ns["REACTION_EMOJIS"]

        for index, value in enumerate(((), [], None), start=1):
            context.bot.available = value
            context.application.bot_data.pop("chat_reactions_cache", None)
            allowed = await ns["_allowed_reactions_for_chat"](context, -1000 - index)
            self.assertEqual(allowed, expected)

    async def test_restricted_allowlist_is_filtered_to_supported_emojis(self):
        ns = load_functions("_strip_vs", "_allowed_reactions_for_chat")

        class FakeBot:
            async def get_chat(self, chat_id):
                return SimpleNamespace(
                    available_reactions=[SimpleNamespace(emoji="🔥"), SimpleNamespace(emoji="custom")]
                )

        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}),
            bot=FakeBot(),
        )
        allowed = await ns["_allowed_reactions_for_chat"](context, -1002)
        self.assertEqual(allowed, ["🔥"])

    async def test_bad_emoji_tries_next_candidate_and_stops_on_success(self):
        ns = load_functions(
            "_strip_vs", "_allowed_reactions_for_chat",
            "_is_chat_permission_reaction_error", "_is_message_target_reaction_error",
            "_pause_chat_reactions", "_set_reaction_global_cooldown",
            "_retry_after_seconds", "_perform_reaction",
        )
        calls = []

        class FakeBot:
            async def get_chat(self, chat_id):
                return SimpleNamespace(available_reactions=["👍", "🔥"])

            async def set_message_reaction(self, **kwargs):
                calls.append(kwargs["reaction"][0].emoji)
                if len(calls) == 1:
                    raise BadRequest("REACTION_INVALID")

        async def fake_save(*args, **kwargs):
            return None

        ns["schedule_state_save"] = fake_save
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "bot_id": 100,
                "state": {"reaction_chat_pauses": {}},
            }),
            bot=FakeBot(),
        )
        target = SimpleNamespace(chat_id=-1003, message_id=55)
        await ns["_perform_reaction"](context, target)
        self.assertEqual(len(calls), 2)
        self.assertEqual(context.application.bot_data["state"]["reaction_chat_pauses"], {})

    async def test_chat_permission_error_persists_long_pause(self):
        ns = load_functions(
            "_is_chat_permission_reaction_error", "_is_message_target_reaction_error",
            "_is_fatal_reaction_error", "_pause_chat_reactions",
            "_set_reaction_global_cooldown", "_perform_reaction",
        )
        ns["_allowed_reactions_for_chat"] = _fixed_allowlist(["👍", "🔥"])
        ns["schedule_state_save"] = _noop_async

        class FakeBot:
            async def set_message_reaction(self, **kwargs):
                raise Forbidden("bot was blocked from reacting")

        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"state": {"reaction_chat_pauses": {}}}),
            bot=FakeBot(),
        )
        await ns["_perform_reaction"](context, SimpleNamespace(chat_id=-1004, message_id=3))
        pause = context.application.bot_data["state"]["reaction_chat_pauses"]["-1004"]
        self.assertGreater(pause["until"], time.time() + 2 * 24 * 60 * 60)

    async def test_retry_after_sets_worker_cooldown_without_spinning_emoji_list(self):
        ns = load_functions(
            "_is_chat_permission_reaction_error", "_is_message_target_reaction_error",
            "_pause_chat_reactions", "_set_reaction_global_cooldown",
            "_retry_after_seconds", "_perform_reaction"
        )
        ns["_allowed_reactions_for_chat"] = _fixed_allowlist(["👍", "🔥"])
        calls = []

        class FakeBot:
            async def set_message_reaction(self, **kwargs):
                calls.append(kwargs)
                raise RetryAfter(9)

        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"state": {"reaction_chat_pauses": {}}}),
            bot=FakeBot(),
        )
        await ns["_perform_reaction"](context, SimpleNamespace(chat_id=-1005, message_id=4))
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(
            context.application.bot_data["reaction_global_cooldown_until"],
            time.monotonic() + 8,
        )



    async def test_chat_where_every_emoji_is_rejected_is_left_alone_for_a_while(self):
        ns = load_functions(
            "_strip_vs", "_allowed_reactions_for_chat",
            "_is_chat_permission_reaction_error", "_is_message_target_reaction_error",
            "_pause_chat_reactions", "_set_reaction_global_cooldown",
            "_retry_after_seconds", "_perform_reaction",
        )
        ns["schedule_state_save"] = _noop_async
        calls = []

        class FakeBot:
            async def get_chat(self, chat_id):
                return SimpleNamespace(available_reactions=[])   # reactions off / unknown

            async def set_message_reaction(self, **kwargs):
                calls.append(kwargs["message_id"])
                raise BadRequest("REACTION_INVALID")

        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"state": {"reaction_chat_pauses": {}}}),
            bot=FakeBot(),
        )
        await ns["_perform_reaction"](context, SimpleNamespace(chat_id=-1077, message_id=1))
        first_round = len(calls)
        self.assertGreater(first_round, 0)
        until = context.application.bot_data["reaction_cooldowns"][-1077]
        self.assertGreater(until, time.monotonic() + 14 * 60)
        # the next messages in that chat cost no API calls at all
        for message_id in range(2, 8):
            await ns["_perform_reaction"](context, SimpleNamespace(chat_id=-1077, message_id=message_id))
        self.assertEqual(len(calls), first_round)

    async def test_failed_candidate_batch_invalidates_chat_allowlist_cache(self):
        ns = load_functions(
            "_strip_vs", "_allowed_reactions_for_chat",
            "_is_chat_permission_reaction_error", "_is_message_target_reaction_error",
            "_pause_chat_reactions", "_set_reaction_global_cooldown",
            "_retry_after_seconds", "_perform_reaction",
        )
        ns["random"].sample = lambda values, k: list(values)[:k]
        ns["schedule_state_save"] = _noop_async

        class FakeBot:
            calls = 0

            async def get_chat(self, chat_id):
                self.calls += 1
                return SimpleNamespace(available_reactions=["👍", "🔥"])

            async def set_message_reaction(self, **kwargs):
                raise BadRequest("REACTION_INVALID")

        fake_bot = FakeBot()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"state": {"reaction_chat_pauses": {}}}),
            bot=fake_bot,
        )
        target = SimpleNamespace(chat_id=-1099, message_id=88)
        await ns["_perform_reaction"](context, target)
        self.assertNotIn(-1099, context.application.bot_data["chat_reactions_cache"])

    async def test_fatal_reaction_error_classifier_exists_and_distinguishes_permissions(self):
        ns = load_functions(
            "_is_chat_permission_reaction_error", "_is_message_target_reaction_error",
            "_is_fatal_reaction_error",
        )
        self.assertTrue(ns["_is_fatal_reaction_error"](Forbidden("no reaction rights")))
        self.assertTrue(ns["_is_fatal_reaction_error"](BadRequest("message to react not found")))
        self.assertTrue(ns["_is_fatal_reaction_error"](BadRequest("MESSAGE_REACTIONS_FORBIDDEN")))
        self.assertFalse(ns["_is_fatal_reaction_error"](BadRequest("REACTION_INVALID")))

def _fixed_allowlist(values):
    async def _allowed(context, chat_id):
        return list(values)
    return _allowed


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_defers_remaining_recipients_and_reports_counts(self):
        ns = load_functions("_retry_after_seconds", "_is_dead_user_error", "_send_one_bot_broadcast")
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        ns["asyncio"] = SimpleNamespace(sleep=fake_sleep)

        class FakeBot:
            def __init__(self):
                self.rate_limit_calls = 0

            async def send_message(self, chat_id, **kwargs):
                if chat_id == 2:
                    raise Forbidden("bot cannot initiate this chat")
                if chat_id == 3:
                    self.rate_limit_calls += 1
                    raise RetryAfter(15)
                return SimpleNamespace(message_id=chat_id)

        stats = await ns["_send_one_bot_broadcast"](
            FakeBot(), 123, {"1": {}, "2": {}, "3": {}, "4": {}},
            "hello", "normal", 0,
        )
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["forbidden"], 1)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["deferred"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["attempted"], 3)
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.05, places=3)

    async def test_short_retry_after_is_honoured_but_duplicate_chat_ids_are_skipped(self):
        ns = load_functions("_retry_after_seconds", "_is_dead_user_error", "_send_one_bot_broadcast")
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        ns["asyncio"] = SimpleNamespace(sleep=fake_sleep)

        class FakeBot:
            calls = 0

            async def send_message(self, chat_id, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RetryAfter(2)
                return SimpleNamespace(message_id=chat_id)

        stats = await ns["_send_one_bot_broadcast"](
            FakeBot(), 789, {"123": {}, "00123": {}}, "hello", "normal", 0
        )
        self.assertEqual(stats["users"], 1)
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(stats["sent"], 1)
        self.assertGreaterEqual(waits[0], 2.0)

    async def test_invalid_recipient_keys_are_reported(self):
        ns = load_functions("_retry_after_seconds", "_is_dead_user_error", "_send_one_bot_broadcast")

        class FakeBot:
            async def send_message(self, chat_id, **kwargs):
                return SimpleNamespace(message_id=chat_id)

        stats = await ns["_send_one_bot_broadcast"](
            FakeBot(), 456, {"bad-key": {}, "-1": {}, "7": {}}, "hello", "normal", 0
        )
        self.assertEqual(stats["invalid"], 2)
        self.assertEqual(stats["users"], 1)
        self.assertEqual(stats["sent"], 1)


class StateAndShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_state_rejects_malformed_saved_collections(self):
        ns = load_functions("load_state", "normalize_state")

        async def fake_firebase_call(func, *args, **kwargs):
            return func()

        ns["_firebase_call_with_retry"] = fake_firebase_call

        class FakeRef:
            def get(self):
                return {"users": ["not", "a", "mapping"]}

        with self.assertRaisesRegex(RuntimeError, "malformed"):
            await ns["load_state"](FakeRef(), 123, "worker_test")

    async def test_normalize_state_preserves_unknown_fields_and_repairs_metrics(self):
        ns = load_functions("normalize_state")
        normalized = ns["normalize_state"]({
            "users": [],
            "channels": {"x": True},
            "custom_future_field": {"keep": 1},
            "metrics": {"messages_seen": "9", "reactions_sent": "bad"},
        })
        self.assertEqual(normalized["users"], {})
        self.assertEqual(normalized["channels"], {"x": True})
        self.assertEqual(normalized["custom_future_field"], {"keep": 1})
        self.assertEqual(normalized["metrics"]["messages_seen"], 9)
        self.assertEqual(normalized["metrics"]["reactions_sent"], 0)

    async def test_same_telegram_identity_cannot_start_two_pollers(self):
        ns = load_functions(
            "get_bot_start_lock", "_worker_is_running",
            "start_managed_bot_from_record",
        )
        start_calls = []
        cleanup_calls = []

        class FakeApp:
            def __init__(self):
                self.bot = SimpleNamespace(id=555, username="worker_test")
                self.running = False
                self.updater = SimpleNamespace(running=False)
                self.bot_data = {}

        async def fake_build_bot(*args, **kwargs):
            await asyncio.sleep(0.01)
            return FakeApp()

        async def fake_start_application(app):
            start_calls.append(app)
            await asyncio.sleep(0.02)
            app.running = True
            app.updater.running = True

        async def fake_cleanup(app):
            cleanup_calls.append(app)

        async def fake_persist(*args, **kwargs):
            return True

        ns["decrypt_token"] = lambda value: "decrypted"
        ns["build_bot"] = fake_build_bot
        ns["start_application"] = fake_start_application
        ns["_cleanup_failed_application_init"] = fake_cleanup
        ns["_persist_worker_runtime_status"] = fake_persist

        first, second = await asyncio.gather(
            ns["start_managed_bot_from_record"]({"token": "encrypted"}),
            ns["start_managed_bot_from_record"]({"token": "encrypted"}),
        )
        self.assertIs(first, second)
        self.assertEqual(len(start_calls), 1)
        self.assertEqual(len(cleanup_calls), 1)
        self.assertIs(ns["RUNNING_BOTS"][555], first)

    async def test_worker_running_requires_application_and_updater(self):
        ns = load_functions("_worker_is_running")
        self.assertTrue(ns["_worker_is_running"](SimpleNamespace(
            running=True, updater=SimpleNamespace(running=True)
        )))
        self.assertFalse(ns["_worker_is_running"](SimpleNamespace(
            running=True, updater=SimpleNamespace(running=False)
        )))
        self.assertFalse(ns["_worker_is_running"](None))

    async def test_shutdown_wait_respects_deadline_for_stuck_task(self):
        ns = load_functions("_retain_shutdown_task", "_await_shutdown_task")

        async def never_finishes():
            await asyncio.Event().wait()

        task = asyncio.create_task(never_finishes())
        started = time.monotonic()
        result = await ns["_await_shutdown_task"](
            task, time.monotonic() + 0.03, "test stuck task"
        )
        elapsed = time.monotonic() - started
        await asyncio.sleep(0)
        self.assertFalse(result)
        self.assertLess(elapsed, 0.25)
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
