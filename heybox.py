"""Anonymous Switch catalogue and price endpoints from the Heybox web client."""

import asyncio
import re
import time

import aiohttp

from .deals import Game, SourceError
from .heybox_sign import sign
from .jump import money


def heybox_id(value):
    match = re.fullmatch(r"heybox:([1-9]\d{0,12})", value, re.I)
    if not match:
        raise ValueError("请使用 heybox:数字 格式的小黑盒 ID。")
    return "heybox:" + match[1]


def normalized(name):
    name = name.casefold().replace("马里奥", "马力欧").replace("赛车", "卡丁车")
    return "".join(c for c in name if c.isalnum())


class HeyboxSource:
    label = "小黑盒 / Switch 系列 · 目录范围内查询"
    country = "跨区人民币参考价"

    def __init__(self, timeout=20, max_pages=5):
        self.timeout, self.max_pages = timeout, max(1, min(20, max_pages))
        self.session = None
        self.catalogue = None
        self.catalogue_at = 0
        self.cache = {}
        self.lock = asyncio.Lock()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, path, **params):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.xiaoheihe.cn/"},
            )
        common = dict(
            os_type="web",
            app="heybox",
            client_type="mobile",
            version="999.0.3",
            x_client_type="web",
            x_os_type="Mac",
            x_app="heybox",
            heybox_id=-1,
        )
        try:
            async with self.session.get(
                "https://api.xiaoheihe.cn/" + path, params=sign(path, {**common, **params})
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise SourceError("小黑盒接口不可用或要求登录；未取得价格，不能判断为无折扣。")
            if not isinstance(payload.get("result"), dict):
                raise SourceError("小黑盒数据格式变化，请稍后重试。")
            return payload["result"]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise SourceError("小黑盒暂时不可用，请稍后重试。") from exc

    async def catalogue_games(self):
        async with self.lock:
            if self.catalogue is not None and time.monotonic() - self.catalogue_at < 300:
                return self.catalogue
            games, seen = [], set()
            for page in range(self.max_pages):
                payload = await self.request(
                    "game/get_game_list_v3/",
                    filter_head="switch",
                    offset=page * 30,
                    limit=30,
                    include_filter=-1,
                )
                rows = payload.get("games")
                if not isinstance(rows, list):
                    raise SourceError("小黑盒目录结构变化。")
                new = [g for g in rows if str(g.get("appid") or g.get("steam_appid")) not in seen]
                for g in new:
                    seen.add(str(g.get("appid") or g.get("steam_appid")))
                games.extend(new)
                if len(rows) < 30 or not new:
                    break
            self.catalogue, self.catalogue_at = games, time.monotonic()
            return games

    async def search(self, term):
        # Keyword search requires login. Never pretend the bounded catalogue is a full search.
        key = normalized(term)
        return [
            (
                heybox_id("heybox:" + str(g.get("appid") or g["steam_appid"])),
                g["name"] + " [Switch系列，目录范围]",
            )
            for g in await self.catalogue_games()
            if key in normalized(g["name"])
        ][:8]

    def parse(self, item, price, platform="Switch（来源未细分代际）"):
        id_ = heybox_id("heybox:" + str(item.get("appid") or item["steam_appid"]))
        current, initial = (
            money(price.get("current"), yuan=True),
            money(price.get("initial"), yuan=True),
        )
        try:
            percent = int(price.get("discount") or 0)
        except (TypeError, ValueError) as exc:
            raise SourceError("小黑盒折扣格式无效") from exc
        if not 0 <= percent <= 100:
            raise SourceError("小黑盒折扣范围无效")
        expires = price.get("deadline_timestamp")
        expires = int(expires) if expires and str(expires).isdigit() and int(expires) > 0 else None
        if percent and expires and expires <= time.time():
            raise SourceError("小黑盒折扣已过期，等待来源刷新")
        return Game(
            id_,
            item["name"],
            "CNY≈",
            initial,
            current,
            percent,
            bool(item.get("is_free")),
            expires,
            source="小黑盒",
            platform=platform,
            region=price.get("region_name") or "地区未标注",
            url="https://www.xiaoheihe.cn/game/console/" + id_.split(":")[1],
            note="跨区人民币折算参考价；非商城券后价。来源未明确细分 Switch / Switch 2 时不推断代际。",
        )

    async def game(self, id_):
        id_ = heybox_id(id_)
        cached = self.cache.get(id_)
        if (
            cached
            and time.monotonic() - cached[0] < 300
            and (not cached[1].expires or cached[1].expires > time.time())
        ):
            return cached[1]
        appid = id_.split(":")[1]
        info, details = await asyncio.gather(
            self.request("game/get_game_infos/", appids=appid),
            self.request("game/console/get_game_detail/", appid=appid, platf="console"),
        )
        rows = info.get("base_infos")
        if not isinstance(rows, list) or len(rows) != 1 or str(rows[0].get("steam_appid")) != appid:
            raise SourceError("小黑盒游戏 ID 不匹配或不存在")
        if str(details.get("appid")) != appid or not any(
            p.get("platf") == "switch" for p in details.get("platforms_list", [])
        ):
            raise SourceError("该小黑盒条目不是可验证的 Switch 系列游戏")
        if not isinstance(rows[0].get("price"), dict):
            raise SourceError("小黑盒未返回可靠价格")
        game = self.parse(rows[0], rows[0]["price"])
        if len(self.cache) >= 1000:
            self.cache.clear()
        self.cache[id_] = (time.monotonic(), game)
        return game

    async def today(self, limit=10):
        games = []
        for item in await self.catalogue_games():
            for platform in item.get("platform_infos", []):
                if platform.get("key") == "switch" and isinstance(platform.get("price"), dict):
                    game = self.parse(item, platform["price"])
                    if game.discounted:
                        games.append(game)
                    break
            if len(games) >= limit:
                break
        return games
