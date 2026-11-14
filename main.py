from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .comparison import Comparison
from .deals import SourceError, SteamSource, Store, Watcher, app_id, clock_time
from .digest import DigestService
from .digest_image import DigestImageRenderer, mode_name
from .heybox import HeyboxSource, heybox_id
from .jump import JumpSource, SourceRouter, jump_id
from .scheduling import start_scheduler

HELP = """🎮 游戏折扣帮助
/游戏比价 马车8
/游戏搜索 switch 马力欧卡丁车8
/游戏搜索 jump 马力欧
/游戏搜索 heybox 马力欧
/游戏查价 jump:294
/今日折扣 [jump|switch|heybox|steam] [数量]
/订阅游戏 jump:294
/退订游戏 jump:294
/游戏订阅列表
/检查游戏折扣
/折扣通知时间 20:30
/暂停游戏通知 或 /恢复游戏通知
/订阅折扣日报 20:30 [jump|switch|heybox|steam|all] [数量]
/退订折扣日报 [来源]
/折扣日报列表
/折扣日报样式 image [来源]
/暂停折扣日报 或 /恢复折扣日报 [来源]

jump默认Switch 2；switch为Jump Switch；heybox为小黑盒Switch系列。
马车8是Switch豪华版本体，与Switch 2马车世界分开比较。
日报与单游戏订阅独立；群内通知设置及日报修改需AstrBot管理员。
小黑盒搜索仅限匿名目录范围，价格为跨区人民币参考值。
兼容旧入口 /游戏折扣。"""


@register(
    "astrbot_plugin_game_deals", "Cvandia", "Jump/小黑盒综合比价、游戏与折扣日报订阅", "1.4.1"
)
class GameDealsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None
        self.active_checks: set[asyncio.Task] = set()
        self.store: Store | None = None
        self.source: SteamSource | None = None

    async def initialize(self):
        timezone = str(self.config.get("timezone", "Asia/Shanghai"))
        ZoneInfo(timezone)
        self.limit = max(1, min(200, int(self.config.get("max_subscriptions", 50))))
        self.source = SteamSource(
            str(self.config.get("country", "cn")),
            max(5, min(60, int(self.config.get("request_timeout", 20)))),
        )
        self.jump = JumpSource(self.source.timeout, int(self.config.get("jump_max_pages", 5)))
        self.default_source = str(self.config.get("default_source", "jump"))
        if self.default_source not in {"jump", "switch", "heybox", "steam"}:
            raise ValueError("default_source 必须为 jump/switch/heybox/steam")
        self.jump_switch = JumpSource(self.source.timeout, self.jump.max_pages, scope="switch")
        self.jump_all = JumpSource(self.source.timeout, self.jump.max_pages, scope="all")
        self.heybox = HeyboxSource(self.source.timeout, int(self.config.get("heybox_max_pages", 5)))
        self.router = SourceRouter(self.source, self.jump, self.heybox, self.jump_all)
        self.comparison = Comparison(self.jump_all, self.heybox)
        self.store = Store(
            StarTools.get_data_dir("astrbot_plugin_game_deals") / "subscriptions.sqlite3",
            clock_time(str(self.config.get("default_time", "09:00"))),
        )
        self.watcher = Watcher(self.store, self.router, self._send, timezone)
        self.digest_image_dir = (
            StarTools.get_data_dir("astrbot_plugin_game_deals") / "digest_images"
        )
        self.image_renderer = None
        self.digests = DigestService(
            self.store,
            {
                "jump": self.jump,
                "switch": self.jump_switch,
                "heybox": self.heybox,
                "steam": self.source,
            },
            self._send,
            timezone,
            send_image=self._send_digest_image,
            default_mode=str(self.config.get("digest_send_mode", "text")),
            image_fallback=bool(self.config.get("digest_image_fallback", True)),
            image_rows=int(self.config.get("digest_image_rows", 8)),
        )
        self.scheduler = start_scheduler(self, self.watcher.timezone)

    async def _send(self, origin: str, text: str):
        return await self.context.send_message(origin, MessageChain().message(text))

    async def _send_digest_image(self, origin, snapshot):
        def render():
            if self.image_renderer is None:
                self.image_renderer = DigestImageRenderer(
                    self.digest_image_dir, str(self.config.get("digest_font_path", ""))
                )
            path = self.image_renderer.render(snapshot)
            self.image_renderer.cleanup()
            return path

        path = await asyncio.to_thread(render)
        return await self.context.send_message(origin, MessageChain().file_image(str(path)))

    async def tick(self):
        task = asyncio.current_task()
        self.active_checks.add(task)
        try:
            await self.watcher.tick()
            if getattr(self, "digests", None):
                await self.digests.tick()
        finally:
            self.active_checks.discard(task)

    async def terminate(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            await asyncio.sleep(0)  # Let AsyncIOScheduler cancel its executor jobs.
        for task in list(self.active_checks):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.source:
            await self.source.close()
        if getattr(self, "jump", None):
            await self.jump.close()
        for name in ("jump_switch", "jump_all", "heybox"):
            extra = getattr(self, name, None)
            if extra:
                await extra.close()
        if self.store:
            self.store.close()

    async def _llm_dispatch(self, event: AstrMessageEvent, args: list[str]) -> str:
        """Use the command service so LLM calls cannot bypass chat permissions."""
        if self.store is None or self.source is None:
            return self._tool_error("插件尚未就绪，请稍后重试。")
        if not event.unified_msg_origin or not event.get_sender_id():
            return self._tool_error("缺少当前会话或发送者信息，无法执行。")
        try:
            result = await self.handle(event, args)
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except (ValueError, SourceError) as exc:
            return self._tool_error(str(exc))
        except Exception:
            logger.exception("游戏折扣 LLM 工具执行失败")
            return self._tool_error("处理失败，请稍后重试；管理员可查看插件日志。")

    @staticmethod
    def _tool_error(message: str) -> str:
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)

    @staticmethod
    def _tool_source(source: str) -> str:
        if not isinstance(source, str) or source.lower() not in {
            "jump",
            "switch2",
            "switch",
            "heybox",
            "steam",
        }:
            raise ValueError(
                "source：jump/switch2（Switch 2）、switch（Jump Switch）、heybox（小黑盒）、steam。"
            )
        return "jump" if source.lower() == "switch2" else source.lower()

    @staticmethod
    def _tool_game_id(game_id: str) -> str:
        if not isinstance(game_id, str):
            raise ValueError("请使用搜索工具返回的完整游戏 ID。")
        game_id = game_id.strip()
        if game_id.lower().startswith("heybox:"):
            return heybox_id(game_id)
        if game_id.lower().startswith("jump:"):
            return jump_id(game_id)
        if game_id.lower().startswith("steam:"):
            return "steam:" + app_id(game_id.split(":", 1)[1])
        raise ValueError("请先搜索并使用完整 ID：jump:53387 或 steam:1145360；不要猜测游戏 ID。")

    @llm_tool(name="game_deals_search")
    async def tool_search_games(
        self, event: AstrMessageEvent, keyword: str, source: str = "jump"
    ) -> str:
        """按名称搜索游戏，返回可查询和订阅的完整 ID。Switch 2 使用 jump，仅返回 Jump 标记的 Switch 2 版本，不混入 Switch 初代。名称有歧义时展示候选供用户选择，不猜测 ID。

        Args:
            keyword(string): 游戏名称关键词，例如马力欧或 Hades。
            source(string): 来源：jump/switch2 为 Switch 2，switch 为 Jump Switch，heybox 为小黑盒 Switch系列目录，steam 为 Steam；默认 jump。
        """
        try:
            selected = self._tool_source(source)
            if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 200:
                raise ValueError("请提供 1-200 字符的游戏名称。")
            return await self._llm_dispatch(event, [selected, "搜索", keyword.strip()])
        except ValueError as exc:
            return self._tool_error(str(exc))

    @llm_tool(name="game_deals_today")
    async def tool_today_deals(
        self, event: AstrMessageEvent, source: str = "jump", limit: int = 10
    ) -> str:
        """查询当前折扣游戏。默认 Jump Switch 2。结果是来源扫描范围内的当前折扣，不代表今日新开或全站全部优惠；Jump 价格为人民币折算参考值，回复时保留地区和平台。

        Args:
            source(string): 来源：jump/switch2 为 Switch 2，switch 为 Jump Switch，heybox 为小黑盒 Switch系列目录，steam 为 Steam；默认 jump。
            limit(number): 返回数量，1 到 20 的整数，默认 10。
        """
        try:
            selected = self._tool_source(source)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or not 1 <= limit <= 20
                or int(limit) != limit
            ):
                raise ValueError("limit 必须是 1-20 的整数。")
            return await self._llm_dispatch(event, [selected, "今日", str(int(limit))])
        except ValueError as exc:
            return self._tool_error(str(exc))

    @llm_tool(name="game_deals_price")
    async def tool_game_price(self, event: AstrMessageEvent, game_id: str) -> str:
        """查询指定ID的单来源游戏价格及折扣。用户用游戏名称询价或要求综合比价时优先使用 game_deals_compare。使用搜索结果中的完整 ID，不猜测 ID。支持 Jump Switch/Switch 2 与小黑盒；回复保留价格地区、人民币折算标记、来源和折扣时间。

        Args:
            game_id(string): 搜索返回的完整 ID，例如 jump:53387、heybox:900000090 或 steam:1145360。
        """
        try:
            return await self._llm_dispatch(event, ["查询", self._tool_game_id(game_id)])
        except ValueError as exc:
            return self._tool_error(str(exc))

    @llm_tool(name="game_deals_subscriptions")
    async def tool_subscriptions(
        self, event: AstrMessageEvent, action: str, game_id: str = ""
    ) -> str:
        """管理当前会话游戏订阅。仅在用户明确要求订阅或退订时使用对应操作；查询价格不自动订阅。群聊通知本群，私聊通知本人。退订受创建者/管理员权限限制。不能指定其他群或用户。名称不明确时先搜索让用户选版本。

        Args:
            action(string): subscribe 订阅、unsubscribe 退订、list 查看订阅及通知时间、check 立即查询订阅中的折扣。
            game_id(string): subscribe/unsubscribe 必填搜索返回的完整 ID，例如 jump:53387；list/check 留空。
        """
        try:
            actions = {"subscribe": "订阅", "unsubscribe": "退订", "list": "列表", "check": "检查"}
            if not isinstance(action, str) or action not in actions:
                raise ValueError("action 只能是 subscribe、unsubscribe、list 或 check。")
            args = [actions[action]]
            if action in {"subscribe", "unsubscribe"}:
                args.append(self._tool_game_id(game_id))
            elif game_id != "":
                raise ValueError("list/check 不接收 game_id。")
            return await self._llm_dispatch(event, args)
        except ValueError as exc:
            return self._tool_error(str(exc))

    @llm_tool(name="game_deals_schedule")
    async def tool_schedule(self, event: AstrMessageEvent, action: str, time: str = "") -> str:
        """用户明确要求时，设置当前会话每日检查时间或暂停/恢复通知。定时由 APScheduler 执行；群聊必须为 AstrBot 管理员，不能通过工具绕过。只作用于当前会话，不自动创建游戏订阅。不要将权限错误回复为成功。

        Args:
            action(string): set_time 设置每日时间、pause 暂停、resume 恢复。
            time(string): set_time 必填 HH:MM（24 小时制），例如 20:30，按插件配置时区（默认北京时间）；pause/resume 留空。
        """
        try:
            actions = {"set_time": "时间", "pause": "暂停", "resume": "恢复"}
            if not isinstance(action, str) or action not in actions:
                raise ValueError("action 只能是 set_time、pause 或 resume。")
            args = [actions[action]]
            if action == "set_time":
                if not isinstance(time, str):
                    raise ValueError("time 必须是 HH:MM 格式。")
                args.append(clock_time(time))
            elif time != "":
                raise ValueError("pause/resume 不接收 time。")
            return await self._llm_dispatch(event, args)
        except ValueError as exc:
            return self._tool_error(str(exc))

    async def _command_results(self, event, args):
        result = json.loads(await self._llm_dispatch(event, args))
        text = result.get("result") if result["ok"] else result["error"]
        for offset in range(0, len(text), 2800):
            yield event.plain_result(text[offset : offset + 2800])

    def _tail(self, event):
        parts = event.get_message_str().strip().split(maxsplit=1)
        return parts[1].split() if len(parts) > 1 else []

    def _source_args(self, event, op):
        args = self._tail(event)
        if args and args[0].lower() in {"jump", "switch2", "switch", "steam", "heybox"}:
            return [args[0], op, *args[1:]]
        return [op, *args]

    @llm_tool(name="game_deals_compare")
    async def tool_compare(self, event: AstrMessageEvent, query: str) -> str:
        """综合对比 Jump 与小黑盒的同款游戏价格，支持马车8别名。保留版本、平台、地区与来源；版本有歧义时返回候选，不把马车8与马车世界或DLC混比。来源失败或代际未确认时不声称全网最低价。

        Args:
            query(string): 游戏名称或别名，例如马车8，或完整精确版本名称。
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return self._tool_error("请输入 1-200 字符的游戏名称。")
        return await self._llm_dispatch(event, ["比价", query])

    @llm_tool(name="game_deals_daily_digest")
    async def tool_daily_digest(
        self,
        event: AstrMessageEvent,
        action: str,
        source: str = "jump",
        time: str = "",
        limit: int = 5,
        mode: str = "default",
    ) -> str:
        """用户明确要求时，订阅每天推送当前折扣游戏列表，不必先订阅单款游戏。仅操作当前会话，群内修改需AstrBot管理员。APScheduler按指定时区触发，默认北京时间。日报独立于单款游戏提醒；all按来源分别推送，非全站完整列表。

        Args:
            action(string): subscribe订阅或更新时间、unsubscribe退订、list查看、pause暂停、resume恢复、set_mode只修改发送方式。
            source(string): jump为Switch 2、switch为Jump Switch、heybox为小黑盒Switch系列、steam或all；默认jump。
            time(string): subscribe时的HH:MM每日推送时间，例如20:30；留空使用插件默认时间。
            limit(number): 每个来源每天最多推送多少款，1-20整数，默认5。
            mode(string): image图片日报、text文字日报、default跟随全局；subscribe或set_mode时生效，默认default。
        """
        if not isinstance(action, str) or not isinstance(source, str) or not isinstance(time, str):
            return self._tool_error("日报操作、来源与时间须为字符串。")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, (int, float))
            or not 1 <= limit <= 20
            or int(limit) != limit
        ):
            return self._tool_error("数量须为1-20整数。")
        try:
            selected_mode = mode_name(mode)
        except (ValueError, TypeError):
            return self._tool_error("mode 须为 image、text 或 default。")
        return await self._llm_dispatch(
            event, ["日报", action, source, time, str(int(limit)), selected_mode]
        )

    @filter.command("游戏折扣")
    async def game_deals(self, event: AstrMessageEvent):
        if self.store is None or self.source is None:
            yield event.plain_result("插件尚未就绪，请稍后重试。")
            return
        try:
            # Preserve spaces in game names without requiring quotes.
            raw = event.get_message_str().strip()
            parts = raw.split(maxsplit=1)
            args = parts[1].split() if len(parts) > 1 else []
            if not args or args[0] in {"帮助", "help"}:
                text = HELP
            else:
                text = await self.handle(event, args)
        except (ValueError, SourceError) as exc:
            text = str(exc)
        except Exception:
            logger.exception("游戏折扣命令执行失败")
            text = "处理失败，请稍后重试；管理员可查看插件日志。"
        for offset in range(0, len(text), 2800):
            yield event.plain_result(text[offset : offset + 2800])

    async def handle(self, event, args: list[str]) -> str:
        selected = getattr(self, "default_source", "steam")
        if args[0].lower() in {"steam", "jump", "switch2", "switch", "heybox"}:
            selected = self._tool_source(args[0])
            args = args[1:]
            if not args:
                return HELP
        op, rest = args[0], args[1:]
        if selected == "jump":
            source = self.jump
        elif selected == "switch":
            source = self.jump_switch
        elif selected == "heybox":
            source = self.heybox
        else:
            source = self.source
        source_label = (
            getattr(source, "label", f"Steam / {self.source.country.upper()}")
            if selected != "jump"
            else JumpSource.label
        )
        origin = event.unified_msg_origin
        owner = str(event.get_sender_id())
        admin = event.is_admin()
        if op == "比价":
            return await self.comparison.compare(" ".join(rest))
        if op == "日报":
            if not rest:
                return self.digests.manage(event, "list")
            action = rest[0]
            daily_source = rest[1] if len(rest) > 1 else "jump"
            at = rest[2] if len(rest) > 2 else ""
            count = int(rest[3]) if len(rest) > 3 else 5
            mode = rest[4] if len(rest) > 4 else "default"
            return self.digests.manage(event, action, daily_source, at, count, mode)
        if op == "搜索":
            if not rest:
                raise ValueError("用法：/游戏折扣 搜索 游戏名称")
            games = await source.search(" ".join(rest))
            if selected == "steam":
                games = [(f"steam:{id_}", name) for id_, name in games]
            return (
                f"搜索结果 · {source_label}（使用返回的完整 ID 订阅）：\n"
                + "\n".join(f"{name} — {id_}" for id_, name in games)
                if games
                else "本次搜索范围内未找到游戏，请使用更精确的名称。Jump 结果仅包含所选平台范围；小黑盒仅在有限目录内检索。"
            )
        if op == "今日":
            if len(rest) > 1:
                raise ValueError("用法：/游戏折扣 今日 [数量1-20]")
            limit = int(rest[0]) if rest else 10
            if not 1 <= limit <= 20:
                raise ValueError("数量范围为 1-20。")
            games = await source.today(limit)
            return (
                f"🎮 当前折扣 · {source_label}\n\n"
                + ("\n\n".join(g.text() for g in games) or "本次扫描范围内暂无符合条件的折扣。")
                + "\n\n价格以商店实时结算为准；此列表非全站完整折扣。"
                + (
                    f"Jump 最多扫描 {self.jump.max_pages} 页，已过滤 Switch 初代条目。"
                    if selected == "jump"
                    else ""
                )
            )
        if op in {"订阅", "退订", "查询"}:
            if len(rest) != 1:
                raise ValueError(f"用法：/游戏折扣 {op} 完整游戏ID或Steam商店链接")
            raw_id = rest[0]
            if raw_id.lower().startswith("heybox:"):
                id_ = heybox_id(raw_id)
            elif raw_id.lower().startswith("jump:"):
                id_ = jump_id(raw_id)
            elif raw_id.lower().startswith("steam:"):
                id_ = app_id(raw_id.split(":", 1)[1])
            elif selected == "heybox":
                id_ = heybox_id("heybox:" + raw_id)
            elif raw_id.startswith("http") or selected == "steam":
                id_ = app_id(raw_id)
            else:
                id_ = jump_id(raw_id)
            if op == "退订":
                return (
                    "已退订。"
                    if self.store.unsubscribe(origin, id_, owner, admin)
                    else "本会话尚未订阅此游戏。"
                )
            game = await (self.router if hasattr(self, "router") else self.source).game(id_)
            if op == "查询":
                return game.text() + f"\n数据：{game.source}"
            added = self.store.subscribe(origin, game, owner, self.limit)
            settings = self.store.settings(origin)
            return (
                ("已订阅" if added else "本会话已订阅")
                + f"：{game.name}\n每天 {settings['time']}（{self.watcher.timezone.key}）检查，有折扣通知当前会话。"
                + ("\n当前通知已暂停，使用“恢复”启用。" if not settings["enabled"] else "")
            )
        if op == "列表":
            settings = self.store.settings(origin)
            rows = self.store.subscriptions(origin)
            return (
                f"本会话订阅 · {'启用' if settings['enabled'] else '暂停'}\n"
                f"每日 {settings['time']}（{self.watcher.timezone.key}）\n"
                + (
                    "\n".join(
                        f"{r['name']} — {r['appid'] if r['appid'].startswith(('jump:', 'heybox:')) else 'steam:' + r['appid']}"
                        for r in rows
                    )
                    or "暂无订阅。"
                )
            )
        if op in {"时间", "暂停", "恢复"}:
            if event.get_group_id() and not admin:
                raise ValueError("群聊的通知时间和开关仅 AstrBot 管理员可修改。")
            if op == "时间":
                if len(rest) != 1:
                    raise ValueError("用法：/游戏折扣 时间 HH:MM")
                at = clock_time(rest[0])
                self.store.configure(origin, at=at)
                return f"已设置每天 {at}（{self.watcher.timezone.key}）检查。当天已检查的游戏从明天生效。"
            self.store.configure(origin, enabled=op == "恢复")
            return "已暂停通知，订阅仍保留。" if op == "暂停" else "已恢复每日检查。"
        if op == "检查":
            rows = self.store.subscriptions(origin)
            if not rows:
                return "本会话暂无订阅。"
            results = []
            for row in rows:
                try:
                    game = await (self.router if hasattr(self, "router") else self.source).game(
                        row["appid"]
                    )
                    if game.discounted:
                        results.append(game.text())
                except SourceError:
                    results.append(f"{row['name']}：查询失败，请稍后重试。")
            return "当前订阅折扣（手动查询不改变每日通知记录）：\n\n" + (
                "\n\n".join(results) or "订阅游戏目前均无折扣。"
            )
        return HELP

    @filter.command("游戏搜索")
    async def search_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, self._source_args(event, "搜索")):
            yield result

    @filter.command("游戏查价")
    async def price_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        op = "查询"
        if (
            args
            and not args[0].lower().startswith(("jump:", "steam:", "heybox:", "http"))
            and not args[0].isdigit()
        ):
            op = "比价"
        async for result in self._command_results(event, [op, *args]):
            yield result

    @filter.command("游戏比价")
    async def compare_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["比价", *self._tail(event)]):
            yield result

    @filter.command("今日折扣")
    async def today_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, self._source_args(event, "今日")):
            yield result

    @filter.command("订阅游戏")
    async def subscribe_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["订阅", *self._tail(event)]):
            yield result

    @filter.command("退订游戏")
    async def unsubscribe_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["退订", *self._tail(event)]):
            yield result

    @filter.command("游戏订阅列表")
    async def subscriptions_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["列表"]):
            yield result

    @filter.command("检查游戏折扣")
    async def check_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["检查"]):
            yield result

    @filter.command("折扣通知时间")
    async def schedule_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["时间", *self._tail(event)]):
            yield result

    @filter.command("暂停游戏通知")
    async def pause_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["暂停"]):
            yield result

    @filter.command("恢复游戏通知")
    async def resume_command(self, event: AstrMessageEvent):
        async for result in self._command_results(event, ["恢复"]):
            yield result

    @filter.command("订阅折扣日报")
    async def subscribe_digest_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        if not 1 <= len(args) <= 4:
            yield event.plain_result(
                "用法：/订阅折扣日报 HH:MM [jump|switch|heybox|steam|all] [数量1-20] [image|text|default]"
            )
            return
        async for result in self._command_results(
            event,
            [
                "日报",
                "subscribe",
                args[1] if len(args) > 1 else "jump",
                args[0],
                args[2] if len(args) > 2 else "5",
                args[3] if len(args) > 3 else "default",
            ],
        ):
            yield result

    @filter.command("退订折扣日报")
    async def unsubscribe_digest_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        if len(args) > 1:
            yield event.plain_result("用法：/退订折扣日报 [jump|switch|heybox|steam|all]")
            return
        async for result in self._command_results(
            event, ["日报", "unsubscribe", args[0] if args else "jump"]
        ):
            yield result

    @filter.command("折扣日报列表")
    async def list_digest_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        if len(args) > 1:
            yield event.plain_result("用法：/折扣日报列表 [jump|switch|heybox|steam|all]")
            return
        async for result in self._command_results(
            event, ["日报", "list", args[0] if args else "jump"]
        ):
            yield result

    @filter.command("暂停折扣日报")
    async def pause_digest_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        if len(args) > 1:
            yield event.plain_result("用法：/暂停折扣日报 [jump|switch|heybox|steam|all]")
            return
        async for result in self._command_results(
            event, ["日报", "pause", args[0] if args else "jump"]
        ):
            yield result

    @filter.command("恢复折扣日报")
    async def resume_digest_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        if len(args) > 1:
            yield event.plain_result("用法：/恢复折扣日报 [jump|switch|heybox|steam|all]")
            return
        async for result in self._command_results(
            event, ["日报", "resume", args[0] if args else "jump"]
        ):
            yield result

    @filter.command("折扣日报样式")
    async def digest_mode_command(self, event: AstrMessageEvent):
        args = self._tail(event)
        if not 1 <= len(args) <= 2:
            yield event.plain_result(
                "用法：/折扣日报样式 image|text|default [jump|switch|heybox|steam|all]；也支持图片/文字/默认。"
            )
            return
        async for result in self._command_results(
            event, ["日报", "set_mode", args[1] if len(args) > 1 else "jump", "", "5", args[0]]
        ):
            yield result
