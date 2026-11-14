import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_commands
from astrbot_plugin_game_deals.comparison import Comparison
from astrbot_plugin_game_deals.deals import Game, SourceError, Store
from astrbot_plugin_game_deals.digest import DigestService
from astrbot_plugin_game_deals.heybox import HeyboxSource
from astrbot_plugin_game_deals.jump import JumpSource
from test_commands import Event


def now(day=14, hour=9):
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo("Asia/Shanghai"))


def sale(**kwargs):
    args = dict(
        id="jump:294",
        name="马力欧卡丁车8 豪华版",
        currency="CNY≈",
        original=40000,
        final=20000,
        percent=50,
        source="Jump",
        platform="Switch",
        region="日本",
    )
    args.update(kwargs)
    return Game(**args)


class SourceExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def test_heybox_checksum_matches_upstream_fixed_vector(self):
        from astrbot_plugin_game_deals.heybox_sign import sign

        with (
            patch("astrbot_plugin_game_deals.heybox_sign.time.time", return_value=1700000000),
            patch(
                "astrbot_plugin_game_deals.heybox_sign.secrets.token_hex",
                return_value="ABC123DEF456",
            ),
        ):
            params = sign("game/get_game_list_v3/", {"q": "test"})
        self.assertEqual(params["hkey"], "G12HX00")
        self.assertEqual(params["_time"], 1700000000)
        self.assertEqual(params["nonce"], "ABC123DEF456")

    async def test_jump_supports_switch_without_mislabeling(self):
        row = dict(
            oldGameId=294,
            name="马力欧卡丁车8 豪华版",
            platform=1,
            subPlatform=10,
            originPrice=400,
            price=200,
            cutOff=50,
        )
        with self.assertRaises(SourceError):
            JumpSource().parse(row)
        self.assertEqual(JumpSource(scope="switch").parse(row).platform, "Switch")
        self.assertEqual(JumpSource(scope="all").parse(row).id, "jump:294")

    async def test_heybox_catalogue_pagination_cache_and_name_variant(self):
        source = HeyboxSource(max_pages=2)
        source.request = AsyncMock(
            side_effect=[
                {"games": [{"appid": i, "name": "Other"} for i in range(30)]},
                {"games": [{"appid": 900000090, "name": "马里奥赛车8 豪华版"}]},
            ]
        )
        results = await source.search("马力欧卡丁车8")
        self.assertEqual(results[0][0], "heybox:900000090")
        await source.search("赛车8")
        self.assertEqual(source.request.await_count, 2)

    async def test_heybox_verified_identity_and_units(self):
        source = HeyboxSource()
        source.request = AsyncMock(
            side_effect=[
                {
                    "base_infos": [
                        {
                            "steam_appid": 900000090,
                            "name": "马里奥赛车8 豪华版",
                            "price": {
                                "current": "274.8",
                                "initial": "300",
                                "discount": 8,
                                "region_name": "日本",
                            },
                        }
                    ]
                },
                {"appid": 900000090, "platforms_list": [{"platf": "switch"}]},
            ]
        )
        game = await source.game("heybox:900000090")
        self.assertEqual(game.final, 27480)
        self.assertIn("未细分", game.platform)
        self.assertTrue(game.discounted)
        await source.game("heybox:900000090")
        self.assertEqual(source.request.await_count, 2)

    async def test_heybox_rejects_wrong_platform_identity_or_unavailable_price(self):
        for details in [
            {"appid": 99, "platforms_list": [{"platf": "switch"}]},
            {"appid": 1, "platforms_list": [{"platf": "pc"}]},
        ]:
            source = HeyboxSource()
            source.request = AsyncMock(
                side_effect=[
                    {"base_infos": [{"steam_appid": 1, "name": "test", "price": {"current": 5}}]},
                    details,
                ]
            )
            with self.assertRaises(SourceError):
                await source.game("heybox:1")

    async def test_heybox_today_filters_actual_discounts(self):
        source = HeyboxSource()
        source.catalogue_games = AsyncMock(
            return_value=[
                {
                    "appid": 1,
                    "name": "sale",
                    "platform_infos": [
                        {
                            "key": "switch",
                            "price": {"current": "5", "initial": "10", "discount": 50},
                        }
                    ],
                },
                {
                    "appid": 2,
                    "name": "regular",
                    "platform_infos": [
                        {
                            "key": "switch",
                            "price": {"current": "10", "initial": "10", "discount": 0},
                        }
                    ],
                },
            ]
        )
        self.assertEqual([g.id for g in await source.today()], ["heybox:1"])


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_mario8_alias_is_same_edition_and_compares_cny(self):
        jump, heybox = AsyncMock(), AsyncMock()
        jump.game.return_value = sale()
        heybox.game.return_value = sale(
            id="heybox:900000090",
            name="马里奥赛车8 豪华版",
            source="小黑盒",
            platform="Switch（来源未细分代际）",
            final=19000,
            region="中国香港",
        )
        result = await Comparison(jump, heybox).compare("马车8")
        jump.game.assert_awaited_once_with("jump:294")
        self.assertIn("小黑盒，约人民币 190.00", result)
        self.assertIn("中国香港", result)
        self.assertNotIn("全网最低", result)

    async def test_wrong_edition_is_not_compared(self):
        jump, heybox = AsyncMock(), AsyncMock()
        jump.game.return_value = sale(name="马力欧卡丁车：世界", platform="Switch 2")
        heybox.game.return_value = sale(
            id="heybox:900000090", name="马里奥赛车8 豪华版", source="小黑盒"
        )
        result = await Comparison(jump, heybox).compare("马车8")
        self.assertIn("不匹配", result)
        self.assertNotIn("本次已取得报价中的较低参考值", result)

    async def test_source_failure_is_reported_with_other_quote(self):
        jump, heybox = AsyncMock(), AsyncMock()
        jump.game.return_value = sale()
        heybox.game.side_effect = SourceError("offline")
        result = await Comparison(jump, heybox).compare("马车8")
        self.assertIn("Jump", result)
        self.assertIn("小黑盒：价格查询失败", result)
        self.assertIn("暂不计算", result)

    async def test_ambiguous_search_returns_candidates_without_guessing(self):
        jump, heybox = AsyncMock(), AsyncMock()
        jump.search.return_value = [("jump:1", "Game [Switch]"), ("jump:2", "Game [Switch 2]")]
        heybox.search.return_value = []
        result = await Comparison(jump, heybox).compare("Game")
        jump.game.assert_not_awaited()
        self.assertIn("候选版本", result)


class DigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db"
        self.store = Store(self.path)
        self.source = AsyncMock()
        self.source.today.return_value = [sale()]
        self.send = AsyncMock(return_value=True)
        self.service = DigestService(
            self.store, {"jump": self.source, "heybox": self.source}, self.send
        )
        self.event = Event(admin=True)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_permission_and_independent_game_subscriptions(self):
        with self.assertRaises(ValueError):
            self.service.manage(Event(), "subscribe", at="09:00")
        self.service.manage(self.event, "subscribe", at="09:00")
        self.assertEqual(self.store.subscriptions(self.event.unified_msg_origin), [])
        self.assertIn("09:00", self.service.manage(self.event, "list"))

    async def test_one_daily_digest_time_pause_resume_and_next_day(self):
        self.service.manage(self.event, "subscribe", at="10:00")
        await self.service.tick(now())
        self.send.assert_not_awaited()
        self.service.manage(self.event, "pause")
        await self.service.tick(now(hour=11))
        self.send.assert_not_awaited()
        self.service.manage(self.event, "resume")
        await self.service.tick(now(hour=11))
        await self.service.tick(now(hour=12))
        self.send.assert_awaited_once()
        await self.service.tick(now(day=15, hour=11))
        self.assertEqual(self.send.await_count, 2)

    async def test_partial_send_restart_retries_only_unsent_parts(self):
        self.source.today.return_value = [sale(name="x" * 5000)]
        self.service.manage(self.event, "subscribe", at="09:00")
        self.send.side_effect = [True, False]
        with self.assertLogs("astrbot", level="ERROR"):
            await self.service.tick(now())
        first = self.send.await_args_list[0].args[1]
        self.store.close()
        self.store = Store(self.path)
        self.send = AsyncMock(return_value=True)
        self.service = DigestService(self.store, {"jump": self.source}, self.send)
        await self.service.tick(now(hour=12))
        self.assertTrue(self.send.await_count >= 1)
        self.assertTrue(all(c.args[1] != first for c in self.send.await_args_list))
        self.assertEqual(self.source.today.await_count, 1)
        sent = self.send.await_count
        await self.service.tick(now(hour=13))
        self.assertEqual(self.send.await_count, sent)

    async def test_source_failure_does_not_mark_or_block_other_source(self):
        failed = AsyncMock()
        failed.today.side_effect = SourceError("offline")
        self.service.sources["heybox"] = failed
        self.service.manage(self.event, "subscribe", source="all", at="09:00")
        with self.assertLogs("astrbot", level="ERROR"):
            await self.service.tick(now())
        self.send.assert_awaited_once()
        self.assertTrue(self.service.retry_after)
        await self.service.tick(now())
        self.assertEqual(failed.today.await_count, 1)

    async def test_restart_resubscribe_same_day_does_not_repeat(self):
        self.service.manage(self.event, "subscribe", at="09:00")
        await self.service.tick(now())
        self.service.manage(self.event, "unsubscribe")
        self.service.manage(self.event, "subscribe", at="09:00")
        await self.service.tick(now(hour=12))
        self.send.assert_awaited_once()

    async def test_two_chats_reuse_listing_but_receive_separate_messages(self):
        other = Event(admin=True)
        other.unified_msg_origin = "qq:GroupMessage:other"
        self.service.manage(self.event, "subscribe", at="09:00")
        self.service.manage(other, "subscribe", at="09:00")
        await self.service.tick(now())
        self.source.today.assert_awaited_once()
        self.assertEqual(
            {call.args[0] for call in self.send.await_args_list},
            {self.event.unified_msg_origin, other.unified_msg_origin},
        )

    async def test_private_scope_empty_list_and_concurrent_ticks(self):
        event = Event(group="")
        event.unified_msg_origin = "qq:FriendMessage:alice"
        self.source.today.return_value = []
        self.service.manage(event, "subscribe", at="09:00")
        await asyncio.gather(self.service.tick(now()), self.service.tick(now()))
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args[0], event.unified_msg_origin)
        self.assertIn("扫描范围内暂无", self.send.await_args.args[1])


class NewCommandToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_commands.CommandTests.asyncSetUp(self)
        self.plugin.comparison = AsyncMock()
        self.plugin.comparison.compare.return_value = "同款比价结果"
        self.plugin.digests = DigestService(
            self.plugin.store, {"jump": self.plugin.source}, AsyncMock()
        )

    async def asyncTearDown(self):
        await test_commands.CommandTests.asyncTearDown(self)

    async def test_compare_command_and_tool(self):
        import json

        result = [x async for x in self.plugin.compare_command(Event("游戏比价 马车8"))]
        self.assertEqual(result, ["同款比价结果"])
        self.assertTrue(json.loads(await self.plugin.tool_compare(Event(), "马车8"))["ok"])
        self.plugin.comparison.compare.assert_awaited_with("马车8")
        result = [x async for x in self.plugin.price_command(Event("游戏查价 马车8"))]
        self.assertEqual(result, ["同款比价结果"])

    async def test_digest_command_and_tool_share_permission_and_state(self):
        import json

        rejected = json.loads(
            await self.plugin.tool_daily_digest(Event(), "subscribe", time="20:30")
        )
        self.assertFalse(rejected["ok"])
        result = [
            x
            async for x in self.plugin.subscribe_digest_command(
                Event("订阅折扣日报 20:30 jump 3", admin=True)
            )
        ]
        self.assertIn("20:30", result[0])
        listed = json.loads(await self.plugin.tool_daily_digest(Event(), "list"))
        self.assertIn("3款", listed["result"])

    async def test_separate_price_and_subscription_commands(self):
        result = [x async for x in self.plugin.price_command(Event("游戏查价 steam:123"))]
        self.assertIn("Hades", result[0])
        result = [x async for x in self.plugin.subscribe_command(Event("订阅游戏 steam:123"))]
        self.assertIn("已订阅", result[0])
