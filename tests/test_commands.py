"""Command behavior with a small AstrBot API double; no chat platform needed."""

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from deals import Game, Store, Watcher


def load_plugin():
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.llm_tool = lambda **kwargs: lambda fn: fn
    api.logger = types.SimpleNamespace(exception=lambda *a: None)
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = object
    event.filter = types.SimpleNamespace(command=lambda *a: lambda f: f)
    star = types.ModuleType("astrbot.api.star")
    star.Context = object

    class StarDouble:
        def __init__(self, context):
            self.context = context

    star.Star = StarDouble
    star.StarTools = object
    star.register = lambda *a: lambda cls: cls
    package = types.ModuleType("_deals_test_plugin")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    import deals

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "_deals_test_plugin": package,
        "_deals_test_plugin.deals": deals,
    }
    spec = importlib.util.spec_from_file_location(
        "_deals_test_plugin.main", Path(package.__path__[0]) / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.GameDealsPlugin


Plugin = load_plugin()


class Event:
    unified_msg_origin = "qq:GroupMessage:123"

    def __init__(self, message="", admin=False, group="123"):
        self.message, self.admin, self.group = message, admin, group

    def get_message_str(self):
        return self.message

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "alice"

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = Plugin(None, {})
        self.plugin.store = Store(Path(self.temp.name) / "db")
        self.plugin.source = AsyncMock()
        self.plugin.source.country = "cn"
        self.game = Game("123", "Hades", "CNY", 10000, 5000, 50)
        self.plugin.source.game.return_value = self.game
        self.plugin.limit = 50
        self.plugin.watcher = Watcher(self.plugin.store, self.plugin.source, AsyncMock())

    async def asyncTearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    async def test_group_settings_require_astrbot_admin(self):
        with self.assertRaises(ValueError):
            await self.plugin.handle(Event(), ["时间", "20:30"])
        await self.plugin.handle(Event(admin=True), ["时间", "20:30"])
        self.assertEqual(self.plugin.store.settings(Event.unified_msg_origin)["time"], "20:30")

    async def test_private_user_can_configure_own_time(self):
        event = Event(group="")
        event.unified_msg_origin = "qq:FriendMessage:alice"
        await self.plugin.handle(event, ["时间", "20:30"])
        self.assertEqual(self.plugin.store.settings(event.unified_msg_origin)["time"], "20:30")

    async def test_subscribe_manual_check_and_unsubscribe(self):
        event = Event()
        self.assertIn("已订阅", await self.plugin.handle(event, ["订阅", "123"]))
        self.assertIn("Hades", await self.plugin.handle(event, ["检查"]))
        row = self.plugin.store.subscriptions(event.unified_msg_origin)[0]
        self.assertIsNone(row["last_checked"])
        self.assertIn("已退订", await self.plugin.handle(event, ["退订", "123"]))

    async def test_game_name_spaces_and_apostrophe(self):
        self.plugin.source.search.return_value = [("123", "Don't Starve Together")]
        results = [
            r async for r in self.plugin.game_deals(Event("游戏折扣 搜索 Don't Starve Together"))
        ]
        self.plugin.source.search.assert_awaited_once_with("Don't Starve Together")
        self.assertIn("123", results[0])

    async def test_invalid_time_returns_user_message(self):
        result = [r async for r in self.plugin.game_deals(Event("游戏折扣 时间 25:00", admin=True))]
        self.assertIn("HH:MM", result[0])

    async def test_long_reply_split_into_bounded_messages(self):
        self.plugin.source.search.return_value = [("123", "游戏" * 2000)]
        results = [r async for r in self.plugin.game_deals(Event("游戏折扣 搜索 test"))]
        self.assertGreater(len(results), 1)
        self.assertTrue(all(len(r) <= 2800 for r in results))


class JumpCommandTests(CommandTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin.default_source = "jump"
        self.plugin.jump = AsyncMock()
        self.plugin.jump.max_pages = 5
        from deals import Game

        self.plugin.jump.game.return_value = Game(
            "jump:53387",
            "马力欧卡丁车：世界",
            "CNY≈",
            37509,
            37509,
            source="Jump",
            platform="Switch 2",
        )
        from types import SimpleNamespace

        async def route(id_):
            return await (self.plugin.jump if id_.startswith("jump:") else self.plugin.source).game(
                id_
            )

        self.plugin.router = SimpleNamespace(game=route)

    # Reuse general permission/reply tests with explicit legacy Steam mode where needed.
    async def test_game_name_spaces_and_apostrophe(self):
        self.plugin.source.search.return_value = [("123", "Don't Starve Together")]
        result = await self.plugin.handle(Event(), ["steam", "搜索", "Don't", "Starve", "Together"])
        self.plugin.source.search.assert_awaited_once_with("Don't Starve Together")
        self.assertIn("steam:123", result)

    async def test_subscribe_manual_check_and_unsubscribe(self):
        event = Event()
        self.assertIn("已订阅", await self.plugin.handle(event, ["订阅", "jump:53387"]))
        self.plugin.jump.game.assert_awaited_once_with("jump:53387")
        self.plugin.source.game.assert_not_awaited()
        self.assertIn("马力欧", await self.plugin.handle(event, ["列表"]))
        self.assertIn("已退订", await self.plugin.handle(event, ["退订", "jump:53387"]))

    async def test_long_reply_split_into_bounded_messages(self):
        self.plugin.jump.search.return_value = [("jump:53387", "游戏" * 2000)]
        results = [r async for r in self.plugin.game_deals(Event("游戏折扣 搜索 test"))]
        self.assertGreater(len(results), 1)
        self.assertTrue(all(len(r) <= 2800 for r in results))

    async def test_explicit_steam_id_still_routes_under_jump_default(self):
        await self.plugin.handle(Event(), ["订阅", "steam:123"])
        self.plugin.source.game.assert_awaited_once_with("123")
        self.plugin.jump.game.assert_not_awaited()
        self.assertIn("steam:123", await self.plugin.handle(Event(), ["列表"]))

    async def test_default_search_uses_jump(self):
        self.plugin.jump.search.return_value = [("jump:53387", "马力欧卡丁车：世界 [Switch 2]")]
        result = await self.plugin.handle(Event(), ["搜索", "马力欧"])
        self.assertIn("jump:53387", result)
        self.plugin.jump.search.assert_awaited_once_with("马力欧")
        self.plugin.source.search.assert_not_awaited()
