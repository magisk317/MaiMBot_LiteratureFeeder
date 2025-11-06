# MaiMBot Literature Feeder

> 学术文献聚合与推送插件（开发脚手架）

本仓库基于 [MaiMBot 插件开发快速入门](https://docs.mai-mai.org/develop/plugin_develop/quick-start.html) 的推荐结构，提供一个 **学术文献推送插件** 的基础框架，便于后续补全数据抓取、摘要生成和消息推送能力。

## 目标功能

- 聚合多源学术资讯（arXiv、RSS、定制接口等）
- 缓存与去重，防止重复推送
- LLM/模板驱动的摘要输出
- 定时推送到指定群聊或私聊
- 命令式手动预览（`/lit preview`）与强制推送（`/lit force`）工具

当前仓库仅完成插件骨架与配置结构，核心业务逻辑需按实际需求补充。

## 目录结构

```
MaiMBot_LiteratureFeeder/
├── _manifest.json          # 插件元信息
├── config.toml             # 默认配置
├── plugin.py               # 插件主入口（含事件、命令骨架）
├── README.md               # 本说明
├── requirements.txt        # 额外依赖（可选安装）
├── __init__.py             # 保持为 Python 包
└── data/                   # 缓存/中间数据目录（运行时自动创建）
```

## 快速开始

1. 将插件目录放置于 `data/MaiMBot/plugins/` 下（已通过命令完成）。
2. 根据需要编辑 `config.toml`，至少配置推送目标与数据源。
3. 若需要联网抓取，请在麦麦运行环境中安装 `requirements.txt` 列出的依赖：
   ```bash
   pip install -r requirements.txt
   ```
4. 重启 MaiMBot，使插件被加载。
5. 使用命令 `/lit preview` 验证插件注册是否成功；若已在配置中开启调试命令，还可以通过 `/lit force` 立即推送一次最新数据（忽略去重缓存）。

### 调度模式

- **间隔模式**：`scheduler.interval_minutes` > 0 时，按固定间隔推送。
- **定时模式**：在 `scheduler.specific_times` 写入 `["09:30", "18:00"]` 等时间点（默认使用 `scheduler.timezone`，推荐 `Asia/Shanghai`），插件会在指定分钟触发。
- 如需同时使用两种模式，调度器会按照最小的检查间隔运行；若仅定时模式生效，可调 `scheduler.check_interval_minutes` 控制轮询频率。
- 调试命令默认关闭，可在 `[debug]` 下开启，并按需限制 `allow_user_ids`。

## 待补全事项

- [ ] 实现真实的数据源抓取器（arXiv API、RSS、第三方接口等）
- [ ] 增加持久化缓存与去重逻辑
- [ ] 结合 LLM 摘要或模板化输出
- [ ] 支持多渠道投递（群聊/私聊/频道）
- [ ] 补充测试与监控指标

欢迎基于此脚手架继续拓展，打造属于麦麦的学术情报源。PR 和 Issue 都非常欢迎！
