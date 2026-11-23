"""/start must stay silent in groups (Telegram posts it when a bot is added by link)
but keep working in private chats. Runs without Telegram or Firebase."""
import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

SOURCE = Path(__file__).resolve().parents[1] / "bot.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def _constant(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in bot.py")


def _load_start_command(reply_in_groups):
    node = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_command")
    calls = {"save": 0}

    async def save_state(context):
        calls["save"] += 1
        return True

    async def schedule_state_save(context, delay=0):
        pass

    namespace = {
        "Update": object,
        "ContextTypes": NS(DEFAULT_TYPE=object),
        "REPLY_TO_START_IN_GROUPS": reply_in_groups,
        "get_user_display_name": lambda user: "Tester",
        "get_main_menu": lambda name, username, user_id: ("welcome", "markup"),
        "IMAGE_URL": "https://example.invalid/x.jpg",
        "save_state": save_state,
        "schedule_state_save": schedule_state_save,
        "logger": NS(warning=lambda *a, **k: None),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["start_command"], calls


class FakeBot:
    username = "Jr_Auto_Reaction_1_Bot"

    def __init__(self):
        self.photos = []

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)


def _run(chat_type, reply_in_groups=False):
    start_command, calls = _load_start_command(reply_in_groups)
    bot = FakeBot()
    state = {"users": {}}
    context = NS(bot=bot, application=NS(bot_data={"state": state}))
    update = NS(
        effective_chat=NS(type=chat_type, id=-100123 if chat_type != "private" else 42),
        effective_user=NS(id=42, username="u"),
        message=None,
    )
    asyncio.run(start_command(update, context))
    return bot, state


class StartInGroupsTests(unittest.TestCase):
    def test_default_is_silent_in_groups(self):
        self.assertIs(_constant("REPLY_TO_START_IN_GROUPS"), False)

    def test_group_and_supergroup_get_no_menu_and_save_nothing(self):
        for chat_type in ("group", "supergroup"):
            bot, state = _run(chat_type)
            self.assertEqual(bot.photos, [], chat_type)
            self.assertEqual(state["users"], {}, chat_type)

    def test_private_chat_still_gets_menu_and_user_is_saved(self):
        bot, state = _run("private")
        self.assertEqual(len(bot.photos), 1)
        self.assertIn("42", state["users"])

    def test_switch_brings_the_group_menu_back(self):
        bot, _ = _run("supergroup", reply_in_groups=True)
        self.assertEqual(len(bot.photos), 1)


if __name__ == "__main__":
    unittest.main()
