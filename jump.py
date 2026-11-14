"""Jump Switch 2 source, using verified anonymous read-only app endpoints."""

from __future__ import annotations

import asyncio
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import aiohttp

from .deals import Game, SourceError

BASE = "https://switch.jumpvg.com/jump/"


def jump_id(value: str) -> str:
    match = re.fullmatch(r"(?:jump:)?([1-9]\d{0,9})", value, re.IGNORECASE)
    if not match:
        raise ValueError("请使用搜索结果中的 Jump ID，例如 jump:53387；不要使用 Steam AppID。")
    return "jump:" + match[1]


def money(value, *, yuan: bool = False) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        return int((amount * (100 if yuan else 1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (ValueError, InvalidOperation):
        return None


class JumpSource:
    country = "全球低价 / 人民币估算"
    label = "Jump / Switch 2 · 跨区低价（人民币估算）"

    def __init__(self, timeout: int = 20, max_pages: int = 5, scope: str = "switch2"):
        if scope not in {"switch", "switch2", "all"}:
            raise ValueError("无效 Jump 平台范围")
        self.scope = scope
        self.label = (
            "Jump / "
            + {"switch": "Switch", "switch2": "Switch 2", "all": "Switch 系列"}[scope]
            + " · 跨区人民币参考价"
        )
        self.timeout = timeout
        self.max_pages = max(1, min(20, max_pages))
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, tuple[float, Game]] = {}
        self.lock = asyncio.Lock()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, endpoint: str, *, method: str = "GET", **params):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,
                headers={"User-Agent": "AstrBot-GameDeals/1.1"},
            )
        try:
            async with self.session.request(method, BASE + endpoint, params=params) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
                if not isinstance(data, dict) or data.get("success") is not True:
                    raise SourceError("Jump 查询失败或接口要求登录，请稍后重试并查看接口状态。")
                if "data" not in data:
                    raise SourceError("Jump 返回数据缺少 data，无法判断价格。")
                return data["data"]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise SourceError("Jump 暂时不可用，请稍后重试。") from exc

    @staticmethod
    def is_switch2(item: dict) -> bool:
        # Confirmed against the dedicated Switch 2 catalogue, e.g. Mario Kart World.
        return (
            item.get("platform") == 1
            and item.get("subPlatform") == 11
            and not item.get("isUpgradePass")
            and not item.get("isDlc")
        )

    def accepts(self, item: dict) -> bool:
        sub = item.get("subPlatform")
        return (
            item.get("platform") == 1
            and sub
            in ({10, 11} if self.scope == "all" else {10} if self.scope == "switch" else {11})
            and not item.get("isUpgradePass")
            and not item.get("isDlc")
        )

    def parse(self, item: dict, *, detail: bool = False) -> Game:
        if not self.accepts(item):
            raise SourceError("此条目不是所选 Jump 平台范围内的游戏本体，请选择正确版本。")
        id_ = jump_id(str(item.get("oldGameId", "")))
        original = money(item.get("originPrice"), yuan=not detail)
        final = money(item.get("price"), yuan=not detail)
        pct = item.get("cutoff" if detail else "cutOff", 0)
        try:
            pct = int(pct)
        except (TypeError, ValueError) as exc:
            raise SourceError("Jump 返回的折扣格式无效。") from exc
        if not 0 <= pct <= 100:
            raise SourceError("Jump 返回的折扣范围无效。")
        expiry = item.get("discountEndDate")
        expires = int(expiry) // 1000 if isinstance(expiry, (int, float)) and expiry > 0 else None
        # Stale expired campaigns must never produce a discount notification.
        if pct > 0 and expires and expires <= time.time():
            raise SourceError("Jump 的折扣已过期，等待来源刷新后重试。")
        region = str(item.get("priceCountry") or "地区未标注")
        chinese = str(item.get("chinese") or "中文支持未标注")
        return Game(
            id_,
            str(item["name"]),
            "CNY≈",
            original,
            final,
            pct,
            free=original == 0 and final == 0,
            expires=expires,
            source="Jump",
            platform="Switch 2" if item["subPlatform"] == 11 else "Switch",
            region=region,
            url="https://www.jumpvg.com/inapp",
            note=f"人民币折算参考价；{chinese}。购买前确认该地区语言及购买资格。",
        )

    async def search(self, term: str) -> list[tuple[str, str]]:
        results = []
        seen = set()
        for page in range(self.max_pages):
            payload = await self.request(
                "searchGame/list/v2",
                type=3,
                searchName=term,
                platForm=1,
                offset=page * 30,
                limit=30,
                version=3,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("gameList"), list):
                raise SourceError("Jump 搜索返回结构变化，无法确认游戏结果。")
            items = payload["gameList"]
            new = False
            for item in items:
                id_ = item.get("oldGameId")
                if id_ in seen:
                    continue
                seen.add(id_)
                new = True
                if self.accepts(item):
                    results.append(
                        (
                            jump_id(str(id_)),
                            str(item["name"])
                            + (" [Switch 2]" if item["subPlatform"] == 11 else " [Switch]"),
                        )
                    )
            if len(results) >= 8 or len(items) < 30 or not new:
                break
        return results[:8]

    async def today(self, limit: int = 10) -> list[Game]:
        games = []
        seen = set()
        # v2 ignores subPlatform: filter locally rather than mislabel Switch games.
        for page in range(self.max_pages):
            items = await self.request(
                "discount/find4Discount/5/v2",
                countries="",
                offset=page * 10,
                platform=1,
                size=10,
                termsId=17,
                version=3,
            )
            if not isinstance(items, list):
                raise SourceError("Jump 折扣列表结构变化，请稍后重试。")
            new = False
            for item in items:
                id_ = item.get("oldGameId")
                if id_ in seen:
                    continue
                seen.add(id_)
                new = True
                if self.accepts(item):
                    game = self.parse(item)
                    if game.discounted:
                        games.append(game)
            if len(games) >= limit or len(items) < 10 or not new:
                break
        return games[:limit]

    async def game(self, id_: str) -> Game:
        id_ = jump_id(id_)
        async with self.lock:
            cached = self.cache.get(id_)
            if cached and time.monotonic() - cached[0] < 300:
                if cached[1].expires and cached[1].expires <= time.time():
                    self.cache.pop(id_, None)
                else:
                    return cached[1]
            payload = await self.request(
                "game/detail",
                method="POST",
                id=id_.split(":")[1],
                platform=1,
                version=3,
                clickFrom=-1,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("jumpGame"), dict):
                raise SourceError("Jump 游戏不存在或详情暂不可用。")
            game = self.parse(payload["jumpGame"], detail=True)
            if game.id != id_:
                raise SourceError("Jump 返回的游戏 ID 不匹配，已取消查询。")
            if len(self.cache) >= 1000:
                self.cache.clear()
            self.cache[id_] = (time.monotonic(), game)
            return game


class SourceRouter:
    """Namespaced Jump IDs preserve existing numeric Steam subscription rows."""

    def __init__(self, steam, jump, heybox=None, jump_all=None):
        self.steam, self.jump = steam, jump
        self.heybox, self.jump_all = heybox, jump_all

    async def game(self, id_: str):
        if id_.startswith("heybox:"):
            if self.heybox is None:
                raise SourceError("小黑盒来源尚未就绪")
            return await self.heybox.game(id_)
        return await ((self.jump_all or self.jump) if id_.startswith("jump:") else self.steam).game(
            id_
        )
