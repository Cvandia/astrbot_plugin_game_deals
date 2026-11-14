"""Steam data source and persistent subscription service; independent of AstrBot."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A source could not return a trustworthy price."""


def clock_time(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间格式应为 HH:MM，例如 09:00。")
    return value


def app_id(value: str) -> str:
    match = re.fullmatch(r"(?:https?://store\.steampowered\.com/app/)?([1-9]\d{0,9})(?:/[^\s]*)?", value)
    if not match:
        raise ValueError("请提供 Steam AppID 或商店游戏链接；先用“搜索”查找 ID。")
    return match[1]


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    currency: str = ""
    original: int | None = None
    final: int | None = None
    percent: int = 0
    free: bool = False
    expires: int | None = None
    source: str = "Steam"
    platform: str = ""
    region: str = ""
    url: str = ""
    note: str = ""

    @property
    def discounted(self) -> bool:
        return (not self.free and self.percent > 0 and self.original is not None
                and self.final is not None and self.original > self.final >= 0)

    def text(self) -> str:
        if self.free:
            price = "免费游戏（非限时折扣）"
        elif self.final is None:
            price = "当前地区无价格 / 尚未发售"
        elif self.discounted:
            price = f"{self.currency} {self.original / 100:.2f} → {self.final / 100:.2f}（-{self.percent}%）"
        else:
            price = f"{self.currency} {self.final / 100:.2f}（无折扣）"
        end = ""
        if self.expires:
            end = "\n预计结束：" + datetime.fromtimestamp(self.expires, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M 北京时间")
        label = f" [{self.platform}]" if self.platform else ""
        region = f"\n地区：{self.region}" if self.region else ""
        link = self.url or f"https://store.steampowered.com/app/{self.id}/"
        note = f"\n{self.note}" if self.note else ""
        display_id = f"steam:{self.id}" if self.source == "Steam" else self.id
        return f"{self.name}{label} [ID:{display_id}]\n{price}{region}{end}\n{link}{note}"


class SteamSource:
    """Public store APIs. A short cache coalesces checks across sessions."""
    def __init__(self, country: str = "cn", timeout: int = 20):
        if not re.fullmatch(r"[a-z]{2}", country):
            raise ValueError("country 应为两位小写地区代码，例如 cn。")
        self.country = country
        self.timeout = timeout
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, tuple[float, Game]] = {}
        self.lock = asyncio.Lock()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, endpoint: str, **params):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=True,
                headers={"User-Agent": "AstrBot-GameDeals/1.0"})
        try:
            async with self.session.get("https://store.steampowered.com/api/" + endpoint,
                                        params={"cc": self.country, "l": "schinese", **params}) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
                if not isinstance(data, dict):
                    raise SourceError("Steam 返回的数据格式不正确。")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise SourceError("Steam 暂时不可用，请稍后重试。") from exc

    async def search(self, term: str) -> list[tuple[str, str]]:
        data = await self.request("storesearch/", term=term)
        return [(str(x["id"]), x["name"]) for x in data.get("items", [])
                if x.get("type") == "app"][:8]

    async def today(self, limit: int = 10) -> list[Game]:
        data = await self.request("featuredcategories/")
        if not isinstance(data.get("specials", {}).get("items"), list):
            raise SourceError("Steam 折扣列表暂不可用。")
        games = []
        for x in data["specials"]["items"]:
            if x.get("type") != 0:
                continue
            game = Game(str(x["id"]), x["name"], x.get("currency", ""),
                        x.get("original_price"), x.get("final_price"),
                        x.get("discount_percent", 0), expires=x.get("discount_expiration"))
            if game.discounted:
                games.append(game)
        return games[:limit]

    async def game(self, id_: str) -> Game:
        async with self.lock:
            cached = self.cache.get(id_)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            payload = await self.request("appdetails", appids=id_)
            item = payload.get(id_, {})
            if not item.get("success") or not isinstance(item.get("data"), dict):
                raise SourceError("未找到此游戏，或当前地区不可访问。")
            x = item["data"]
            p = x.get("price_overview") or {}
            result = Game(id_, x["name"], p.get("currency", ""), p.get("initial"),
                          p.get("final"), p.get("discount_percent", 0), x.get("is_free", False))
            # Bound memory even when many different games are queried.
            if len(self.cache) >= 1000:
                self.cache.clear()
            self.cache[id_] = (time.monotonic(), result)
            return result


class Store:
    def __init__(self, path: Path, default_time: str = "09:00"):
        self.default_time = clock_time(default_time)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                origin TEXT PRIMARY KEY, time TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS subscriptions (
                origin TEXT NOT NULL REFERENCES sessions(origin), appid TEXT NOT NULL,
                name TEXT NOT NULL, owner TEXT NOT NULL, last_checked TEXT, last_notified TEXT,
                PRIMARY KEY(origin, appid));
        """)
        self.db.commit()

    def ensure(self, origin: str):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sessions(origin,time) VALUES (?,?)", (origin, self.default_time))

    def settings(self, origin: str):
        self.ensure(origin)
        return self.db.execute("SELECT * FROM sessions WHERE origin=?", (origin,)).fetchone()

    def configure(self, origin: str, *, at: str | None = None, enabled: bool | None = None):
        self.ensure(origin)
        with self.db:
            if at is not None:
                self.db.execute("UPDATE sessions SET time=? WHERE origin=?", (clock_time(at), origin))
            if enabled is not None:
                self.db.execute("UPDATE sessions SET enabled=? WHERE origin=?", (int(enabled), origin))

    def subscriptions(self, origin: str):
        return self.db.execute("SELECT * FROM subscriptions WHERE origin=? ORDER BY appid", (origin,)).fetchall()

    def subscribe(self, origin: str, game: Game, owner: str, limit: int) -> bool:
        self.ensure(origin)
        if self.db.execute("SELECT 1 FROM subscriptions WHERE origin=? AND appid=?", (origin, game.id)).fetchone():
            return False
        if len(self.subscriptions(origin)) >= limit:
            raise ValueError(f"本会话最多订阅 {limit} 个游戏，请先退订。")
        with self.db:
            self.db.execute("INSERT INTO subscriptions(origin,appid,name,owner) VALUES (?,?,?,?)",
                            (origin, game.id, game.name, owner))
        return True

    def unsubscribe(self, origin: str, id_: str, owner: str, admin: bool) -> bool:
        row = self.db.execute("SELECT owner FROM subscriptions WHERE origin=? AND appid=?", (origin, id_)).fetchone()
        if not row:
            return False
        if row["owner"] != owner and not admin:
            raise ValueError("仅订阅创建者或 AstrBot 管理员可退订该游戏。")
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE origin=? AND appid=?", (origin, id_))
        return True

    def due(self, now: datetime):
        return self.db.execute("""SELECT sub.* FROM subscriptions sub JOIN sessions s ON s.origin=sub.origin
            WHERE s.enabled=1 AND s.time<=? AND (sub.last_checked IS NULL OR sub.last_checked<>?)""",
                               (now.strftime("%H:%M"), now.date().isoformat())).fetchall()

    def mark(self, origin: str, id_: str, day: str, notified: bool):
        with self.db:
            self.db.execute("""UPDATE subscriptions SET last_checked=?,
                last_notified=CASE WHEN ? THEN ? ELSE last_notified END WHERE origin=? AND appid=?""",
                            (day, notified, day, origin, id_))

    def close(self):
        self.db.close()


class Watcher:
    def __init__(self, store: Store, source: SteamSource, send, timezone: str = "Asia/Shanghai"):
        self.store, self.source, self.send = store, source, send
        self.timezone = ZoneInfo(timezone)
        self.lock = asyncio.Lock()
        self.retry_after: dict[tuple[str, str], float] = {}

    async def tick(self, now: datetime | None = None):
        async with self.lock:
            now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
            day = now.date().isoformat()
            for row in self.store.due(now):
                key = (row["origin"], row["appid"])
                if time.monotonic() < self.retry_after.get(key, 0):
                    continue
                try:
                    game = await self.source.game(row["appid"])
                    notified = False
                    if game.discounted and row["last_notified"] != day:
                        result = await self.send(row["origin"], "🎮 订阅游戏折扣提醒\n" + game.text()
                                                 + f"\n数据：{game.source}\n检查日期：{day}")
                        if result is False:
                            raise RuntimeError("平台未找到或不支持主动消息")
                        notified = True
                    self.store.mark(*key, day, notified)
                    self.retry_after.pop(key, None)
                except Exception:
                    # A failed lookup/send remains due. Retry at most once every five minutes.
                    self.retry_after[key] = time.monotonic() + 300
                    log.exception("折扣查询或通知失败；5 分钟后重试，AppID=%s", row["appid"])
            active = {(r["origin"], r["appid"]) for r in self.store.due(now)}
            self.retry_after = {k: v for k, v in self.retry_after.items() if k in active}
