import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from deals import Game, SourceError, SteamSource, Store, Watcher, app_id, clock_time

SALE = Game('123', '测试游戏', 'CNY', 10000, 5000, 50)
REGULAR = Game('123', '测试游戏', 'CNY', 10000, 10000, 0)
GROUP = 'qq:GroupMessage:100'
PRIVATE = 'qq:FriendMessage:200'


def at(day=14, hour=9, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai'))


class ValidationTests(unittest.TestCase):
    def test_time(self):
        self.assertEqual(clock_time('23:59'), '23:59')
        for bad in ['24:00', '9:00', '12:60', '-1:00', '00:00\n']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clock_time(bad)

    def test_id(self):
        for text in ['123', 'https://store.steampowered.com/app/123/Test/?a=1']:
            self.assertEqual(app_id(text), '123')
        for bad in ['0', '-1', 'https://evil.com/app/123', '123 garbage']:
            with self.assertRaises(ValueError):
                app_id(bad)

    def test_free_and_missing_prices_are_not_discounts(self):
        self.assertTrue(SALE.discounted)
        self.assertTrue(Game('1', '限免', 'CNY', 1000, 0, 100).discounted)
        self.assertFalse(Game('1', '永久免费', 'CNY', 0, 0, 100, True).discounted)
        self.assertFalse(Game('1', '未发售', percent=50).discounted)
        self.assertFalse(REGULAR.discounted)
        self.assertIn('100.00 → 50.00', SALE.text())


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.source = AsyncMock()
        self.source.country = 'cn'
        self.source.game.return_value = SALE
        self.send = AsyncMock(return_value=True)
        self.watcher = Watcher(self.store, self.source, self.send)
        self.store.subscribe(GROUP, SALE, 'alice', 50)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_not_before_time_then_one_per_day(self):
        await self.watcher.tick(at(hour=8, minute=59))
        self.send.assert_not_awaited()
        await self.watcher.tick(at())
        await self.watcher.tick(at(hour=15))
        self.assertEqual(self.send.await_count, 1)
        await self.watcher.tick(at(day=15))
        self.assertEqual(self.send.await_count, 2)

    async def test_sessions_are_isolated(self):
        self.store.subscribe(PRIVATE, SALE, 'bob', 50)
        self.store.configure(PRIVATE, at='11:30')
        await self.watcher.tick(at())
        self.assertEqual(self.send.await_args.args[0], GROUP)
        await self.watcher.tick(at(hour=11, minute=30))
        self.assertEqual(self.send.await_args.args[0], PRIVATE)

    async def test_pause_and_resume(self):
        self.store.configure(GROUP, enabled=False)
        await self.watcher.tick(at())
        self.send.assert_not_awaited()
        self.store.configure(GROUP, enabled=True)
        await self.watcher.tick(at(hour=12))
        self.send.assert_awaited_once()

    async def test_no_discount_silent_and_checked(self):
        self.source.game.return_value = REGULAR
        await self.watcher.tick(at())
        self.send.assert_not_awaited()
        self.assertEqual(self.store.due(at(hour=12)), [])
        self.assertIsNone(self.store.subscriptions(GROUP)[0]['last_notified'])

    async def test_source_failure_retries_without_marking(self):
        self.source.game.side_effect = SourceError('offline')
        with self.assertLogs('deals', level='ERROR'):
            await self.watcher.tick(at())
        self.assertEqual(len(self.store.due(at())), 1)
        await self.watcher.tick(at())
        self.assertEqual(self.source.game.await_count, 1)  # backoff
        self.source.game.side_effect = None
        self.watcher.retry_after.clear()
        await self.watcher.tick(at())
        self.send.assert_awaited_once()

    async def test_send_false_or_exception_keeps_pending(self):
        for result in [False, RuntimeError('send error')]:
            self.send.side_effect = result if isinstance(result, Exception) else None
            self.send.return_value = result
            self.watcher.retry_after.clear()
            with self.assertLogs('deals', level='ERROR'):
                await self.watcher.tick(at())
            self.assertEqual(len(self.store.due(at())), 1)
        self.send.side_effect = None
        self.send.return_value = True
        self.watcher.retry_after.clear()
        await self.watcher.tick(at())
        self.assertEqual(self.store.due(at()), [])

    async def test_failed_game_does_not_block_other_session(self):
        self.store.subscribe(PRIVATE, Game('456', 'other'), 'bob', 50)
        async def lookup(id_):
            if id_ == '123':
                raise SourceError('offline')
            return Game('456', 'other', 'CNY', 2000, 1000, 50)
        self.source.game.side_effect = lookup
        with self.assertLogs('deals', level='ERROR'):
            await self.watcher.tick(at())
        self.assertEqual(self.send.await_args.args[0], PRIVATE)

    async def test_restart_preserves_subscription_time_and_delivery(self):
        await self.watcher.tick(at())
        self.store.configure(GROUP, at='10:00')
        self.store.close()
        self.store = Store(self.path)
        self.watcher = Watcher(self.store, self.source, self.send)
        self.assertEqual(self.store.settings(GROUP)['time'], '10:00')
        await self.watcher.tick(at(hour=12))
        self.send.assert_awaited_once()
        await self.watcher.tick(at(day=15, hour=12))  # missed time catches up
        self.assertEqual(self.send.await_count, 2)

    async def test_subscription_ownership_limits_and_no_owner_overwrite(self):
        self.assertFalse(self.store.subscribe(GROUP, SALE, 'bob', 50))
        self.assertEqual(self.store.subscriptions(GROUP)[0]['owner'], 'alice')
        with self.assertRaises(ValueError):
            self.store.unsubscribe(GROUP, '123', 'bob', False)
        with self.assertRaises(ValueError):
            self.store.subscribe(GROUP, Game('456', 'x'), 'alice', 1)
        self.assertFalse(self.store.unsubscribe(PRIVATE, '123', 'alice', True))
        self.assertTrue(self.store.unsubscribe(GROUP, '123', 'admin', True))

    async def test_concurrent_ticks_do_not_duplicate(self):
        await asyncio.gather(self.watcher.tick(at()), self.watcher.tick(at()))
        self.send.assert_awaited_once()

    async def test_configured_timezone_used(self):
        utc = datetime(2026, 11, 14, 1, 0, tzinfo=ZoneInfo('UTC'))
        await self.watcher.tick(utc)
        self.send.assert_awaited_once()


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_cache_and_concurrent_coalescing(self):
        source = SteamSource()
        source.request = AsyncMock(return_value={'123': {'success': True, 'data': {
            'name': '测试', 'price_overview': {'currency': 'CNY', 'initial': 100, 'final': 50, 'discount_percent': 50}}}})
        results = await asyncio.gather(source.game('123'), source.game('123'))
        self.assertTrue(results[0].discounted)
        source.request.assert_awaited_once()

    async def test_unavailable_is_not_cached(self):
        source = SteamSource()
        source.request = AsyncMock(return_value={'123': {'success': False}})
        for _ in range(2):
            with self.assertRaises(SourceError):
                await source.game('123')
        self.assertEqual(source.request.await_count, 2)

    async def test_featured_filters_non_apps_and_regular_prices(self):
        source = SteamSource()
        source.request = AsyncMock(return_value={'specials': {'items': [
            {'id': 1, 'type': 0, 'name': 'sale', 'original_price': 100, 'final_price': 50, 'discount_percent': 50},
            {'id': 2, 'type': 0, 'name': 'regular', 'original_price': 100, 'final_price': 100, 'discount_percent': 0},
            {'id': 3, 'type': 1, 'name': 'package', 'original_price': 100, 'final_price': 50, 'discount_percent': 50}]}})
        self.assertEqual([x.id for x in await source.today()], ['1'])

    async def test_missing_featured_data_is_error(self):
        source = SteamSource()
        source.request = AsyncMock(return_value={})
        with self.assertRaises(SourceError):
            await source.today()


if __name__ == '__main__':
    unittest.main()
