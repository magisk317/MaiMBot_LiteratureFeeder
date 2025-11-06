# MaiMBot Literature Feeder

> 学术文献聚合与推送插件

Literature Feeder 会从多个主流来源抓取最新论文、进行去重整理，并按配置定时或实时推送到麦麦的指定会话。插件还提供手动预览、关键词搜索、来源过滤与强制推送等能力，方便在群聊或私聊中快速获取高价值学术资讯。

## 核心能力

- **多源聚合**：支持 arXiv、RSS 等多种学术渠道，并可自行扩展来源列表。
- **智能去重与摘要**：缓存已推送条目，自动格式化标题、作者、摘要与链接。
- **灵活调度**：既可设定固定时间（如 09:00、18:30），也可按分钟间隔轮询推送。
- **命令交互**：
  - `/lit preview [--source]` 查看推送计划
  - `/lit search <关键词> [--source]` 搜索匹配文献
  - `/lit force [--source]` 强制推送最新条目（需在配置中启用）

## 目录结构

```
MaiMBot_LiteratureFeeder/
├── _manifest.json          # 插件元信息
├── config.toml             # 默认配置（可自定义）
├── config.template.toml    # 配置模板
├── plugin.py               # 插件主入口
├── requirements.txt        # 运行依赖
├── README.md               # 使用说明
├── __init__.py             # 标记为 Python 包
└── data/                   # 运行期缓存目录
```

## 快速开始

1. 将插件目录放置于 `data/MaiMBot/plugins/` 下。
2. 根据 `config.template.toml` 调整 `config.toml`，至少配置推送目标与数据源。
3. 在麦麦运行环境中安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
4. 重启 MaiMBot 使插件加载。
5. 使用 `/lit preview` 验证；根据需要执行 `/lit search ...` 或 `/lit force`。

## 调度模式

- **间隔模式**：`scheduler.interval_minutes` > 0 时按固定间隔推送。
- **定时模式**：`scheduler.specific_times = ["09:30","18:00"]` 可设定北京时间的固定触发点。
- **双模式并行**：同时配置时会按最小检查间隔运行；`scheduler.check_interval_minutes` 控制定时模式轮询频率。
- `[debug]` 可开启 `/lit force` 调试命令，并限制允许使用的账号。

## 命令与参数

- `/lit preview [--arxiv] [--nature]`：预览推送计划并按来源过滤。
- `/lit search machine learning --arxiv`：搜索包含关键词的条目，支持多来源筛选。
- `/lit force [--arxiv]`：忽略去重立即推送最新文献，需在配置中开启 `enable_force_command`。

## 后续扩展方向

- 接入更多数据源（CrossRef、Semantic Scholar 等）。
- 按学科、年份等维度加强筛选与排序。
- 推送格式适配更多渠道（如频道、邮件、Webhook）。

欢迎提交 Issue 或 PR，共同完善麦麦的学术情报系统。

