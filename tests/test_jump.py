import asyncio
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from astrbot_test_support import install_logger_stub

install_logger_stub()

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_game_deals.deals import SourceError, Store, Watcher
from astrbot_plugin_game_deals.jump import JumpSource, SourceRouter, jump_id, money
from astrbot_plugin_game_deals.scheduling import start_scheduler
from test_commands import Plugin


def item(**changes):
    x = dict(
        oldGameId=53387,
        name="马力欧卡丁车：世界",
        platform=1,
        subPlatform=11,
        originPrice=375.09,
        price=375.09,
        priceCountry="日本",
        cutOff=0,
        chinese="全区中文",
    )
    x.update(changes)
    return x


class JumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_ids_and_money_units(self):
        self.assertEqual(jump_id("53387"), "jump:53387")
        self.assertEqual(jump_id("JUMP:53387"), "jump:53387")
        with self.assertRaises(ValueError):
            jump_id("steam:53387")
        self.assertEqual(money("39.97", yuan=True), 3997)
        self.assertEqual(money(3996), 3996)
        self.assertEqual(money("1.005", yuan=True), 101)
        for invalid in [-1, None, "nan", "inf", "bad", True]:
            self.assertIsNone(money(invalid))

    async def test_strict_switch2_filter(self):
        source = JumpSource()
        self.assertTrue(source.is_switch2(item()))
        for changes in [
            dict(subPlatform=10),
            dict(subPlatform=None),
            dict(platform=4),
            dict(isDlc=1),
            dict(isUpgradePass=1),
        ]:
            self.assertFalse(source.is_switch2(item(**changes)))
            with self.assertRaises(SourceError):
                source.parse(item(**changes))

    async def test_promotion_badge_does_not_hide_valid_game_or_count_as_discount(self):
        game = JumpSource().parse(item(jumpCutOff=True, plusPrice=1))
        self.assertFalse(game.discounted)
        self.assertEqual(game.id, "jump:53387")

    async def test_detail_and_list_use_different_units(self):
        source = JumpSource()
        listed = source.parse(item(price=39.97, originPrice=270.41, cutOff=85))
        detailed = source.parse(item(price=3997, originPrice=27041, cutoff=85), detail=True)
        self.assertEqual((listed.original, listed.final), (detailed.original, detailed.final))
        self.assertTrue(detailed.discounted)
        self.assertIn("Switch 2", listed.text())
        self.assertIn("日本", listed.text())
        self.assertIn("人民币折算", listed.text())
        self.assertNotIn("steampowered", listed.text())

    async def test_search_paginates_before_local_platform_filter(self):
        source = JumpSource()
        source.request = AsyncMock(
            side_effect=[
                {"gameList": [item(oldGameId=n, subPlatform=10) for n in range(1, 31)]},
                {"gameList": [item()]},
            ]
        )
        self.assertEqual(
            await source.search("马力欧"), [("jump:53387", "马力欧卡丁车：世界 [Switch 2]")]
        )
        self.assertEqual(source.request.await_args.kwargs["offset"], 30)
        self.assertEqual(source.request.await_args.kwargs["type"], 3)
        self.assertEqual(source.request.await_args.kwargs["platForm"], 1)

    async def test_discount_pagination_uses_verified_ten_item_page(self):
        source = JumpSource()
        source.request = AsyncMock(
            side_effect=[
                [item(oldGameId=n, subPlatform=10) for n in range(1, 11)],
                [item(price=50, originPrice=100, cutOff=50)],
            ]
        )
        games = await source.today(5)
        self.assertEqual([g.id for g in games], ["jump:53387"])
        self.assertEqual(source.request.await_args.kwargs["offset"], 10)

    async def test_duplicate_page_is_bounded(self):
        source = JumpSource(max_pages=20)
        source.request = AsyncMock(
            return_value=[item(oldGameId=n, subPlatform=10) for n in range(1, 11)]
        )
        self.assertEqual(await source.today(), [])
        self.assertEqual(source.request.await_count, 2)

    async def test_detail_identity_cache_and_platform_verification(self):
        source = JumpSource()
        source.request = AsyncMock(
            return_value={"jumpGame": item(price=20000, originPrice=40000, cutoff=50)}
        )
        first, second = await asyncio.gather(source.game("jump:53387"), source.game("jump:53387"))
        self.assertIs(first, second)
        source.request.assert_awaited_once()
        self.assertEqual(source.request.await_args.kwargs["method"], "POST")
        source.cache.clear()
        source.request.return_value = {"jumpGame": item(oldGameId=123)}
        with self.assertRaises(SourceError):
            await source.game("jump:53387")
        self.assertFalse(source.cache)

    async def test_expired_sale_not_notified_or_cached(self):
        source = JumpSource()
        source.request = AsyncMock(
            return_value={
                "jumpGame": item(
                    price=50,
                    originPrice=100,
                    cutoff=50,
                    discountEndDate=int(time.time() - 60) * 1000,
                )
            }
        )
        with self.assertRaises(SourceError):
            await source.game("jump:53387")
        self.assertFalse(source.cache)

    async def test_missing_shape_is_failure_not_no_discount(self):
        source = JumpSource()
        source.request = AsyncMock(return_value={})
        for operation in [source.search("test"), source.today(), source.game("jump:53387")]:
            with self.assertRaises(SourceError):
                await operation

    async def test_router_legacy_and_jump_ids_do_not_collide(self):
        steam, jump = AsyncMock(), AsyncMock()
        router = SourceRouter(steam, jump)
        await router.game("53387")
        await router.game("jump:53387")
        steam.game.assert_awaited_once_with("53387")
        jump.game.assert_awaited_once_with("jump:53387")

    async def test_existing_db_coexists_and_notifications_use_jump_label(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "db")
            jump = JumpSource()
            jump.game = AsyncMock(
                return_value=jump.parse(item(price=50, originPrice=100, cutOff=50))
            )
            steam = AsyncMock()
            steam.game.return_value = jump.parse(item(price=50, originPrice=100, cutOff=50))
            from astrbot_plugin_game_deals.deals import Game

            store.subscribe("qq:GroupMessage:1", Game("53387", "legacy"), "alice", 50)
            store.subscribe("qq:GroupMessage:1", await jump.game("jump:53387"), "alice", 50)
            send = AsyncMock(return_value=True)
            try:
                watcher = Watcher(store, SourceRouter(steam, jump), send)
                await watcher.tick(datetime(2026, 11, 14, 9, tzinfo=ZoneInfo("Asia/Shanghai")))
                self.assertEqual(len(store.subscriptions("qq:GroupMessage:1")), 2)
                self.assertIn("数据：Jump", send.await_args.args[1])
                await watcher.tick(datetime(2026, 11, 14, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
                self.assertEqual(send.await_count, 2)
            finally:
                store.close()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_apscheduler_job_configuration_and_immediate_recovery(self):
        fired = asyncio.Event()

        class Target:
            async def tick(self):
                fired.set()

        scheduler = start_scheduler(Target(), ZoneInfo("Asia/Shanghai"))
        try:
            job = scheduler.get_job("game-deals-check")
            self.assertEqual(job.max_instances, 1)
            self.assertTrue(job.coalesce)
            self.assertEqual(job.trigger.interval.total_seconds(), 30)
            self.assertEqual(str(scheduler.timezone), "Asia/Shanghai")
            await asyncio.wait_for(fired.wait(), timeout=2)
        finally:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)

    async def test_plugin_unload_cancels_active_job_then_closes_resources(self):
        plugin = Plugin(None, {})
        plugin.source, plugin.jump = AsyncMock(), AsyncMock()
        plugin.store = AsyncMock()  # close is deliberately synchronous below
        plugin.store.close = lambda: None
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        plugin.watcher = type("Target", (), {})()
        plugin.watcher.tick = pending
        plugin.scheduler = start_scheduler(plugin, ZoneInfo("Asia/Shanghai"))
        await asyncio.wait_for(started.wait(), timeout=2)
        with self.assertLogs("apscheduler.executors.default", level="ERROR"):
            await plugin.terminate()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(plugin.active_checks)
        self.assertFalse(plugin.scheduler.running)
        plugin.source.close.assert_awaited_once()
        plugin.jump.close.assert_awaited_once()
