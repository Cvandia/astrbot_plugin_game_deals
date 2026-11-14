import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_game_deals.deals import Game,Store
from astrbot_plugin_game_deals.digest import DigestService
from astrbot_plugin_game_deals.digest_image import DigestImageRenderer
from test_commands import Event
import test_commands


def at(hour=9):return datetime(2026,11,14,hour,tzinfo=ZoneInfo('Asia/Shanghai'))
def game(name='马力欧卡丁车8 豪华版'):
    return Game('jump:294',name,'CNY≈',40000,20000,50,source='Jump',platform='Switch',region='日本',note='跨区人民币参考价；全区中文。')


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'db'
        self.store=Store(self.path);self.source=AsyncMock();self.source.today.return_value=[game()]
        self.send=AsyncMock(return_value=True);self.image=AsyncMock(return_value=True)
        self.event=Event(admin=True)
        self.service=DigestService(self.store,{'jump':self.source},self.send,send_image=self.image,default_mode='image')

    async def asyncTearDown(self):self.store.close();self.temp.cleanup()

    async def test_global_image_mode_and_same_day_dedup(self):
        self.service.manage(self.event,'subscribe',at='09:00')
        await self.service.tick(at());await self.service.tick(at(12))
        self.image.assert_awaited_once();self.send.assert_not_awaited()
        snapshot=self.image.await_args.args[1]
        self.assertEqual(snapshot['games'][0]['final'],20000)
        self.assertEqual(snapshot['games'][0]['region'],'日本')
        self.assertEqual(snapshot['day'],'2026-11-14')

    async def test_explicit_text_overrides_global_image(self):
        self.service.manage(self.event,'subscribe',at='09:00',mode='text')
        await self.service.tick(at())
        self.send.assert_awaited_once();self.image.assert_not_awaited()

    async def test_mode_permissions_and_listing(self):
        with self.assertRaises(ValueError):self.service.manage(self.event,'set_mode',mode='image')
        self.service.manage(self.event,'subscribe',mode='text')
        with self.assertRaises(ValueError):self.service.manage(Event(),'set_mode',mode='image')
        self.service.manage(self.event,'set_mode',mode='图片')
        self.assertIn('image',self.service.manage(self.event,'list'))
        with self.assertRaises(ValueError):self.service.manage(self.event,'set_mode',mode='invalid')

    async def test_paginated_image_resume_uses_saved_snapshot(self):
        self.service.image_rows=2
        self.source.today.return_value=[game(str(i)) for i in range(5)]
        self.service.image_fallback=False
        self.service.manage(self.event,'subscribe',at='09:00')
        self.image.side_effect=[True,False]
        with self.assertLogs('astrbot_plugin_game_deals.digest',level='ERROR'):await self.service.tick(at())
        self.assertEqual(self.image.await_args_list[0].args[1]['pages'],3)
        self.store.close();self.store=Store(self.path)
        resumed=AsyncMock(return_value=True)
        service=DigestService(self.store,{'jump':self.source},self.send,send_image=resumed,default_mode='image')
        await service.tick(at(12))
        self.assertEqual([c.args[1]['page'] for c in resumed.await_args_list],[2,3])
        self.source.today.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_fallback_text_progress_survives_restart(self):
        self.source.today.return_value=[game('长游戏名'*1500)]
        self.service.manage(self.event,'subscribe',at='09:00')
        self.image.side_effect=RuntimeError('render error')
        self.send.side_effect=[True,False]
        with self.assertLogs('astrbot_plugin_game_deals.digest',level='WARNING'):await self.service.tick(at())
        first=self.send.await_args_list[0].args[1]
        row=self.store.db.execute('SELECT * FROM digest_parts').fetchone()
        self.assertEqual((row['kind'],row['progress'],row['sent']),('fallback',1,0))
        self.store.close();self.store=Store(self.path)
        text=AsyncMock(return_value=True);images=AsyncMock(return_value=True)
        service=DigestService(self.store,{'jump':self.source},text,send_image=images,default_mode='image')
        await service.tick(at(12))
        images.assert_not_awaited()
        self.assertTrue(all(c.args[1]!=first for c in text.await_args_list))
        self.assertEqual(self.store.db.execute('SELECT sent FROM digest_parts').fetchone()[0],1)
        self.source.today.assert_awaited_once()

    async def test_disable_fallback_keeps_image_pending(self):
        self.service.image_fallback=False
        self.service.manage(self.event,'subscribe',at='09:00')
        self.image.return_value=False
        with self.assertLogs('astrbot_plugin_game_deals.digest',level='ERROR'):await self.service.tick(at())
        self.send.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT kind,sent FROM digest_parts').fetchone()[:],('image',0))

    async def test_style_changes_do_not_rewrite_pending_snapshot(self):
        self.service.image_fallback=False;self.image.return_value=False
        self.service.manage(self.event,'subscribe',at='09:00')
        with self.assertLogs('astrbot_plugin_game_deals.digest',level='ERROR'):await self.service.tick(at())
        self.service.manage(self.event,'set_mode',mode='text')
        self.service.retry_after.clear();self.image.return_value=True
        await self.service.tick(at(12))
        self.send.assert_not_awaited()
        self.assertEqual(self.image.await_count,2)


class RendererTests(unittest.TestCase):
    def test_render_chinese_bounds_cache_and_no_remote_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            renderer=DigestImageRenderer(Path(folder))
            snapshot={'day':'2026-11-14','source':'jump','games':[asdict(game('超长标题'*100)),asdict(game())],
                'total':2,'page':1,'pages':1,'generated':'2026-11-14 09:00','timezone':'Asia/Shanghai'}
            path=renderer.render(snapshot)
            self.assertEqual(path,renderer.render(snapshot))
            with Image.open(path) as image:
                self.assertEqual(image.width,1200)
                self.assertGreaterEqual(image.height,1050)
                self.assertLess(image.height,2500)
                self.assertNotEqual(image.getpixel((60,80)),image.getpixel((0,0)))
            for line in renderer.wrap('超长标题'*100,35,1000,3):
                self.assertLessEqual(renderer.font(35).getlength(line),1000)
            self.assertEqual(len(list(Path(folder).glob('*.png'))),1)

    def test_empty_result_and_missing_font(self):
        with tempfile.TemporaryDirectory() as folder:
            path=DigestImageRenderer(Path(folder)).render({'day':'2026-11-14','source':'steam','games':[]})
            with Image.open(path) as image:self.assertEqual(image.width,1200)
            with self.assertRaises(ValueError):DigestImageRenderer(Path(folder),str(Path(folder)/'missing.ttf'))

    def test_old_schema_additive_migration_preserves_text_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'db')
            store.db.executescript("""
                CREATE TABLE digests(origin TEXT,source TEXT,time TEXT,count INTEGER,enabled INTEGER DEFAULT 1,last_sent TEXT,PRIMARY KEY(origin,source));
                CREATE TABLE digest_parts(origin TEXT,source TEXT,day TEXT,part INTEGER,body TEXT,sent INTEGER DEFAULT 0,PRIMARY KEY(origin,source,day,part));
                INSERT INTO digests(origin,source,time,count) VALUES('test','jump','09:00',5);
                INSERT INTO digest_parts VALUES('test','jump','2026-11-14',0,'old text',1);
            """)
            try:
                DigestService(store,{'jump':AsyncMock()},AsyncMock(),default_mode='image')
                row=store.db.execute('SELECT * FROM digest_parts').fetchone()
                self.assertEqual((row['kind'],row['sent'],row['body']),('text',1,'old text'))
                self.assertEqual(store.db.execute('SELECT mode FROM digests').fetchone()[0],'default')
            finally:store.close()


class ImageCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_commands.CommandTests.asyncSetUp(self)
        self.plugin.digests=DigestService(self.plugin.store,{'jump':self.plugin.source},AsyncMock())

    async def asyncTearDown(self):await test_commands.CommandTests.asyncTearDown(self)

    async def test_subscription_command_and_llm_mode(self):
        rows=[r async for r in self.plugin.subscribe_digest_command(Event('订阅折扣日报 20:30 jump 5 image',admin=True))]
        self.assertIn('image',rows[0])
        result=json.loads(await self.plugin.tool_daily_digest(Event(),'list'))
        self.assertIn('image',result['result'])
        result=json.loads(await self.plugin.tool_daily_digest(Event(admin=True),'set_mode',mode='text'))
        self.assertTrue(result['ok'])
        self.assertIn('text',self.plugin.digests.manage(Event(),'list'))

    async def test_independent_style_command_permission(self):
        self.plugin.digests.manage(Event(admin=True),'subscribe')
        rows=[r async for r in self.plugin.digest_mode_command(Event('折扣日报样式 图片'))]
        self.assertIn('管理员',rows[0])
        rows=[r async for r in self.plugin.digest_mode_command(Event('折扣日报样式 图片',admin=True))]
        self.assertIn('image',rows[0])
