# 多快手号方案（kuaishou2 平台别名）

## 目标

在现有德国协调端 + 阿里云发布端架构上加第二个快手号，发布**不同类型**的作品，
与现有 A 号（搞笑动画）人设、素材、话题池完全分离。

## 方案选型

**采用「平台别名」：把 2 号注册成独立平台 `kuaishou2`。**

| | 平台别名（选定） | account 字段重构 |
|---|---|---|
| DB 迁移 | 不需要（`publish_jobs.platform` 直接存 `kuaishou2`） | `publish_jobs`/`tasks` 加列 + 回填 |
| 代码量 | 约 1 天 | 约 3-5 天 |
| 现有机制复用 | 日限闸门、文案 profile、claim/报告、登录，全部按平台维度现成 | 全部要加账号维度 |
| 2-3 个号 | 够用 | 过度设计 |
| 5 个号以上 | 命名开始难看 | 到时再重构 |

发布注册表（`bot/publish/__init__.py`）每个平台本来就带独立 `state_path`，
且 `kuaishou.login_interactive/publish` 均接受 `state_path` 参数——
`kuaishou2` 复用同一套发布实现，只换登录态文件。

## 内容分离（成败关键）

| | A 号 kuaishou | B 号 kuaishou2 |
|---|---|---|
| 定位 | 搞笑动画（现状） | **待定**，建议萌宠治愈或剧情解说（与 A 互补、有验证过的流量池） |
| 素材 | 现有 IG/YouTube 搞笑动画源 | 不同频道/关键词，**绝不复用 A 已发素材** |
| 标题风格 | 短句笑点钩子 | 独立 profile，治愈系/悬念系 |
| 话题 | 无声视频 + 搞笑系 | 独立锚点（如 无声治愈 + 治愈系组合） |
| 星火任务 | 现有收藏池 | B 号 App 里单独收藏 |
| 日限 | 5 条/天 | 3-5 条/天（冷启动期建议 3） |

同素材两号都发 = 搬运矩阵判定信号，是最大封号风险，必须靠发现层路由保证互斥。

## 代码改动清单（阶段 1，约 1 天）

1. **`bot/publish/__init__.py`**：注册 `kuaishou2`（display_name 快手2号，
   `state_path=ks2_state.json`，login/publish 复用 `kuaishou` 模块函数）。
2. **`bot/publish/kuaishou.py`**：星火轮换 cursor 与任务池缓存路径从全局常量
   改为按 `state_path` 派生（`ks_state.json`→`ks_spark_rr.json`，
   `ks2_state.json`→`ks2_spark_rr.json`），两号收藏池互不污染。
3. **`bot/ai/copywriter.py`**：`PLATFORM_PROFILES` 加 `kuaishou2`，
   tone/话题锚按 B 号定位；`_validate` 的标题规范同 A（短句钩子）。
4. **`bot/publish_worker.py`**：`process_platform` 里 `platform == "kuaishou"`
   的星火特判扩为 `in ("kuaishou", "kuaishou2")`；`maybe_refresh_stats`
   遍历所有启用的快手别名分别抓取。
5. **`bot/monitor/stats.py`**：`persist()` 匹配 tasks 时 join `publish_jobs`
   按 platform 过滤——现状不看平台，双号同时发布会串数据（必改）。
6. **`bot/monitor/spark_income.py`**：`fetch_income/record` 接受 state_path，
   快照文件按号分（`ks_income_snapshots.json` / `ks2_...`），
   worker 定时任务两号各抓一份。
7. **`bot/source/telegram_listener.py`**：`/login` 平台白名单加 `kuaishou2`；
   审核卡片平台按钮加「快手2号」（`db.approve(target_platforms=...)` 已支持）。
8. **`config.yaml`**：`platforms.kuaishou2`（enabled、daily_limit、spark_task）。
9. **测试**：别名注册、双号路径派生、persist 平台过滤、copywriter 双 profile。

已知取舍：`hot_tags` 话题榜初版两号共用（都在验证话题池，可接受），
进阶按 platform 分榜列为后续。

## 上线流程（阶段 2-3）

人工准备（可与开发并行）：
1. 注册 B 号、实名；**养号 3-7 天**（每天刷目标类型内容、点赞关注，
   别注册即发——新号冷启动直接发会被压池）。
2. B 号快手 App 收藏若干星火任务（挑与定位匹配的）。
3. 敲定 B 号定位和素材源关键词。

上线步骤：
1. 部署阶段 1 代码，`TG 发 /login kuaishou2` 扫码。
2. 冒烟：TG 审核卡片手动指定 2-3 条任务到快手2号，观察发布、星火挂载、
   收益快照各自独立。
3. 发现层路由：`config.yaml` 给 B 号的源（yt_search 关键词/频道）默认
   `target_platforms: [kuaishou2]`，A 号源默认 `[kuaishou]`，互斥。
4. B 号日限先 3 条，垂直发 2 周让算法打标，再考虑提量。

## 风险与对策

| 风险 | 评估 | 对策 |
|---|---|---|
| 同 IP 双号关联 | 低（矩阵号常规；阿里云固定 IP） | 素材/话题/星火严格分离即可，暂不动指纹 |
| 素材重叠 → 搬运矩阵判定 | **高**（若混用素材） | 发现层路由互斥 + 发布前 sha 查重可加 |
| B 号新号冷启动播放低 | 预期内 | 前 2 周不看数据，只看发布成功率与账号健康 |
| 一号被风控连坐 | 中 | 登录态/收益/星火全部按号隔离文件，故障半径天然隔离 |
