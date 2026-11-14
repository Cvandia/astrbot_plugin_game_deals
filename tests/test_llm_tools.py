"""LLM tools return JSON to the model and share the command permission checks."""

import json
import unittest
from unittest.mock import AsyncMock

import test_commands
from test_commands import Event

from deals import Game, SourceError


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_commands.CommandTests.asyncSetUp(self)
        self.plugin.default_source = "jump"
        self.plugin.jump = AsyncMock()
        self.plugin.jump.max_pages = 5
        self.plugin.jump.game.return_value = Game(
            "jump:53387",
            "马力欧卡丁车：世界",
            "CNY≈",
            40000,
            20000,
            50,
            source="Jump",
            platform="Switch 2",
            region="日本",
        )

        async def route(id_):
            return await (self.plugin.jump if id_.startswith("jump:") else self.plugin.source).game(
                id_
            )

        self.plugin.router = type("Router", (), {})()
        self.plugin.router.game = route

    async def asyncTearDown(self):
        await test_commands.CommandTests.asyncTearDown(self)

    async def test_search_default_jump_preserves_full_name(self):
        self.plugin.jump.search.return_value = [("jump:53387", "马力欧卡丁车：世界 [Switch 2]")]
        result = json.loads(await self.plugin.tool_search_games(Event(), "马力欧 卡丁车"))
        self.assertTrue(result["ok"])
        self.assertIn("jump:53387", result["result"])
        self.plugin.jump.search.assert_awaited_once_with("马力欧 卡丁车")
        self.assertEqual(self.plugin.store.subscriptions(Event.unified_msg_origin), [])

    async def test_steam_search_returns_namespaced_ids(self):
        self.plugin.source.search.return_value = [("123", "Hades")]
        result = json.loads(await self.plugin.tool_search_games(Event(), "Hades", "steam"))
        self.assertIn("steam:123", result["result"])

    async def test_today_keeps_platform_region_and_currency(self):
        self.plugin.jump.today.return_value = [self.plugin.jump.game.return_value]
        result = json.loads(await self.plugin.tool_today_deals(Event(), limit=3.0))
        self.assertTrue(result["ok"])
        for text in ["Switch 2", "日本", "CNY≈", "非全站完整"]:
            self.assertIn(text, result["result"])
        self.plugin.jump.today.assert_awaited_once_with(3)

    async def test_query_does_not_implicitly_subscribe(self):
        result = json.loads(await self.plugin.tool_game_price(Event(), "jump:53387"))
        self.assertTrue(result["ok"])
        self.assertIn("Jump", result["result"])
        self.assertEqual(self.plugin.store.subscriptions(Event.unified_msg_origin), [])

    async def test_subscribe_list_check_unsubscribe(self):
        event = Event()
        for action in ["subscribe", "subscribe"]:
            result = json.loads(await self.plugin.tool_subscriptions(event, action, "jump:53387"))
            self.assertTrue(result["ok"])
        self.assertEqual(len(self.plugin.store.subscriptions(event.unified_msg_origin)), 1)
        listed = json.loads(await self.plugin.tool_subscriptions(event, "list"))
        self.assertIn("jump:53387", listed["result"])
        checked = json.loads(await self.plugin.tool_subscriptions(event, "check"))
        self.assertIn("马力欧", checked["result"])
        self.assertIsNone(
            self.plugin.store.subscriptions(event.unified_msg_origin)[0]["last_checked"]
        )
        self.assertTrue(
            json.loads(await self.plugin.tool_subscriptions(event, "unsubscribe", "jump:53387"))[
                "ok"
            ]
        )
        self.assertFalse(self.plugin.store.subscriptions(event.unified_msg_origin))

    async def test_tool_cannot_remove_another_users_subscription(self):
        self.plugin.store.subscribe(
            Event.unified_msg_origin, self.plugin.jump.game.return_value, "bob", 50
        )
        result = json.loads(
            await self.plugin.tool_subscriptions(Event(), "unsubscribe", "jump:53387")
        )
        self.assertFalse(result["ok"])
        self.assertIn("创建者", result["error"])
        self.assertEqual(len(self.plugin.store.subscriptions(Event.unified_msg_origin)), 1)

    async def test_group_schedule_cannot_bypass_admin(self):
        for action, time in [("set_time", "20:30"), ("pause", ""), ("resume", "")]:
            result = json.loads(await self.plugin.tool_schedule(Event(), action, time))
            self.assertFalse(result["ok"])
            self.assertIn("管理员", result["error"])

    async def test_group_admin_schedule_uses_existing_state(self):
        event = Event(admin=True)
        result = json.loads(await self.plugin.tool_schedule(event, "set_time", "20:30"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.plugin.store.settings(event.unified_msg_origin)["time"], "20:30")
        await self.plugin.tool_schedule(event, "pause")
        self.assertFalse(self.plugin.store.settings(event.unified_msg_origin)["enabled"])
        await self.plugin.tool_schedule(event, "resume")
        self.assertTrue(self.plugin.store.settings(event.unified_msg_origin)["enabled"])

    async def test_private_configuration_and_subscriptions_stay_isolated(self):
        event = Event(group="")
        event.unified_msg_origin = "qq:FriendMessage:alice"
        self.assertTrue(
            json.loads(await self.plugin.tool_schedule(event, "set_time", "22:00"))["ok"]
        )
        await self.plugin.tool_subscriptions(event, "subscribe", "jump:53387")
        self.assertEqual(len(self.plugin.store.subscriptions(event.unified_msg_origin)), 1)
        self.assertEqual(self.plugin.store.subscriptions(Event.unified_msg_origin), [])

    async def test_invalid_tool_arguments_never_mutate_state(self):
        calls = [
            self.plugin.tool_search_games(Event(), "x", "unknown"),
            self.plugin.tool_search_games(Event(), ""),
            self.plugin.tool_today_deals(Event(), limit=True),
            self.plugin.tool_today_deals(Event(), limit=1.5),
            self.plugin.tool_today_deals(Event(), limit=21),
            self.plugin.tool_game_price(Event(), "53387"),
            self.plugin.tool_subscriptions(Event(), "subscribe", ""),
            self.plugin.tool_subscriptions(Event(), "delete_all"),
            self.plugin.tool_subscriptions(Event(), "list", "jump:53387"),
            self.plugin.tool_schedule(Event(admin=True), "set_time", "25:00"),
            self.plugin.tool_schedule(Event(admin=True), "pause", "20:00"),
        ]
        for call in calls:
            result = json.loads(await call)
            self.assertFalse(result["ok"])
        self.assertFalse(self.plugin.store.subscriptions(Event.unified_msg_origin))
        self.plugin.jump.game.assert_not_awaited()

    async def test_source_failures_are_not_success_or_no_discount(self):
        self.plugin.jump.search.side_effect = SourceError("Jump 暂时不可用")
        result = json.loads(await self.plugin.tool_search_games(Event(), "test"))
        self.assertFalse(result["ok"])
        self.assertIn("Jump", result["error"])
        self.plugin.source.search.side_effect = RuntimeError("secret internal detail")
        result = json.loads(await self.plugin.tool_search_games(Event(), "test", "steam"))
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", result["error"])

    async def test_not_ready_is_explicit_error(self):
        store = self.plugin.store
        self.plugin.store = None
        try:
            result = json.loads(await self.plugin.tool_game_price(Event(), "jump:53387"))
            self.assertFalse(result["ok"])
        finally:
            self.plugin.store = store
