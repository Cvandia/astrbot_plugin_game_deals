# v1.4.0 验证记录

日期：2026-11-14（北京时间）。验证环境：Python 3.12.14、aiohttp 3.14.4、APScheduler 3.11.3。

## 自动验证

- `python -m unittest discover -s tests -q`：90 项通过。
- `python -m compileall -q .`：通过。
- 使用 AstrBot 上游 `register_llm_tool` 及 `docstring_parser`，验证 7 个工具的参数名、JSON Schema 类型与 Python 签名一致，`event` 不暴露给模型。此检查替换注册表存储协作者，不是完整 AstrBot 加载测试。
- Web URL 校验算法固定向量与 huandu/heybox-url 上游 JavaScript 输出一致：固定时间戳 1700000000、测试 nonce ABC123DEF456、路径 /game/get_game_list_v3/ 得到 G12HX00。

主要回归覆盖：

- 原 Jump Switch 2 / Steam 查询、缓存、金额单位、时区、每日去重、订阅权限、重启恢复。
- Jump 扩展 Switch / Switch 2 范围时保留严格版本标记。
- 小黑盒匿名目录分页、名字写法、5分钟缓存、已知ID与主机平台验证、金额转换及实际折扣过滤。
- 马车8版本映射、跨源价格比较、缺失报价、不匹配版本拒绝及多候选不猜测。
- 日报群聊权限、来源独立、时间、暂停恢复、跨天、当日退订再订阅不重复。
- 日报源失败与5分钟退避、部分分段已发送后的重启恢复、只重试未发段落。
- 不同会话分别投递，同一轮相同来源和数量复用列表查询。
- 独立命令和比价/日报 LLM Tool 共用业务逻辑与权限。
- 真实 APScheduler 启动立即触发、30秒interval、时区、coalesce/max_instances及卸载取消。

## 真实只读接口验证

`python smoke_compare.py`：通过。实际核对 Jump `jump:294` 与小黑盒 `heybox:900000090` 是 Switch《马力欧卡丁车8 豪华版》；保留地区差异，不与 Switch 2《马力欧卡丁车：世界》混比。

验证时刻 2026-11-14 22:50 北京时间，Jump 返回中国台湾人民币折算 271.60 元，小黑盒返回日本人民币折算 274.80 元，均无折扣。价格仅代表验证时刻，不能作为后续报价。

已验证小黑盒以下接口匿名可用：

- `/game/get_game_list_v3/`：Switch 系列目录及 platform_infos 价格字段。
- `/game/get_game_infos/`：base_infos 的价格、名称、ID。
- `/game/console/get_game_detail/`：appid 和 platforms_list 的主机平台标记。

`/game/search/` 和官网的综合搜索接口实际要求登录，未假装这些接口可匿名搜索。当前版本使用有限范围目录本地检索。官网公开 JavaScript 中的目录/详情路径、平台字段已交叉核对；没有调用收藏、购买、登录或其他账号写入接口。

小黑盒目录检索“马里奥赛车8”及当前折扣列表也由 `smoke_compare.py` 验证通过。Jump / Steam 既有真实 smoke test 在先前版本通过，本轮未更改 Steam 数据适配逻辑。

## 剩余现场验证

本工作区没有运行完整 AstrBot、连接真实聊天平台或模型服务。命令测试使用 AstrBot API 替身；每日定时发送测试使用消息发送替身。需要在用户实例验证插件加载、工具启用、模型实际选择工具、以及群聊/私聊实际投递。投递步骤和平台限制见 README。

来源价格可能是对同一商店的不同汇率/更新快照，不能据此宣称独立销售渠道或全网最低价。日报分段状态虽持久化，仍不保证进程崩溃等故障下的严格一次投递。

## v1.4.0 图片日报验证

在 Pillow 13.0.0 环境生成真实 Jump Switch 2 折扣日报预览，并实际打开 PNG 检查米白/橙色配色、大星期、中文字体、编号列表、地区及价格排版。图像宽1200像素，高度随内容自适应；当前交付预览包含5款实际查询结果，日期与更新时间写在图片中，价格只代表查询时刻。

新增12项测试覆盖：全局image模式、订阅text覆盖、中文样式别名与管理员权限、图片分页后重启只补发未完成页、图片失败后文字分段续发、关闭回退后保持image待重试、切样式不改写已有快照、中文及超长文本换行、空日报、缺失字体、旧表增量迁移，以及独立样式指令和LLM mode参数。

数据库增量新增 `digests.mode`、`digest_parts.kind`、`digest_parts.progress`，原文字快照、sent标记、检查时间均保留。图片内容与回退文字共享一页快照，文字回退另存分段进度。

图片消息使用 AstrBot 上游可见的 `MessageChain.file_image` API。没有连接真实聊天平台，因此实际图片投递与平台限制仍需用户部署验证。没有在任何聊天中发送测试消息。
