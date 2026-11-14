"""Daily current-discount digests, with durable per-part delivery checkpoints."""

import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .deals import clock_time
from .digest_image import mode_name


class DigestService:
    def __init__(
        self,
        store,
        sources,
        send,
        timezone="Asia/Shanghai",
        *,
        send_image=None,
        default_mode="text",
        image_fallback=True,
        image_rows=8,
    ):
        self.store, self.sources, self.send = store, sources, send
        self.timezone = ZoneInfo(timezone)
        if default_mode not in {"text", "image"}:
            raise ValueError("digest_send_mode 必须为 text 或 image")
        self.default_mode = default_mode
        self.send_image = send_image
        self.image_fallback = image_fallback
        self.image_rows = max(1, min(10, int(image_rows)))
        self.lock = asyncio.Lock()
        self.retry_after = {}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS digests (
                origin TEXT NOT NULL, source TEXT NOT NULL, time TEXT NOT NULL,
                count INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                last_sent TEXT, PRIMARY KEY(origin,source));
            CREATE TABLE IF NOT EXISTS digest_parts (
                origin TEXT NOT NULL, source TEXT NOT NULL, day TEXT NOT NULL,
                part INTEGER NOT NULL, body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(origin,source,day,part));
        """)
        # Additive migrations preserve old text snapshots and per-part delivery history.
        columns = {r["name"] for r in store.db.execute("PRAGMA table_info(digests)")}
        if "mode" not in columns:
            store.db.execute("ALTER TABLE digests ADD COLUMN mode TEXT NOT NULL DEFAULT 'default'")
        columns = {r["name"] for r in store.db.execute("PRAGMA table_info(digest_parts)")}
        if "kind" not in columns:
            store.db.execute(
                "ALTER TABLE digest_parts ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"
            )
        if "progress" not in columns:
            store.db.execute(
                "ALTER TABLE digest_parts ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"
            )
        store.db.commit()

    def manage(self, event, action, source="jump", at="", count=5, mode="default"):
        if action not in {"subscribe", "unsubscribe", "list", "pause", "resume", "set_mode"}:
            raise ValueError("日报操作：subscribe/unsubscribe/list/pause/resume/set_mode")
        if source not in {*self.sources, "all"}:
            raise ValueError("日报来源：jump/switch/heybox/steam/all")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise ValueError("数量须为 1-20 的整数")
        mode = mode_name(mode)
        origin = event.unified_msg_origin
        if action == "list":
            rows = self.store.db.execute(
                "SELECT * FROM digests WHERE origin=? ORDER BY source", (origin,)
            ).fetchall()
            return (
                "本会话折扣日报订阅：\n"
                + (
                    "\n".join(
                        f"{r['source']} · {r['time']} · {r['count']}款 · {'开启' if r['enabled'] else '暂停'} · {r['mode']}（实际 {self.default_mode if r['mode'] == 'default' else r['mode']}）"
                        for r in rows
                    )
                    or "暂无订阅。"
                )
                + f"\n时区：{self.timezone.key}"
            )
        if event.get_group_id() and not event.is_admin():
            raise ValueError("群聊折扣日报的订阅与开关仅 AstrBot 管理员可修改。")
        keys = list(self.sources) if source == "all" else [source]
        if action == "set_mode" and not any(
            self.store.db.execute(
                "SELECT 1 FROM digests WHERE origin=? AND source=?", (origin, key)
            ).fetchone()
            for key in keys
        ):
            raise ValueError("当前来源尚未订阅日报，请先使用 /订阅折扣日报。")
        if action == "subscribe":
            at = clock_time(at or self.store.default_time)
        with self.store.db:
            for key in keys:
                if action == "subscribe":
                    self.store.db.execute(
                        """INSERT INTO digests(origin,source,time,count,mode) VALUES(?,?,?,?,?)
                        ON CONFLICT(origin,source) DO UPDATE SET time=excluded.time,count=excluded.count,enabled=1,mode=excluded.mode""",
                        (origin, key, at, count, mode),
                    )
                elif action == "set_mode":
                    self.store.db.execute(
                        "UPDATE digests SET mode=? WHERE origin=? AND source=?", (mode, origin, key)
                    )
                elif action == "unsubscribe":
                    self.store.db.execute(
                        "DELETE FROM digests WHERE origin=? AND source=?", (origin, key)
                    )
                    # Keep today's delivery history to avoid resubscribe causing duplicate notifications.
                else:
                    self.store.db.execute(
                        "UPDATE digests SET enabled=? WHERE origin=? AND source=?",
                        (int(action == "resume"), origin, key),
                    )
        return (
            f"折扣日报已处理：{action}，来源 {source}，发送方式 {mode if mode != 'default' else '跟随全局 ' + self.default_mode}"
            + (
                f"，每天 {at}，每来源最多 {count} 款（{self.timezone.key}）。"
                if action == "subscribe"
                else "。"
            )
        )

    async def tick(self, now=None):
        async with self.lock:
            now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
            day = now.date().isoformat()
            rows = self.store.db.execute(
                """SELECT * FROM digests WHERE enabled=1 AND time<=?
                AND (last_sent IS NULL OR last_sent<>?)""",
                (now.strftime("%H:%M"), day),
            ).fetchall()
            with self.store.db:
                self.store.db.execute("DELETE FROM digest_parts WHERE day<>?", (day,))
            active = {(r["origin"], r["source"]) for r in rows}
            self.retry_after = {k: v for k, v in self.retry_after.items() if k in active}
            listings = {}
            for row in rows:
                key = (row["origin"], row["source"])
                if time.monotonic() < self.retry_after.get(key, 0):
                    continue
                try:
                    parts = self.store.db.execute(
                        "SELECT * FROM digest_parts WHERE origin=? AND source=? AND day=? ORDER BY part",
                        (*key, day),
                    ).fetchall()
                    if not parts:
                        listing_key = (row["source"], row["count"])
                        if listing_key not in listings:
                            listings[listing_key] = await self.sources[row["source"]].today(
                                row["count"]
                            )
                        games = listings[listing_key]
                        mode = self.default_mode if row["mode"] == "default" else row["mode"]
                        prepared = []
                        if mode == "image":
                            chunks = [
                                games[i : i + self.image_rows]
                                for i in range(0, len(games), self.image_rows)
                            ] or [[]]
                            for number, chunk in enumerate(chunks):
                                snapshot = {
                                    "day": day,
                                    "source": row["source"],
                                    "games": [asdict(g) for g in chunk],
                                    "page": number + 1,
                                    "pages": len(chunks),
                                    "start": number * self.image_rows,
                                    "total": len(games),
                                    "timezone": self.timezone.key,
                                    "generated": now.strftime("%Y-%m-%d %H:%M"),
                                    "text": self._text(row["source"], day, chunk),
                                }
                                prepared.append(("image", json.dumps(snapshot, ensure_ascii=False)))
                        else:
                            text = self._text(row["source"], day, games)
                            prepared = [
                                ("text", text[i : i + 2400]) for i in range(0, len(text), 2400)
                            ]
                        with self.store.db:
                            for number, (kind, body) in enumerate(prepared):
                                self.store.db.execute(
                                    "INSERT INTO digest_parts(origin,source,day,part,body,kind) VALUES(?,?,?,?,?,?)",
                                    (*key, day, number, body, kind),
                                )
                        parts = self.store.db.execute(
                            "SELECT * FROM digest_parts WHERE origin=? AND source=? AND day=? ORDER BY part",
                            (*key, day),
                        ).fetchall()
                    for part in parts:
                        current = self.store.db.execute(
                            "SELECT enabled FROM digests WHERE origin=? AND source=?", key
                        ).fetchone()
                        if not current or not current["enabled"]:
                            break
                        if part["sent"]:
                            continue
                        await self._deliver(row["origin"], part)
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE digest_parts SET sent=1 WHERE origin=? AND source=? AND day=? AND part=?",
                                (*key, day, part["part"]),
                            )
                    pending = self.store.db.execute(
                        "SELECT 1 FROM digest_parts WHERE origin=? AND source=? AND day=? AND sent=0",
                        (*key, day),
                    ).fetchone()
                    if not pending:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE digests SET last_sent=? WHERE origin=? AND source=?",
                                (day, *key),
                            )
                        self.retry_after.pop(key, None)
                except Exception:
                    self.retry_after[key] = time.monotonic() + 300
                    logger.exception("每日折扣日报失败；5分钟后重试，来源=%s", row["source"])

    @staticmethod
    def _text(source, day, games):
        text = f"🎮 每日当前折扣 · {source} · {day}\n\n" + "\n\n".join(g.text() for g in games)
        if not games:
            text += "本次来源扫描范围内暂无符合条件的折扣。"
        return text + "\n\n当前扫描范围内的优惠，非今日新开或全站完整列表。"

    async def _deliver(self, origin, part):
        if part["kind"] == "text":
            if await self.send(origin, part["body"]) is False:
                raise RuntimeError("未找到主动消息平台")
            return
        snapshot = json.loads(part["body"])
        if part["kind"] == "image":
            try:
                if self.send_image is None:
                    raise RuntimeError("图片发送器尚未配置")
                if await self.send_image(origin, snapshot) is False:
                    raise RuntimeError("平台未接受图片")
                return
            except Exception:
                if not self.image_fallback:
                    raise
                logger.warning("日报图片生成/发送失败，本页回退文字", exc_info=True)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE digest_parts SET kind='fallback' WHERE origin=? AND source=? AND day=? AND part=?",
                        (origin, part["source"], part["day"], part["part"]),
                    )
        # Fallback text may span messages: persist its own checkpoints before marking the image page complete.
        chunks = [snapshot["text"][i : i + 2400] for i in range(0, len(snapshot["text"]), 2400)]
        for number in range(part["progress"], len(chunks)):
            current = self.store.db.execute(
                "SELECT enabled FROM digests WHERE origin=? AND source=?", (origin, part["source"])
            ).fetchone()
            if not current or not current["enabled"]:
                raise RuntimeError("日报在发送中暂停或退订")
            if await self.send(origin, chunks[number]) is False:
                raise RuntimeError("文字回退发送失败")
            with self.store.db:
                self.store.db.execute(
                    "UPDATE digest_parts SET progress=? WHERE origin=? AND source=? AND day=? AND part=?",
                    (number + 1, origin, part["source"], part["day"], part["part"]),
                )
