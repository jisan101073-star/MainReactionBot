"""Tests for group_manager.py using small stand-ins for python-telegram-bot.

Run with:  python -m unittest discover -s tests
No Telegram or Firebase connection is used.
"""

import asyncio
import os
import sys
import types
import unittest
from types import SimpleNamespace as NS


# ---------------------------------------------------------------------------
# Stand-ins for the parts of python-telegram-bot that group_manager uses.
# ---------------------------------------------------------------------------

class TelegramError(Exception):
    pass


class BadRequest(TelegramError):
    pass


class Forbidden(TelegramError):
    pass


class RetryAfter(TelegramError):
    def __init__(self, seconds):
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = seconds


class ApplicationHandlerStop(Exception):
    pass


class InlineKeyboardButton:
    def __init__(self, text, **kwargs):
        self.text = text
        self.callback_data = kwargs.get("callback_data")
        self.url = kwargs.get("url")
        self.style = kwargs.get("style")


class InlineKeyboardMarkup:
    def __init__(self, rows):
        self.inline_keyboard = rows


class CallbackQueryHandler:
    def __init__(self, callback, pattern=None):
        self.callback = callback
        self.pattern = pattern


class MessageHandler:
    def __init__(self, filters, callback):
        self.filters = filters
        self.callback = callback


class _Filter:
    def __and__(self, other):
        return self

    def __invert__(self):
        return self


_filters = NS(TEXT=_Filter(), COMMAND=_Filter(), ChatType=NS(PRIVATE=_Filter()))


def _install_stubs():
    telegram = types.ModuleType("telegram")
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    error = types.ModuleType("telegram.error")
    error.BadRequest, error.Forbidden = BadRequest, Forbidden
    error.RetryAfter, error.TelegramError = RetryAfter, TelegramError
    ext = types.ModuleType("telegram.ext")
    ext.ApplicationHandlerStop = ApplicationHandlerStop
    ext.CallbackQueryHandler = CallbackQueryHandler
    ext.MessageHandler = MessageHandler
    ext.filters = _filters
    sys.modules["telegram"] = telegram
    sys.modules["telegram.error"] = error
    sys.modules["telegram.ext"] = ext


_REAL_TELEGRAM = {k: sys.modules.get(k) for k in ("telegram", "telegram.error", "telegram.ext")}
_install_stubs()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.pop("group_manager", None)
import group_manager as gm  # noqa: E402


def tearDownModule():
    # Put the real library back if it was installed, so other tests are unaffected.
    for key, value in _REAL_TELEGRAM.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value
    sys.modules.pop("group_manager", None)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

OWNER = 111
BOT_ID = 7001
GROUP_A = -1001111111111
GROUP_B = -1002222222222


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.edits = []
        self.replies = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def callback_update(data, user_id=OWNER):
    query = FakeQuery(data)
    return NS(effective_user=NS(id=user_id), callback_query=query,
              effective_chat=NS(id=user_id), effective_message=query.message), query


def text_update(text, user_id=OWNER):
    message = FakeMessage(text)
    return NS(effective_user=NS(id=user_id), callback_query=None,
              effective_chat=NS(id=user_id), effective_message=message), message


class FakeWorkerBot:
    def __init__(self, fail_with=None, username="Jr_Auto_Reaction_1_Bot"):
        self.sent = []
        self.fail_with = fail_with
        self.username = username
        self.initialized = False
        self.shut_down = False

    async def initialize(self):
        self.initialized = True

    async def shutdown(self):
        self.shut_down = True

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((chat_id, text, reply_markup))
        return NS(message_id=555)


class Env:
    """Builds a ctx like bot.py would pass to register()."""

    def __init__(self, running=True, groups=None, firebase_groups=None, fail_with=None):
        self.worker_bot = FakeWorkerBot(fail_with=fail_with)
        self.temp_bots = []
        self.jobs = []
        self.firebase_reads = []
        self.firebase_groups = firebase_groups
        groups = groups if groups is not None else {
            str(GROUP_A): {"id": GROUP_A, "title": "Alpha Group", "username": ""},
            str(GROUP_B): {"id": GROUP_B, "title": "Beta Group", "username": ""},
        }
        self.app_data = {}
        running_bots = {}
        if running:
            running_bots[BOT_ID] = NS(bot=self.worker_bot, bot_data={"state": {"groups": groups}})
        registry = {str(BOT_ID): {"bot_id": BOT_ID, "bot_username": "Jr_Auto_Reaction_1_Bot",
                                  "token": "enc", "enabled": True}}

        async def get_managed_bots(force=False, require_fresh=False):
            return registry

        async def save_job(bot_id, chat_id, message_id, delete_at):
            self.jobs.append((bot_id, chat_id, message_id, delete_at))

        async def firebase_call(func, *args, **kwargs):
            return func(*args, **kwargs)

        env = self

        class Ref:
            def __init__(self, path):
                self.path = path

            def get(self):
                env.firebase_reads.append(self.path)
                return env.firebase_groups

        db = NS(reference=lambda path: Ref(path))

        outer = self

        class TempBot(FakeWorkerBot):
            def __init__(self, token=None):
                super().__init__(fail_with=outer.worker_bot.fail_with)
                self.token = token
                outer.temp_bots.append(self)

        self.ctx = NS(
            owner_id=OWNER, get_managed_bots=get_managed_bots, running_bots=running_bots,
            decrypt_token=lambda value: "plain-token", save_delete_job=save_job,
            firebase_call=firebase_call, db=db, bot_class=TempBot,
            is_worker_running=lambda app: True,
        )
        self.context = NS(application=NS(bot_data={gm._CTX_KEY: self.ctx}))


def run(coro):
    return asyncio.run(coro)


def all_callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def all_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class RegisterTests(unittest.TestCase):
    def test_registers_handlers_in_early_groups_only(self):
        added = []
        app = NS(bot_data={}, add_handler=lambda handler, group=0: added.append((handler, group)))
        gm.register(app, owner_id=OWNER, get_managed_bots=None, running_bots={}, decrypt_token=None,
                    save_delete_job=None, firebase_call=None, db=None, bot_class=None)
        self.assertEqual(len(added), 4)
        self.assertTrue(all(group < 0 for _, group in added), "must never share the Manager's group 0")
        self.assertIn(gm._CTX_KEY, app.bot_data)

    def test_failure_while_preparing_leaves_manager_untouched(self):
        added = []
        app = NS(bot_data={}, add_handler=lambda handler, group=0: added.append(handler))
        with self.assertRaises(ValueError):
            gm.register(app, owner_id="not-a-number", get_managed_bots=None, running_bots={},
                        decrypt_token=None, save_delete_job=None, firebase_call=None, db=None, bot_class=None)
        self.assertEqual(added, [])
        self.assertNotIn(gm._CTX_KEY, app.bot_data)


class ScreenTests(unittest.TestCase):
    def test_non_owner_is_refused(self):
        env = Env()
        update, query = callback_update("gm:home", user_id=999)
        run(gm.gm_callback(update, env.context))
        self.assertEqual(query.answers, [("Owner only.", True)])
        self.assertEqual(query.message.edits, [])

    def test_bot_list_and_group_list_from_running_worker_memory(self):
        env = Env(running=True)
        update, query = callback_update("gm:home")
        run(gm.gm_callback(update, env.context))
        text, markup = query.message.edits[-1]
        self.assertIn(f"gm:b:{BOT_ID}:0", all_callbacks(markup))

        update, query = callback_update(f"gm:b:{BOT_ID}:0")
        run(gm.gm_callback(update, env.context))
        text, markup = query.message.edits[-1]
        self.assertIn("Alpha Group", text)
        self.assertIn("Beta Group", text)
        self.assertIn(f"gm:g:{BOT_ID}:{GROUP_A}:0", all_callbacks(markup))
        self.assertEqual(env.firebase_reads, [], "a running worker is read from memory, not Firebase")
        self.assertNotIn("Total", text, "no counts are shown")

    def test_stopped_worker_reads_firebase_groups_node_only(self):
        env = Env(running=False, firebase_groups={str(GROUP_A): {"title": "Saved Group"}})
        update, query = callback_update(f"gm:b:{BOT_ID}:0")
        run(gm.gm_callback(update, env.context))
        self.assertEqual(env.firebase_reads, [f"reaction_bots/{BOT_ID}/groups"])
        self.assertIn("Saved Group", query.message.edits[-1][0])

    def test_channels_collection_is_never_read(self):
        env = Env(running=False, firebase_groups={})
        run(gm._load_groups(env.ctx, BOT_ID))
        self.assertTrue(all(path.endswith("/groups") for path in env.firebase_reads))

    def test_html_in_group_title_is_escaped(self):
        env = Env(groups={str(GROUP_A): {"title": "<b>Evil</b> & co"}})
        update, query = callback_update(f"gm:b:{BOT_ID}:0")
        run(gm.gm_callback(update, env.context))
        text = query.message.edits[-1][0]
        self.assertNotIn("<b>Evil</b>", text)
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; &amp; co", text)

    def test_pagination(self):
        groups = {str(-1000000000000 - i): {"title": f"G{i:02d}"} for i in range(20)}
        env = Env(groups=groups)
        update, query = callback_update(f"gm:b:{BOT_ID}:0")
        run(gm.gm_callback(update, env.context))
        _, markup = query.message.edits[-1]
        self.assertIn(f"gm:b:{BOT_ID}:1", all_callbacks(markup))
        self.assertEqual(len([c for c in all_callbacks(markup) if c.startswith("gm:g:")]), gm.PAGE_SIZE)

    def test_callback_data_fits_telegram_limit(self):
        data = f"gm:s:{8223664417}:{-1001234567890}:123"
        self.assertLessEqual(len(data.encode()), 64)


class SendFlowTests(unittest.TestCase):
    def _compose(self, env, text="Hello group"):
        update, query = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, message = text_update(text)
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        return message

    def test_full_flow_permanent_message_has_add_button_and_no_job(self):
        env = Env()
        message = self._compose(env)
        preview, markup = message.replies[-1]
        self.assertIn("Hello group", preview)
        self.assertIn("gm:t:0", all_callbacks(markup))

        update, query = callback_update("gm:t:0")
        run(gm.gm_callback(update, env.context))
        self.assertIn("gm:go", all_callbacks(query.message.edits[-1][1]))
        self.assertEqual(env.worker_bot.sent, [], "nothing is sent before confirmation")

        update, query = callback_update("gm:go")
        run(gm.gm_callback(update, env.context))
        chat_id, text, markup = env.worker_bot.sent[0]
        self.assertEqual((chat_id, text), (GROUP_A, "Hello group"))
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.text, "➕ Add Bot to Group")
        self.assertEqual(button.url, "https://t.me/Jr_Auto_Reaction_1_Bot?startgroup=true")
        self.assertEqual(env.jobs, [])
        self.assertIn("permanently", query.message.edits[-1][0])

    def test_timer_saves_exactly_one_delete_job(self):
        env = Env()
        self._compose(env)
        for data in ("gm:t:21600", "gm:go"):
            update, query = callback_update(data)
            run(gm.gm_callback(update, env.context))
        self.assertEqual(len(env.jobs), 1)
        bot_id, chat_id, message_id, delete_at = env.jobs[0]
        self.assertEqual((bot_id, chat_id, message_id), (BOT_ID, GROUP_A, 555))
        import time
        self.assertAlmostEqual(delete_at - time.time(), 21600, delta=5)

    def test_double_tap_on_send_cannot_send_twice(self):
        env = Env()
        self._compose(env)
        for data in ("gm:t:0", "gm:go", "gm:go"):
            update, query = callback_update(data)
            run(gm.gm_callback(update, env.context))
        self.assertEqual(len(env.worker_bot.sent), 1)

    def test_timer_never_reaches_48_hours(self):
        self.assertTrue(all(seconds < 48 * 3600 for seconds, _ in gm.TIMER_CHOICES))

    def test_invalid_timer_is_rejected(self):
        env = Env()
        self._compose(env)
        update, query = callback_update("gm:t:999999999")
        run(gm.gm_callback(update, env.context))
        self.assertEqual(query.answers[-1], ("Invalid choice.", True))
        self.assertEqual(env.worker_bot.sent, [])

    def test_stopped_worker_uses_temporary_bot_and_closes_it(self):
        env = Env(running=False, firebase_groups={str(GROUP_A): {"title": "Alpha Group"}})
        self._compose(env)
        for data in ("gm:t:0", "gm:go"):
            update, query = callback_update(data)
            run(gm.gm_callback(update, env.context))
        self.assertEqual(len(env.temp_bots), 1)
        temp = env.temp_bots[0]
        self.assertTrue(temp.initialized and temp.shut_down)
        self.assertEqual(temp.token, "plain-token")
        self.assertEqual(len(temp.sent), 1)

    def test_text_handler_ignores_text_when_no_compose_session(self):
        env = Env()
        update, message = text_update("a bot token or broadcast text")
        run(gm.gm_text(update, env.context))  # must neither raise Stop nor reply
        self.assertEqual(message.replies, [])

    def test_text_handler_ignores_non_owner(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, message = text_update("hi", user_id=999)
        run(gm.gm_text(update, env.context))
        self.assertEqual(message.replies, [])

    def test_too_long_message_is_refused_and_session_kept(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, message = text_update("x" * (gm.MAX_MESSAGE_CHARS + 1))
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertIn("limit", message.replies[-1][0])
        update, message = text_update("short now")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertIn("short now", message.replies[-1][0])

    def test_other_manager_action_abandons_compose(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, _ = callback_update("mgr:add")
        run(gm.gm_reset_on_other_action(update, env.context))
        update, message = text_update("this is a bot token")
        run(gm.gm_text(update, env.context))
        self.assertEqual(message.replies, [], "text now belongs to the Manager's own handler")

    def test_cancel_clears_session(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, query = callback_update("gm:x")
        run(gm.gm_callback(update, env.context))
        self.assertIsNone(gm._get_pending(env.context, OWNER))

    def test_expired_session_is_dropped(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        gm._pending_store(env.context)[OWNER]["created"] -= gm.PENDING_TTL_SECONDS + 1
        update, message = text_update("late text")
        run(gm.gm_text(update, env.context))
        self.assertEqual(message.replies, [])

    def test_group_left_before_confirm_sends_nothing(self):
        env = Env()
        self._compose(env)
        for data in ("gm:t:0",):
            update, query = callback_update(data)
            run(gm.gm_callback(update, env.context))
        env.ctx.running_bots[BOT_ID].bot_data["state"]["groups"].pop(str(GROUP_A))
        update, query = callback_update("gm:go")
        run(gm.gm_callback(update, env.context))
        self.assertEqual(env.worker_bot.sent, [])
        self.assertIn("no longer in this group", query.message.edits[-1][0])


class ErrorTests(unittest.TestCase):
    def _send_with(self, error):
        env = Env(fail_with=error)
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, _ = text_update("hello")
        try:
            run(gm.gm_text(update, env.context))
        except ApplicationHandlerStop:
            pass
        for data in ("gm:t:3600", "gm:go"):
            update, query = callback_update(data)
            run(gm.gm_callback(update, env.context))
        return env, query.message.edits[-1][0]

    def test_no_permission_to_send(self):
        env, text = self._send_with(BadRequest("Bad Request: not enough rights to send text messages to the chat"))
        self.assertIn("does not let the bot send messages", text)
        self.assertEqual(env.jobs, [], "no delete job for a message that was never sent")

    def test_bot_removed_or_blocked(self):
        env, text = self._send_with(Forbidden("Forbidden: bot was kicked from the group chat"))
        self.assertIn("not allowed to post", text)
        self.assertEqual(env.jobs, [])

    def test_rate_limit(self):
        env, text = self._send_with(RetryAfter(17))
        self.assertIn("17 seconds", text)
        self.assertEqual(env.jobs, [])

    def test_job_save_failure_is_reported_honestly(self):
        env = Env()

        async def broken(*args, **kwargs):
            raise RuntimeError("firebase down")

        env.ctx.save_delete_job = broken
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, _ = text_update("hello")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        for data in ("gm:t:3600", "gm:go"):
            update, query = callback_update(data)
            run(gm.gm_callback(update, env.context))
        text = query.message.edits[-1][0]
        self.assertEqual(len(env.worker_bot.sent), 1)
        self.assertIn("could NOT be saved", text)


class ParseHoursTests(unittest.TestCase):
    def test_accepted_values(self):
        cases = {"1": 3600, "2": 7200, "5.5": 19800, "5,5": 19800, "2h": 7200, "2 hours": 7200,
                 "47": 169200, "47.0": 169200, " 3 ": 10800, "০২": 7200, "২.৫": 9000,
                 "0.02": 72, "0.5": 1800}
        for raw, expected in cases.items():
            seconds, error = gm.parse_hours(raw)
            self.assertIsNone(error, raw)
            self.assertEqual(seconds, expected, raw)

    def test_more_than_47_hours_is_never_accepted(self):
        for raw in ("47.01", "47.5", "48", "49", "100", "1000000", "9" * 400, "২৪০"):
            seconds, error = gm.parse_hours(raw)
            self.assertIsNone(seconds, raw)
            self.assertIn("47 hours", error, raw)

    def test_too_short_and_garbage_are_refused(self):
        for raw in ("0", "0.001", "0.01"):
            seconds, error = gm.parse_hours(raw)
            self.assertIsNone(seconds, raw)
            self.assertIn("1 minute", error, raw)
        for raw in ("", "abc", "-5", "5 days", "1e3", "inf", "nan", "2:30", "1.2.3"):
            seconds, error = gm.parse_hours(raw)
            self.assertIsNone(seconds, raw)
            self.assertIn("number of hours", error, raw)

    def test_duration_text(self):
        self.assertEqual(gm._format_duration(3600), "1 hour")
        self.assertEqual(gm._format_duration(19800), "5 hours 30 minutes")
        self.assertEqual(gm._format_duration(169200), "47 hours")
        self.assertEqual(gm._format_duration(90), "1 minute 30 seconds")


class CustomTimerFlowTests(unittest.TestCase):
    def _to_preview(self, env):
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, _ = text_update("Hello group")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))

    def _custom(self, env, typed):
        update, query = callback_update("gm:tc")
        run(gm.gm_callback(update, env.context))
        update, message = text_update(typed)
        try:
            run(gm.gm_text(update, env.context))
        except ApplicationHandlerStop:
            pass
        return query, message

    def test_preview_offers_custom_hours_button(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, message = text_update("Hello")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertIn("gm:tc", all_callbacks(message.replies[-1][1]))

    def test_typed_hours_flow_sends_and_schedules_exact_time(self):
        env = Env()
        self._to_preview(env)
        query, message = self._custom(env, "5.5")
        self.assertIn("47 hours", query.message.edits[-1][0])
        self.assertIn("gm:go", all_callbacks(message.replies[-1][1]))
        self.assertIn("5 hours 30 minutes", message.replies[-1][0])
        update, query = callback_update("gm:go")
        run(gm.gm_callback(update, env.context))
        self.assertEqual(len(env.worker_bot.sent), 1)
        import time
        self.assertAlmostEqual(env.jobs[0][3] - time.time(), 19800, delta=5)

    def test_over_47_hours_is_refused_and_nothing_is_sent(self):
        env = Env()
        self._to_preview(env)
        _, message = self._custom(env, "48")
        self.assertIn("47 hours", message.replies[-1][0])
        self.assertEqual(env.worker_bot.sent, [])
        self.assertEqual(env.jobs, [])
        # the session is still waiting for a valid number
        self.assertEqual(gm._get_pending(env.context, OWNER)["step"], "custom_timer")
        update, message = text_update("47")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertEqual(gm._get_pending(env.context, OWNER)["seconds"], 169200)

    def test_bangla_digits_work(self):
        env = Env()
        self._to_preview(env)
        self._custom(env, "২")
        self.assertEqual(gm._get_pending(env.context, OWNER)["seconds"], 7200)

    def test_back_from_custom_returns_to_preview(self):
        env = Env()
        self._to_preview(env)
        update, _ = callback_update("gm:tc")
        run(gm.gm_callback(update, env.context))
        update, query = callback_update("gm:back_timer")
        run(gm.gm_callback(update, env.context))
        self.assertIn("gm:t:0", all_callbacks(query.message.edits[-1][1]))
        self.assertEqual(gm._get_pending(env.context, OWNER)["step"], "timer")

    def test_do_send_refuses_out_of_range_time_before_sending(self):
        env = Env()
        for bad in (30, gm.MAX_TIMER_SECONDS + 1, 10 ** 9):
            pending = dict(bot_id=BOT_ID, chat_id=GROUP_A, text="hi", seconds=bad,
                           username="u", title="t")
            ok, text = run(gm._do_send(env.ctx, pending))
            self.assertFalse(ok)
            self.assertIn("47 hours", text)
        self.assertEqual(env.worker_bot.sent, [])
        self.assertEqual(env.jobs, [])


class SmallFixTests(unittest.TestCase):
    def test_empty_message_gets_a_reply_and_does_not_leak_to_manager(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, message = text_update("   \n  ")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertIn("empty", message.replies[-1][0])

    def test_message_is_stripped(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, _ = text_update("\n  hello  \n")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertEqual(gm._get_pending(env.context, OWNER)["text"], "hello")

    def test_length_limit_counts_emoji_like_telegram(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, message = text_update("😀" * 2000)          # 4000 UTF-16 units
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertIn("limit", message.replies[-1][0])
        self.assertEqual(gm._get_pending(env.context, OWNER)["step"], "text")
        update, message = text_update("😀" * 1700)          # 3400 units: fine
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        self.assertEqual(gm._get_pending(env.context, OWNER)["step"], "timer")

    def test_page_is_remembered_for_back_buttons(self):
        groups = {str(-1000000000000 - i): {"title": f"G{i:02d}"} for i in range(20)}
        env = Env(groups=groups)
        update, query = callback_update(f"gm:b:{BOT_ID}:1")
        run(gm.gm_callback(update, env.context))
        group_buttons = [c for c in all_callbacks(query.message.edits[-1][1]) if c.startswith("gm:g:")]
        self.assertTrue(all(c.endswith(":1") for c in group_buttons))
        update, query = callback_update(group_buttons[0])
        run(gm.gm_callback(update, env.context))
        panel = all_callbacks(query.message.edits[-1][1])
        self.assertIn(f"gm:b:{BOT_ID}:1", panel)
        chat_id = group_buttons[0].split(":")[3]
        self.assertIn(f"gm:s:{BOT_ID}:{chat_id}:1", panel)
        # cancel goes back to the same page
        update, _ = callback_update(f"gm:s:{BOT_ID}:{chat_id}:1")
        run(gm.gm_callback(update, env.context))
        update, query = callback_update("gm:x")
        run(gm.gm_callback(update, env.context))
        self.assertIn(f"gm:b:{BOT_ID}:1", all_callbacks(query.message.edits[-1][1]))

    def test_compose_button_is_answered_once_and_before_slow_work(self):
        env = Env()
        env.ctx.running_bots.clear()
        env.firebase_groups = {}                     # the group is gone
        update, query = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}:0")
        run(gm.gm_callback(update, env.context))
        self.assertEqual(query.answers, [(None, False)], "answered exactly once, no second alert")
        self.assertIn("no longer in the list", query.message.edits[-1][0])
        self.assertIsNone(gm._get_pending(env.context, OWNER))

    def test_failure_after_ack_still_tells_the_owner(self):
        env = Env()

        async def broken_registry(force=False, require_fresh=False):
            raise RuntimeError("firebase down")

        env.ctx.get_managed_bots = broken_registry
        update, query = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}:0")
        # make the second answer() fail like Telegram does for an already answered query
        original = query.answer
        calls = {"n": 0}

        async def answer(text=None, show_alert=False):
            calls["n"] += 1
            if calls["n"] > 1:
                raise BadRequest("query is too old or already answered")
            await original(text, show_alert)

        query.answer = answer
        run(gm.gm_callback(update, env.context))
        self.assertTrue(any("Something went wrong" in r[0] for r in query.message.replies))

    def test_timer_session_stays_alive_while_owner_is_choosing(self):
        env = Env()
        update, _ = callback_update(f"gm:s:{BOT_ID}:{GROUP_A}")
        run(gm.gm_callback(update, env.context))
        update, _ = text_update("hello")
        with self.assertRaises(ApplicationHandlerStop):
            run(gm.gm_text(update, env.context))
        pending = gm._get_pending(env.context, OWNER)
        pending["created"] -= gm.PENDING_TTL_SECONDS - 5   # almost expired
        almost_expired_at = pending["created"]
        update, _ = callback_update("gm:t:3600")
        run(gm.gm_callback(update, env.context))
        import time
        refreshed = gm._get_pending(env.context, OWNER)["created"]
        self.assertGreater(refreshed, almost_expired_at + gm.PENDING_TTL_SECONDS / 2)
        self.assertAlmostEqual(refreshed, time.time(), delta=5)



if __name__ == "__main__":
    unittest.main()
