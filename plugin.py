"""
MaiMBot Literature Feeder plugin - skeleton implementation.

The goal of this plugin is to aggregate academic literature updates
and push curated highlights to configured channels.  This starter file
sets up the core scaffolding so future development can focus on data
collection, summarisation, and delivery logic.
"""

from __future__ import annotations

import asyncio
import json
import re
import textwrap
import time
from contextlib import suppress
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import httpx
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from src.plugin_system import (
    BasePlugin,
    BaseCommand,
    BaseEventHandler,
    ConfigField,
    EventType,
    MaiMessages,
    CustomEventHandlerResult,
    register_plugin,
    get_logger,
)
from src.plugin_system.apis import chat_api, send_api

try:  # pragma: no cover - optional runtime dependency
    from src.chat.message_receive.chat_stream import get_chat_manager
except Exception:  # pragma: no cover - best effort import
    get_chat_manager = None

logger = get_logger("maimbot_literature_feeder")

CONFIG_VERSION = "0.4.0"


def _extract_source_flags(message) -> List[str]:
    """Parse command text for --source flags."""
    text = ""
    if hasattr(message, "processed_plain_text"):
        text = message.processed_plain_text or ""
    if not text and hasattr(message, "raw_message") and isinstance(message.raw_message, str):
        text = message.raw_message
    flags: List[str] = []
    for token in text.strip().split():
        if token.startswith("--") and len(token) > 2:
            flags.append(token[2:].lower())
    return flags


def _resolve_plugin_instance() -> Optional["LiteratureFeederPlugin"]:
    """Fetch plugin instance from plugin manager as a fallback."""
    try:
        from src.plugin_system.core.plugin_manager import plugin_manager
    except Exception:
        return None
    try:
        return plugin_manager.get_plugin_instance("maimbot_literature_feeder")
    except Exception:
        return None


class LiteraturePreviewCommand(BaseCommand):
    """Manual command: preview the next scheduled delivery."""

    command_name = "literature_preview"
    command_description = "预览下一轮推送的文献摘要"
    command_pattern = r"^(?:[/#])?(?:lit|litfeed)(?:\s+preview)?(?:\s+--[^\s]+)*$"
    command_help = "使用 /lit preview 预览下一轮文献推送"
    intercept_message = True

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """Send a placeholder preview until the fetch pipeline is in place."""
        plugin = getattr(self, "plugin", None)
        if plugin is None:
            plugin = _resolve_plugin_instance()
        if plugin is not None and hasattr(plugin, "build_preview"):
            source_flags = _extract_source_flags(self.message)
            preview_text = await plugin.build_preview(manual_trigger=True, source_flags=source_flags)
        else:
            preview_text = None

        message = preview_text or "📚 文献推送插件就绪，可随时查看配置与来源情况。"
        await self.send_text(message)
        return True, "preview_sent", True


class LiteratureForceDispatchCommand(BaseCommand):
    """Manual command: force a dispatch regardless of seen cache."""

    command_name = "literature_force_dispatch"
    command_description = "强制触发一次文献推送（忽略去重缓存）"
    command_pattern = r"^(?:[/#])?(?:lit|litfeed)\s+(?:force|dispatch)(?:\s+--[^\s]+)*$"
    command_help = "使用 /lit force 强制推送一次最新文献"
    intercept_message = True

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        plugin = getattr(self, "plugin", None)
        logger.debug(
            "[ForceCommand] received request, plugin=%s, config_debug=%s",
            type(plugin).__name__ if plugin else None,
            self.get_config("debug.enable_force_command", False),
        )
        if plugin is None:
            plugin = _resolve_plugin_instance()
        if not plugin:
            await self.send_text("❌ 插件实例未加载，无法执行。")
            return False, "plugin_missing", True

        if not plugin.get_config("debug.enable_force_command", False):
            await self.send_text("❌ 未启用调试命令。")
            return False, "force_disabled", True

        allow_ids = set(str(uid) for uid in plugin.get_config("debug.allow_user_ids", []) or [])
        sender_id = ""
        try:
            sender_id = str(self.message.sender.user_id)  # type: ignore[attr-defined]
        except Exception:
            sender_id = ""

        if allow_ids and sender_id and sender_id not in allow_ids:
            await self.send_text("❌ 你没有权限使用该调试命令。")
            return False, "not_authorized", True

        await self.send_text("⏱️ 正在抓取文献并强制推送，请稍候…")
        source_flags = _extract_source_flags(self.message)
        success, detail = await plugin.manual_dispatch(
            ignore_seen=True,
            reason="manual",
            source_flags=source_flags,
        )
        logger.debug(
            "[ForceCommand] executed manual dispatch result=%s detail=%s flags=%s",
            success,
            detail,
            source_flags,
        )
        if success:
            await self.send_text(f"✅ 强制推送完成：{detail}")
            return True, "force_sent", True

        await self.send_text(f"⚠️ 强制推送未发送：{detail}")
        return False, "force_not_sent", True


class LiteratureSearchCommand(BaseCommand):
    """Manual command: search literature by keywords."""

    command_name = "literature_search"
    command_description = "基于关键词搜索学术文献"
    command_pattern = r"^(?:[/#])?(?:lit|litfeed)\s+search\b.*$"
    command_help = "使用 /lit search 关键词 [--source] 执行搜索"
    intercept_message = True

    def _extract_keywords(self) -> List[str]:
        text = ""
        if hasattr(self.message, "processed_plain_text"):
            text = self.message.processed_plain_text or ""
        if not text and hasattr(self.message, "raw_message") and isinstance(self.message.raw_message, str):
            text = self.message.raw_message

        tokens = [tok for tok in text.strip().split() if tok]
        if not tokens:
            return []

        try:
            search_index = next(i for i, tok in enumerate(tokens) if tok.lower() == "search")
        except StopIteration:
            return []

        keywords = []
        for token in tokens[search_index + 1 :]:
            if token.startswith("--"):
                continue
            keywords.append(token)
        return keywords

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        plugin = getattr(self, "plugin", None)
        if plugin is None:
            plugin = _resolve_plugin_instance()
        if not plugin:
            await self.send_text("❌ 插件实例未加载，无法执行。")
            return False, "plugin_missing", True

        keywords = self._extract_keywords()
        if not keywords:
            await self.send_text("❌ 请提供搜索关键词，例如 /lit search machine learning")
            return False, "missing_keywords", True

        source_flags = _extract_source_flags(self.message)
        success, message = await plugin.search_literature(keywords, source_flags)
        await self.send_text(message)
        return success, ("ok" if success else "no_results"), True


class LiteratureSchedulerEventHandler(BaseEventHandler):
    """Kick off the background scheduler once MaiMBot is ready."""

    event_type = EventType.ON_START
    handler_name = "literature_scheduler"
    handler_description = "启动学术文献推送的定时调度"
    weight = 50
    intercept_message = False

    async def execute(
        self, message: Optional[MaiMessages]
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        """Defer the scheduler bootstrap to the plugin instance."""
        try:
            from src.plugin_system.core.plugin_manager import plugin_manager

            plugin = plugin_manager.get_plugin_instance("maimbot_literature_feeder")
            if not plugin:
                logger.error("未找到 Literature Feeder 插件实例")
                return False, True, None, None, None

            await plugin.ensure_scheduler_started()
            return True, True, None, None, None
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(f"启动文献调度器失败: {exc}", exc_info=True)
            return False, True, None, None, None


@register_plugin
class LiteratureFeederPlugin(BasePlugin):
    """Main entry point for the literature feeder plugin."""

    plugin_name = "maimbot_literature_feeder"
    enable_plugin = True
    dependencies: List[str] = []
    python_dependencies: List[str] = [
        "httpx>=0.26.0",
        "feedparser>=6.0.10",
        "beautifulsoup4>=4.12.0",
    ]
    config_file_name = "config.toml"

    config_section_descriptions: Dict[str, str] = {
        "plugin": "插件基础开关与版本",
        "scheduler": "推送调度设置",
        "sources": "文献来源配置",
        "delivery": "推送目标与格式设置",
        "search": "搜索与调试设置",
        "debug": "调试与开发配置",
    }

    config_schema: Dict[str, Dict[str, ConfigField]] = {
        "plugin": {
            "_section_description": (
                "# Literature Feeder 配置\n"
                "# 负责聚合学术更新并推送到麦麦指定会话\n"
            ),
            "enabled": ConfigField(bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(str, default=CONFIG_VERSION, description="配置文件版本"),
            "debug": ConfigField(bool, default=False, description="调试模式，输出详细日志"),
        },
        "scheduler": {
            "_section_description": "\n# 调度设置",
            "enable": ConfigField(bool, default=True, description="定时推送开关"),
            "interval_minutes": ConfigField(int, default=120, description="按固定间隔推送的周期（分钟，<=0 表示关闭）"),
            "startup_delay_seconds": ConfigField(int, default=10, description="启动初始延迟（秒）"),
            "max_batch_size": ConfigField(int, default=5, description="单次推送的文献数量上限"),
            "specific_times": ConfigField(list, default=[], description="每日固定时间推送（北京时间，24小时制，如 \"16:00\"）"),
            "timezone": ConfigField(str, default="Asia/Shanghai", description="定时推送使用的时区"),
            "check_interval_minutes": ConfigField(int, default=1, description="调度器检查间隔（分钟），针对 fixed time 模式"),
            "quiet_hours": ConfigField(list, default=["01:00-08:00"], description="静默时间段，避免在此时间范围内推送，可配置多个区间"),
            "on_the_hour": ConfigField(bool, default=False, description="是否在每个整点触发一次推送"),
        },
        "sources": {
            "_section_description": "\n# 数据源设置",
            "feeds": ConfigField(
                list,
                default=[
                    {
                        "type": "arxiv",
                        "query": "cs.CL",
                        "max_items": 5,
                        "label": "arXiv: Computation and Language",
                    }
                ],
                description="文献源列表，可包含 arxiv/rss/custom 类型",
            ),
            "http_timeout": ConfigField(int, default=20, description="抓取来源的网络超时（秒）"),
            "cache_hours": ConfigField(int, default=6, description="缓存有效期（小时）"),
        },
        "delivery": {
            "_section_description": "\n# 推送目标与格式",
            "targets": ConfigField(
                list,
                default=[],
                description="推送目标列表，例如 ['group:123456', 'private:987654']",
            ),
            "fallback_target": ConfigField(
                str,
                default="",
                description="备用推送目标（可选，格式同 targets 项）",
            ),
            "include_tags": ConfigField(
                bool,
                default=True,
                description="在推送中附加分类标签或来源信息",
            ),
            "summary_style": ConfigField(
                str,
                default="bullet",
                description="摘要风格：plain/bullet/llm",
            ),
            "llm_model": ConfigField(
                str,
                default="",
                description="可选的LLM摘要模型（OpenAI兼容格式）",
            ),
        },
        "search": {
            "_section_description": "\n# 搜索相关设置",
            "max_results": ConfigField(int, default=5, description="搜索返回的最大条目数"),
            "match_title": ConfigField(bool, default=True, description="是否匹配标题"),
            "match_summary": ConfigField(bool, default=True, description="是否匹配摘要"),
            "match_authors": ConfigField(bool, default=False, description="是否匹配作者信息"),
        },
        "debug": {
            "_section_description": "\n# 调试设置",
            "enable_force_command": ConfigField(bool, default=False, description="是否启用强制推送调试命令 /lit force"),
            "allow_user_ids": ConfigField(list, default=[], description="允许执行调试命令的用户ID列表（留空则表示不限制）"),
        },
    }

    def __init__(self, plugin_dir: str, **kwargs: Any):
        super().__init__(plugin_dir, **kwargs)
        self._scheduler_task: Optional[asyncio.Task] = None
        self._scheduler_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._stop_event.set()  # scheduler idle until explicitly started

        self.plugin_path = Path(plugin_dir)
        self.data_dir = self.plugin_path / "data"
        self.data_dir.mkdir(exist_ok=True)

        self.seen_cache_path = self.data_dir / "seen_items.json"
        self.seen_ids: set[str] = set()
        self._load_seen_cache()
        self._llm_warning_emitted = False
        self._last_interval_run_ts: float = 0.0
        self._last_fixed_run_key: Optional[str] = None
        self._last_hour_run_key: Optional[str] = None

        tz_name = str(self.get_config("scheduler.timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            self._timezone = ZoneInfo(tz_name)
        except Exception:
            logger.warning("无法解析时区 %s，改用 UTC", tz_name)
            self._timezone = ZoneInfo("UTC")

        logger.info("MaiMBot Literature Feeder 插件框架已初始化")

    def get_plugin_components(self) -> List[Tuple[Any, type]]:
        """Expose plugin components to the host application."""
        components: List[Tuple[Any, type]] = []
        if not self.get_config("plugin.enabled", True):
            return components

        if self.get_config("scheduler.enable", True):
            components.append(
                (LiteratureSchedulerEventHandler.get_handler_info(), LiteratureSchedulerEventHandler)
            )
        components.append((LiteraturePreviewCommand.get_command_info(), LiteraturePreviewCommand))
        components.append((LiteratureSearchCommand.get_command_info(), LiteratureSearchCommand))
        if self.get_config("debug.enable_force_command", False):
            components.append((LiteratureForceDispatchCommand.get_command_info(), LiteratureForceDispatchCommand))
        return components

    async def ensure_scheduler_started(self) -> None:
        """Start the background loop if configuration allows it."""
        async with self._scheduler_lock:
            if self._scheduler_task and not self._scheduler_task.done():
                return

            if not self.get_config("plugin.enabled", True):
                logger.info("Literature Feeder 插件未启用，跳过调度器启动")
                return

            if not self.get_config("scheduler.enable", True):
                logger.info("调度器被配置关闭，跳过启动")
                return

            specific_times = self._parse_specific_times()
            interval_cfg = int(self.get_config("scheduler.interval_minutes", 120))
            if interval_cfg <= 0 and not specific_times:
                logger.info("未配置定时任务（interval<=0 且 specific_times 为空），跳过启动")
                return

            check_interval_cfg = max(1, int(self.get_config("scheduler.check_interval_minutes", 1)))
            if specific_times:
                loop_interval = check_interval_cfg
                if interval_cfg > 0:
                    loop_interval = min(loop_interval, interval_cfg)
            else:
                loop_interval = max(1, interval_cfg)

            self._stop_event.clear()
            delay = max(0, int(self.get_config("scheduler.startup_delay_seconds", 10)))

            async def runner():
                try:
                    if delay:
                        await asyncio.sleep(delay)
                    while not self._stop_event.is_set():
                        await self.dispatch_cycle()
                        try:
                            await asyncio.wait_for(
                                self._stop_event.wait(), timeout=loop_interval * 60
                            )
                        except asyncio.TimeoutError:
                            continue
                except asyncio.CancelledError:  # pragma: no cover - lifecycle
                    logger.debug("文献推送调度器收到取消信号")
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error(f"调度器运行异常: {exc}", exc_info=True)
                finally:
                    self._stop_event.set()

            self._scheduler_task = asyncio.create_task(runner(), name="literature_feeder_scheduler")
            logger.info("文献推送调度器已启动（检查间隔 %s 分钟）", loop_interval)

    async def stop_scheduler(self) -> None:
        """Cancel the running scheduler, if any."""
        async with self._scheduler_lock:
            if not self._scheduler_task:
                return

            self._stop_event.set()
            self._scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._scheduler_task
            self._scheduler_task = None
            logger.info("文献推送调度器已停止")

    async def dispatch_cycle(self) -> None:
        """
        Single iteration of the scheduler.

        Steps:
        1. Collect new items from configured sources
        2. Deduplicate against local cache
        3. Summarise / format content
        4. Deliver to configured chats
        """
        try:
            trigger_reason = self._check_schedule_trigger()
            if not trigger_reason:
                return

            success, detail = await self._execute_dispatch(
                ignore_seen=False,
                reason=trigger_reason,
                source_flags=None,
            )
            if success:
                logger.info("成功推送文献更新（%s）: %s", trigger_reason, detail)
            else:
                logger.debug("调度触发（%s）但未发送：%s", trigger_reason, detail)
        except Exception as exc:  # pragma: no cover - defensive
            if self.get_config("plugin.debug", False):
                logger.exception("调度器循环失败: %s", exc)
            else:
                logger.error(f"调度器循环失败: {exc}")

    async def manual_dispatch(
        self,
        *,
        ignore_seen: bool,
        reason: str,
        source_flags: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Manual dispatch for commands/tests."""
        return await self._execute_dispatch(
            ignore_seen=ignore_seen,
            reason=reason,
            source_flags=source_flags,
        )

    def _check_schedule_trigger(self) -> Optional[str]:
        """Determine whether the scheduler should fire this cycle."""
        if not self.get_config("scheduler.enable", True):
            return None

        now = self._now()
        now_ts = now.timestamp()

        interval_minutes = int(self.get_config("scheduler.interval_minutes", 120))
        specific_times = self._parse_specific_times()
        every_hour = bool(self.get_config("scheduler.on_the_hour", False))

        triggered_by_interval = False
        triggered_by_fixed_time = False
        triggered_by_hourly = False

        if self._in_quiet_hours(now):
            logger.debug("当前处于静默时间段，跳过调度触发")
            return None

        if interval_minutes > 0:
            if now_ts - self._last_interval_run_ts >= interval_minutes * 60:
                triggered_by_interval = True
                self._last_interval_run_ts = now_ts

        if specific_times:
            current_minute = now.hour * 60 + now.minute
            current_key = now.strftime("%Y%m%d%H%M")
            if current_minute in specific_times and current_key != self._last_fixed_run_key:
                triggered_by_fixed_time = True
                self._last_fixed_run_key = current_key

        if every_hour and now.minute == 0:
            current_hour_key = now.strftime("%Y%m%d%H")
            if current_hour_key != self._last_hour_run_key:
                triggered_by_hourly = True
                self._last_hour_run_key = current_hour_key

        if triggered_by_fixed_time:
            return "fixed"
        if triggered_by_hourly:
            return "hourly"
        if triggered_by_interval:
            return "interval"
        return None

    def _parse_specific_times(self) -> List[int]:
        """Parse HH:MM strings into minutes after midnight."""
        specific_times_cfg = self.get_config("scheduler.specific_times", []) or []
        parsed: List[int] = []
        for entry in specific_times_cfg:
            if not entry:
                continue
            value = str(entry).strip()
            try:
                hour_str, minute_str = value.split(":")
                hour = int(hour_str)
                minute = int(minute_str)
                if 0 <= hour < 24 and 0 <= minute < 60:
                    parsed.append(hour * 60 + minute)
                else:
                    raise ValueError
            except Exception:
                logger.warning("忽略无效的定时配置: %s", entry)
        return sorted(set(parsed))

    def _parse_quiet_hours(self) -> List[tuple[int, int]]:
        """Parse quiet hour ranges into start/end minute tuples."""
        quiet_cfg = self.get_config("scheduler.quiet_hours", []) or []
        ranges: List[tuple[int, int]] = []
        for entry in quiet_cfg:
            if not entry:
                continue
            value = str(entry).strip()
            try:
                start_str, end_str = value.split("-")
                sh, sm = map(int, start_str.split(":"))
                eh, em = map(int, end_str.split(":"))
                if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
                    raise ValueError
                start = sh * 60 + sm
                end = eh * 60 + em
                ranges.append((start, end))
            except Exception:
                logger.warning("忽略无效的静默时间段配置: %s", entry)
        return ranges

    def _in_quiet_hours(self, now: datetime) -> bool:
        """Return True if current time falls into any configured quiet range."""
        ranges = self._parse_quiet_hours()
        if not ranges:
            return False
        current_minute = now.hour * 60 + now.minute
        for start, end in ranges:
            if start == end:
                continue
            if start < end:
                if start <= current_minute < end:
                    return True
            else:
                if current_minute >= start or current_minute < end:
                    return True
        return False

    def _now(self) -> datetime:
        """Return current time in configured timezone."""
        return datetime.now(self._timezone)

    async def _execute_dispatch(
        self,
        *,
        ignore_seen: bool,
        reason: str,
        source_flags: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Fetch, format, and deliver a batch of literature updates."""
        sources_cfg = self._select_sources(source_flags)
        if not sources_cfg:
            if source_flags:
                flags_text = ", ".join(source_flags)
                return False, f"未找到匹配的数据源：{flags_text}"
            return False, "未配置文献来源"

        targets = list(self.get_config("delivery.targets", []) or [])
        fallback_target = self.get_config("delivery.fallback_target", "")
        if not targets and fallback_target:
            targets.append(fallback_target)

        if not targets:
            return False, "未配置推送目标"

        batch_size = max(1, int(self.get_config("scheduler.max_batch_size", 5)))
        include_tags = bool(self.get_config("delivery.include_tags", True))
        summary_style = str(self.get_config("delivery.summary_style", "bullet")).lower()

        collected_items = await self._collect_from_sources(sources_cfg)
        if not collected_items:
            return False, "未从任何来源获取到文献条目"

        if ignore_seen:
            selected_items = collected_items[:batch_size]
        else:
            fresh_items = [item for item in collected_items if item["uid"] not in self.seen_ids]
            if not fresh_items:
                return False, "无新的文献条目"
            selected_items = fresh_items[:batch_size]

        if not selected_items:
            return False, "选取条目为空"

        message = self._format_batch(
            selected_items,
            include_tags=include_tags,
            summary_style=summary_style,
        )

        await self._send_to_targets(message, targets)

        for item in selected_items:
            self.seen_ids.add(item["uid"])

        await self._persist_seen_cache()
        return True, f"推送 {len(selected_items)} 条到 {len(targets)} 个目标（{reason}）"

    async def search_literature(
        self,
        keywords: List[str],
        source_flags: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Search configured sources by keywords."""
        keywords_clean = [kw.strip() for kw in keywords if kw.strip()]
        if not keywords_clean:
            return False, "❌ 未提供有效的搜索关键词。"

        sources_cfg = self._select_sources(source_flags)
        if not sources_cfg:
            if source_flags:
                return False, f"⚠️ 未找到匹配的数据源：{', '.join(source_flags)}"
            return False, "⚠️ 未配置可用的数据源。"

        items = await self._collect_from_sources(sources_cfg)
        if not items:
            return False, "⚠️ 未从指定来源获取到任何条目。"

        max_results = max(1, int(self.get_config("search.max_results", 5)))
        match_title = bool(self.get_config("search.match_title", True))
        match_summary = bool(self.get_config("search.match_summary", True))
        match_authors = bool(self.get_config("search.match_authors", False))

        keywords_lower = [kw.lower() for kw in keywords_clean]

        def matches(entry: Dict[str, Any]) -> bool:
            texts: List[str] = []
            if match_title:
                texts.append(entry.get("title", ""))
            if match_summary:
                texts.append(entry.get("summary", ""))
            if match_authors:
                texts.append(entry.get("authors", ""))
            if not texts:
                texts.append(entry.get("title", ""))
            combined = " ".join(texts).lower()
            return all(kw in combined for kw in keywords_lower)

        filtered = [entry for entry in items if matches(entry)]
        if not filtered:
            return False, "⚠️ 未找到匹配的文献，请尝试调整关键词。"

        limited = filtered[:max_results]
        message = self._format_search_results(
            limited,
            keywords_clean,
            include_tags=bool(self.get_config("delivery.include_tags", True)),
            summary_style=str(self.get_config("delivery.summary_style", "bullet")).lower(),
            source_flags=source_flags,
        )
        return True, message

    def _select_sources(self, flags: Optional[List[str]]) -> List[Dict[str, Any]]:
        """Select configured sources matching the provided flags."""
        sources = self.get_config("sources.feeds", []) or []
        if not flags:
            return list(sources)

        normalized = [flag.lower() for flag in flags if flag]
        if not normalized:
            return list(sources)

        selected: List[Dict[str, Any]] = []
        for feed in sources:
            tags = set()
            feed_type = str(feed.get("type", "")).lower()
            if feed_type:
                tags.add(feed_type)
                tags.add(feed_type.replace("_", "-"))

            label = feed.get("label")
            if label:
                lower_label = str(label).lower()
                tags.add(lower_label)
                for part in re.split(r"[^a-z0-9]+", lower_label):
                    if part:
                        tags.add(part)

            name = feed.get("name")
            if name:
                lower_name = str(name).lower()
                tags.add(lower_name)
                for part in re.split(r"[^a-z0-9]+", lower_name):
                    if part:
                        tags.add(part)

            key = feed.get("key")
            if key:
                tags.add(str(key).lower())

            slug = feed.get("slug")
            if slug:
                tags.add(str(slug).lower())

            if any(flag in tags for flag in normalized):
                selected.append(feed)

        return selected

    async def _collect_from_sources(self, feeds: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Fetch and normalise entries from all configured sources."""
        feeds_to_use = feeds if feeds is not None else (self.get_config("sources.feeds", []) or [])
        if not feeds_to_use:
            return []

        timeout_seconds = max(1, int(self.get_config("sources.http_timeout", 20)))
        results: List[Dict[str, Any]] = []
        timeout = httpx.Timeout(timeout_seconds, connect=timeout_seconds)

        async with httpx.AsyncClient(timeout=timeout) as client:
            for feed_cfg in feeds_to_use:
                feed_type = str(feed_cfg.get("type", "rss")).lower()
                try:
                    if feed_type == "arxiv":
                        entries = await self._fetch_arxiv(client, feed_cfg)
                    elif feed_type == "rss":
                        entries = await self._fetch_rss(client, feed_cfg)
                    else:
                        logger.warning("未知的数据源类型 %s，已跳过", feed_type)
                        continue
                    results.extend(entries)
                except Exception as exc:
                    label = feed_cfg.get("label") or feed_cfg.get("url") or feed_cfg.get("query") or feed_type
                    log_fn = logger.warning if isinstance(exc, (httpx.HTTPError, httpx.TimeoutException)) else logger.error
                    if self.get_config("plugin.debug", False):
                        logger.exception("获取数据源 %s 失败: %s", label, exc)
                    else:
                        log_fn("获取数据源 %s 失败: %s", label, exc)

        results.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
        return results

    async def _fetch_arxiv(self, client: httpx.AsyncClient, feed_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch entries from arXiv."""
        query = feed_cfg.get("query") or feed_cfg.get("q") or "all"
        max_items = int(feed_cfg.get("max_items", 5))
        label = feed_cfg.get("label") or f"arXiv: {query}"

        params = {
            "search_query": query,
            "start": 0,
            "max_results": max(1, max_items),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        headers = {
            "User-Agent": "MaiMBot-LiteratureFeeder/0.1 (+https://github.com/magisk317/MaiMBot_LiteratureFeeder)"
        }
        response = await client.get("https://export.arxiv.org/api/query", params=params, headers=headers)
        response.raise_for_status()

        parsed = feedparser.parse(response.text)
        entries: List[Dict[str, Any]] = []

        for entry in parsed.entries[:max_items]:
            uid = entry.get("id") or entry.get("link") or entry.get("title")
            if not uid:
                continue

            summary = self._clean_summary(entry.get("summary", ""))
            authors = self._extract_authors(entry)
            link = entry.get("link") or uid

            entries.append(
                {
                    "uid": str(uid),
                    "title": entry.get("title", "").strip(),
                    "link": link.strip(),
                    "summary": summary,
                    "authors": authors,
                    "label": label,
                    "timestamp": self._entry_timestamp(entry),
                }
            )

        return entries

    async def _fetch_rss(self, client: httpx.AsyncClient, feed_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch entries from an RSS/Atom feed."""
        url = feed_cfg.get("url")
        if not url:
            raise ValueError("RSS 类型数据源缺少 url 字段")

        max_items = int(feed_cfg.get("max_items", 5))
        label = feed_cfg.get("label") or url
        headers = {
            "User-Agent": "MaiMBot-LiteratureFeeder/0.1",
            "Accept": "application/rss+xml, application/atom+xml;q=0.9, application/xml;q=0.8, */*;q=0.5",
        }

        response = await client.get(url, headers=headers)
        response.raise_for_status()

        parsed = feedparser.parse(response.text)
        entries: List[Dict[str, Any]] = []

        for entry in parsed.entries[:max_items]:
            uid = entry.get("id") or entry.get("link") or entry.get("title")
            if not uid:
                continue

            summary = self._clean_summary(entry.get("summary", entry.get("description", "")))
            authors = self._extract_authors(entry)
            link = entry.get("link") or uid

            entries.append(
                {
                    "uid": str(uid),
                    "title": entry.get("title", "").strip(),
                    "link": link.strip(),
                    "summary": summary,
                    "authors": authors,
                    "label": label,
                    "timestamp": self._entry_timestamp(entry),
                }
            )

        return entries

    @staticmethod
    def _entry_timestamp(entry: Any) -> float:
        """Convert feed entry timestamps to epoch seconds."""
        for key in ("updated_parsed", "published_parsed", "created_parsed"):
            parsed = entry.get(key) if hasattr(entry, "get") else getattr(entry, key, None)
            if parsed:
                try:
                    return time.mktime(parsed)
                except (TypeError, OverflowError):
                    continue

        for key in ("updated", "published", "created"):
            value = entry.get(key) if hasattr(entry, "get") else getattr(entry, key, None)
            if value:
                for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %Z"):
                    with suppress(ValueError):
                        return datetime.strptime(value, fmt).timestamp()

        return time.time()

    @staticmethod
    def _extract_authors(entry: Any) -> str:
        """Extract author names from an entry."""
        authors = []
        candidates = None

        if hasattr(entry, "get"):
            candidates = entry.get("authors") or entry.get("author_detail")
        else:
            candidates = getattr(entry, "authors", None) or getattr(entry, "author_detail", None)

        if isinstance(candidates, list):
            for author in candidates:
                name = None
                if isinstance(author, dict):
                    name = author.get("name")
                else:
                    name = getattr(author, "name", None)
                if name:
                    authors.append(str(name).strip())
        elif isinstance(candidates, dict):
            name = candidates.get("name")
            if name:
                authors.append(str(name).strip())

        if not authors:
            fallback = entry.get("author") if hasattr(entry, "get") else getattr(entry, "author", "")
            if fallback:
                return str(fallback).strip()

        return ", ".join(authors)

    @staticmethod
    def _clean_summary(raw_summary: str, max_length: int = 280) -> str:
        """Strip HTML and trim the summary to a readable length."""
        if not raw_summary:
            return ""
        soup = BeautifulSoup(raw_summary, "html.parser")
        cleaned = soup.get_text(separator=" ", strip=True)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) > max_length:
            cleaned = cleaned[: max_length - 1].rstrip() + "…"
        return cleaned

    def _format_batch(self, batch: List[Dict[str, Any]], *, include_tags: bool, summary_style: str) -> str:
        """Build the outbound message for the selected batch."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines: List[str] = [f"📚 文献速递（{timestamp}）", ""]

        for idx, item in enumerate(batch, 1):
            title = item.get("title") or "未命名条目"
            lines.append(f"{idx}. {title}")

            meta_parts: List[str] = []
            if include_tags and item.get("label"):
                meta_parts.append(str(item["label"]))
            if item.get("authors"):
                meta_parts.append(str(item["authors"]))
            if meta_parts:
                lines.append(f"   来源：{' | '.join(meta_parts)}")

            summary = item.get("summary")
            if summary:
                formatted_summary = self._format_summary(summary, summary_style)
                if formatted_summary:
                    lines.append(f"   摘要：{formatted_summary}")

            link = item.get("link")
            if link:
                lines.append(f"   链接：{link}")

            lines.append("")

        return "\n".join(lines).strip()

    def _format_summary(self, summary: str, style: str) -> str:
        """Format summary text according to the requested style."""
        summary = summary.strip()
        if not summary:
            return ""

        style = style.lower()
        if style == "bullet":
            fragments = [frag.strip() for frag in re.split(r"[。！？.!?]", summary) if frag.strip()]
            if len(fragments) >= 2:
                fragments = fragments[:3]
                bullet_text = "\n     • " + "\n     • ".join(fragments)
                return bullet_text
            return summary
        if style == "llm":
            if not self._llm_warning_emitted:
                logger.warning("LLM 摘要模式尚未实现，暂时使用原始摘要输出")
                self._llm_warning_emitted = True
            return summary

        # Default plain fallback
        wrapped = textwrap.fill(summary, width=70)
        return wrapped

    async def _send_to_targets(self, message: str, targets: List[str]) -> None:
        """Send the formatted message to all configured targets."""
        for target in targets:
            stream_id = self._resolve_stream_id(target)
            if not stream_id:
                logger.warning("无法解析推送目标：%s", target)
                continue
            try:
                await send_api.text_to_stream(
                    text=message,
                    stream_id=str(stream_id),
                    typing=False,
                    storage_message=True,
                )
            except Exception as exc:
                if self.get_config("plugin.debug", False):
                    logger.exception("发送文献推送至 %s 失败: %s", target, exc)
                else:
                    logger.error("发送文献推送至 %s 失败: %s", target, exc)

    def _resolve_stream_id(self, target: str) -> Optional[str]:
        """Resolve a target descriptor to a stream id."""
        if target is None:
            return None

        candidate = str(target).strip()
        if not candidate:
            return None

        if ":" in candidate:
            prefix, identifier = candidate.split(":", 1)
            prefix = prefix.strip().lower()
            identifier = str(identifier).strip()
        else:
            prefix, identifier = "stream", candidate

        stream_obj = None
        try:
            if prefix == "group":
                resolver = getattr(chat_api, "get_stream_by_group_id", None)
                if resolver:
                    stream_obj = resolver(identifier)
            elif prefix in {"private", "user"}:
                resolver = getattr(chat_api, "get_stream_by_user_id", None)
                if resolver:
                    stream_obj = resolver(identifier)
            else:
                resolver = getattr(chat_api, "get_stream", None)
                if resolver:
                    stream_obj = resolver(identifier)
        except Exception as exc:
            logger.warning("通过 chat_api 解析目标 %s 失败: %s", target, exc)

        # 借鉴话题插件的处理方式：若未找到，尝试从聊天管理器或依据纯数字ID作为群号再次解析
        if not stream_obj and get_chat_manager:
            with suppress(Exception):
                stream_obj = get_chat_manager().get_stream(identifier)

        if not stream_obj:
            # 若未指定前缀且目标看起来像群号，再试一次按群聊解析
            if prefix == "stream" and identifier.isdigit():
                resolver = getattr(chat_api, "get_stream_by_group_id", None)
                if resolver:
                    with suppress(Exception):
                        stream_obj = resolver(identifier)
            # 或者尝试作为用户ID
            if not stream_obj and prefix == "stream":
                resolver = getattr(chat_api, "get_stream_by_user_id", None)
                if resolver:
                    with suppress(Exception):
                        stream_obj = resolver(identifier)

        if stream_obj:
            stream_id = getattr(stream_obj, "stream_id", None) or getattr(stream_obj, "id", None)
            if stream_id:
                return str(stream_id)

        if prefix == "stream":
            return identifier or None

        return None

    def _load_seen_cache(self) -> None:
        """Load seen item identifiers from disk."""
        if not self.seen_cache_path.exists():
            return
        try:
            content = self.seen_cache_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, list):
                self.seen_ids.update(str(item) for item in data)
            elif isinstance(data, dict):
                self.seen_ids.update(str(item) for item in data.get("items", []))
        except Exception as exc:
            logger.warning("加载去重缓存失败，将重新生成: %s", exc)

    async def _persist_seen_cache(self) -> None:
        """Persist seen identifiers to disk."""
        data = sorted(self.seen_ids)

        def _write():
            tmp_path = self.seen_cache_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self.seen_cache_path)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.warning("保存去重缓存失败: %s", exc)

    async def build_preview(
        self,
        manual_trigger: bool = False,
        source_flags: Optional[List[str]] = None,
    ) -> str:
        """Construct a human readable preview based on current configuration."""
        sources = self._select_sources(source_flags)
        if source_flags and not sources:
            flags_text = ", ".join(source_flags)
            return f"⚠️ 未找到匹配的数据源：{flags_text}"

        interval = int(self.get_config("scheduler.interval_minutes", 120))
        specific_times = self.get_config("scheduler.specific_times", [])
        timezone_name = self.get_config("scheduler.timezone", "Asia/Shanghai")
        on_the_hour = bool(self.get_config("scheduler.on_the_hour", False))
        targets = self.get_config("delivery.targets", [])
        summary_style = self.get_config("delivery.summary_style", "bullet")

        header = "📚 文献推送计划预览\n"
        if manual_trigger:
            header = "📚 文献推送手动预览\n"

        lines = [
            header,
            f"• 调度间隔：{'关闭' if interval <= 0 else f'每 {interval} 分钟'}",
            f"• 定时触发：{specific_times or '未配置'}（时区：{timezone_name}）",
            f"• 整点触发：{'开启' if on_the_hour else '关闭'}",
            f"• 推送目标：{targets or '未配置'}",
            f"• 摘要模式：{summary_style}",
            f"• 数据源数量：{len(sources)}",
        ]

        if source_flags:
            lines.append(f"• 指定来源：{', '.join(source_flags)}")

        if sources:
            lines.append("\n➡️ 已配置的数据源：")
            for idx, item in enumerate(sources, 1):
                label = item.get("label") or item.get("type", "未知来源")
                lines.append(f"  {idx}. {label}")

        return "\n".join(lines)

    async def on_destroy(self) -> None:
        """Ensure background tasks are cancelled when the plugin unloads."""
        await self.stop_scheduler()

    async def on_unload(self) -> None:
        """Compatibility hook for plugin manager variants."""
        await self.stop_scheduler()

    def _format_search_results(
        self,
        items: List[Dict[str, Any]],
        keywords: List[str],
        *,
        include_tags: bool,
        summary_style: str,
        source_flags: Optional[List[str]] = None,
    ) -> str:
        """Format search results for presentation."""
        query = " ".join(keywords)
        header = f"🔎 文献搜索：{query}" if query else "🔎 文献搜索结果"
        if source_flags:
            header += f"（来源：{', '.join(source_flags)}）"

        lines: List[str] = [header, ""]
        for idx, item in enumerate(items, 1):
            title = item.get("title") or "未命名条目"
            lines.append(f"{idx}. {title}")

            meta_parts: List[str] = []
            if include_tags and item.get("label"):
                meta_parts.append(str(item["label"]))
            if item.get("authors"):
                meta_parts.append(str(item["authors"]))
            if meta_parts:
                lines.append(f"   来源：{' | '.join(meta_parts)}")

            summary = item.get("summary")
            if summary:
                formatted_summary = self._format_summary(summary, summary_style)
                if formatted_summary:
                    lines.append(f"   摘要：{formatted_summary}")

            link = item.get("link")
            if link:
                lines.append(f"   链接：{link}")

            lines.append("")

        return "\n".join(lines).strip()
