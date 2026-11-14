# -*- coding: utf-8 -*-
"""话匣子（astrbot_plugin_chatterbox）—— M2：状态机 + 闸门 + 主动发言。

本阶段做的事：
1. 捕获群消息（私聊永不触碰），维护每群上下文缓冲与水位线
2. 每群独立状态机：潜水 / 活跃（被 @ 进活跃、到期回落、带续期与硬顶）
3. 被 @ / 提到名字：过闸门 → 走 AstrBot 管线回复 → 引用那条消息；超出配额静默丢弃
4. 潜水 / 活跃按各自节奏判定，REPLY 则主动接一句（活跃态先掷概率）
5. shadow_mode=true 时判定照跑但不主动发言；被 @ / 引用的回复仍正常走 AstrBot 管线
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.cache import JsonCache, make_key
from .core.commands import is_plugin_command
from .core.config import (
    Cfg,
    OnceFlag,
    media_enabled,
    resolve_groups,
    save_disabled_groups,
    save_value,
)
from .core.ctx import (
    CtxBuffer,
    apply_caption,
    apply_forward,
    group_context_block,
    render,
    trim_contexts,
)
from .core.decide import (
    Verdict,
    build_hint,
    compose_probe,
    judge,
    pick_active_action,
    pick_judge_persona,
    pick_judge_provider,
)
from .core.forward import parse_forward
from .core.gate import Gate, gap_blocks
from .core.presence import MANUAL_AWAKE, MANUAL_SLEEP, Presence, should_sleep
from .core.segment import has_separator, plan_segments
from .core.trace import Trace, TraceStore, extract_memories
from .modules.capture import Capture, forward_ids, image_segments
from .modules.persona import PersonaCache
from .modules.speak import insert_quote, send_text, split_plain_segments
from .modules.vision import caption

PLUGIN_NAME = "astrbot_plugin_chatterbox"
VERSION = "0.20.0"

TAG = "[话匣子]"
EXTRA_BLOCKED = "chatterbox_gated"
EXTRA_QUOTE = "chatterbox_quote"
EXTRA_PROACTIVE = "chatterbox_proactive"  # 这条回复是「主动接话」发出的
EXTRA_TRIGGER_SEQ = "chatterbox_trigger_seq"  # 发起时的群触发序号
TICK_SEC = 1.0
SEGMENT_GAP_SEC = 1.2  # probe 分段发送时两段之间的间隔，像正常打字，也避开连发风控
BEAT_LOG_SEC = 300.0


class ChatterboxPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cfg = Cfg(config)
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME

        self._alive = False
        self._task: asyncio.Task | None = None
        self._workers: set[asyncio.Task] = set()
        self._ticks = 0
        self._last_beat = 0.0
        # 手动模式（v0.19.0）：/offair 存 sleep，/onair 与 /lurk 存 awake；空 = 跟时间窗
        self._manual_mode: dict[str, str] = {}
        self._sleep_read_at: dict[str, float] = {}  # 群号 -> 上次休眠读群时间
        # 媒体模型缺失的一次性告警（D3）：只刷屏一次，之后安静降级为占位符
        self._warned_no_vision_provider = OnceFlag()
        self._warned_no_stt_provider = OnceFlag()
        # 判定模型缺失的一次性提示（D5）：给出修复路径，不每次判定都刷警告
        self._warned_no_judge_provider = OnceFlag()
        self._warned_bad_judge_provider = OnceFlag()

        self.buf = CtxBuffer(maxlen_per_group=120)
        self.capture = Capture(
            self.buf, self.target_groups, self_id="", bot_names=self._bot_name_list
        )
        self.persona_cache = PersonaCache()
        self.vision_cache = JsonCache(
            self.data_dir / "vision_cache.json",
            ttl_sec=self.cfg.num("context.cache_ttl_hours", 168.0) * 3600.0,
        )
        self.voice_cache = JsonCache(
            self.data_dir / "voice_cache.json",
            ttl_sec=self.cfg.num("context.cache_ttl_hours", 168.0) * 3600.0,
        )
        self.forward_cache = JsonCache(
            self.data_dir / "forward_cache.json",
            ttl_sec=self.cfg.num("context.cache_ttl_hours", 168.0) * 3600.0,
        )
        self._bots: dict[str, object] = {}  # 群号 -> 平台 bot 实例（展开转发表要用）
        self.traces = TraceStore(
            self.data_dir / "trace.json",
            max_items=self.cfg.integer("debug.trace_max_items", 100),
        )
        self._pending_trace: dict[str, Trace] = {}  # umo -> 待归档的发言档案
        self._pending_verdict: dict[str, tuple[bool, str, float]] = {}  # 群号 -> (yes, 理由, 时间)
        self._last_persona: dict[str, str] = {}  # 群号 -> 最近一次用的人格名
        self.presence = Presence(self.cfg)
        self.gate = Gate(self.cfg)
        self._rt: dict[str, dict] = {}
        self._last_verdict: dict[str, Verdict] = {}
        self._judged_total = 0
        self._replied_total = 0
        self._verdict_log: list[dict] = []  # 最近判定流水，给面板看
        self._img_paths: dict[str, str] = {}  # 图片 URL -> 本地文件（视觉模型读本地更快）
        self._voice_paths: dict[str, str] = {}  # 语音标识 -> 本地文件（STT 只吃本地）
        self._sticker_skipped = 0  # 累计跳过的表情包数量（给面板和日志看）

        # 后台预转述：消息进来就丢进队列，判定时只读缓存。
        # 转述耗时取决于模型与网络，同步等待会拖长「有人说话 → 判定」的链路，
        # 改成后台跑之后这段时间完全藏起来，成本不变。
        self._caption_q: asyncio.Queue | None = None
        self._caption_task: asyncio.Task | None = None
        self._caption_inflight: set[str] = set()
        self._caption_done = 0
        self._caption_dropped = 0
        self._register_web()

    # ───────── 生命周期 ─────────

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._alive = True
        self._task = asyncio.create_task(self._tick_loop(), name="chatterbox_tick")
        self._caption_q = asyncio.Queue()
        self._caption_task = asyncio.create_task(
            self._caption_loop(), name="chatterbox_caption"
        )
        groups = self.target_groups()
        if not self.configured_groups():
            logger.warning(
                f"{TAG} 尚未配置管理的群号（groups 为空）：插件不会在任何群生效。"
                "请在插件配置 groups 里填写要管理的群号（建议先只填一个测试群）。"
            )
        logger.info(
            f"{TAG} v{VERSION} 已加载（M2 · 主动发言影子={self.cfg.bool('debug.shadow_mode', True)}）· "
            f"管理群 {len(groups)} 个：{','.join(groups) if groups else '(空)'}"
        )
        judge_pid = self.cfg.text("decision.judge_provider_id", "").strip()
        logger.info(
            f"{TAG} 判定模型：{judge_pid or '跟随会话模型（decision.judge_provider_id 留空即可）'}"
        )
        if self.cfg.bool("debug.shadow_mode", True):
            logger.info(
                f"{TAG} 主动发言影子模式开启：不会主动接话或开话题，"
                "被 @/引用的回复仍由 AstrBot 正常处理。确认日志无误后可在面板关闭。"
            )
        missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
        if missing:
            logger.warning(
                f"{TAG} 未找到 {','.join(missing)}：语音消息会转写失败（AstrBot 语音预处理依赖它）"
            )
        else:
            logger.info(f"{TAG} ffmpeg 可用：{shutil.which('ffmpeg')}")

    async def terminate(self):
        self._alive = False
        for task in list(self._workers):
            if not task.done():
                task.cancel()
        self._workers.clear()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        caption_task = self._caption_task
        self._caption_task = None
        self._caption_q = None
        if caption_task is not None and not caption_task.done():
            caption_task.cancel()
            try:
                await caption_task
            except asyncio.CancelledError:
                pass
        try:
            self.vision_cache.save()
            self.voice_cache.save()
            self.forward_cache.save()
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"{TAG} 已停止（心跳 {self._ticks} 次，判定 {self._judged_total} 次）")

    # ───────── 消息捕获 ─────────

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息唯一入口。

        三条通道：
        1. @ / 引用 / 唤醒词 —— AstrBot 本来就会回复，我们只加引用 + 过闸门；
        2. 叫到名字（无 @）—— 管线不会唤醒，自己发起一次 request_llm；
        3. 普通消息 —— 按节奏判定，REPLY 就用 request_llm 走管线生成。

        v0.6.0 起主动发言不再自己拼提示词，而是复用 AstrBot 的正常聊天管线，
        这样人格、对话历史、长期记忆、内置工具都自动生效。
        """
        # 指令（/onair 等）自己管状态，不能当成「被叫到」先替它进活跃 ——
        # 否则命令执行时状态已经是 active，/onair 永远回「本来就在活跃」。
        if self._is_command(event):
            return
        # 非受管群一律不碰：媒体收集（下载图片/语音）也必须发生在
        # 确认群范围之后，避免替不管理的群白花流量和模型调用。
        gid = str(event.get_group_id() or "").strip()
        if not gid or gid not in set(self.target_groups()):
            return
        try:
            imgs, voices, forwards, stickers = await self._collect_media(event)
            msg = self.capture.feed(
                event,
                log=logger,
                # 显式传列表（哪怕是空列表）：传 None 会让 Capture 重新从原始事件
                # 提取媒体，等于绕过了上面的开关。
                images=imgs,
                voices=voices,
                forwards=forwards,
                sticker_only=bool(
                    stickers and not imgs and not voices and not forwards
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 捕获异常：{exc!r}")
            return
        if msg is None:
            return

        # 图片/语音/转发立刻丢给后台处理，不阻塞下面的判定。
        # 这一轮拿不到内容就先按占位符走，下一次判定就有。
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._bots[gid] = bot
        self._enqueue_captions(gid, msg)

        # 休眠时段：消息照收（缓冲不丢），被 @ / 引用 / 叫名字 仍正常回复，
        # 但绝不进入活跃（不进 30 分钟窗口、不判定、不主动发言）。
        sleeping = self._sleeping_now(gid)
        try:
            if event.is_at_or_wake_command:
                # 管线会回（@、@全体、引用 bot 都算唤醒），这里只负责
                # 「闸门 / 记引用 / 日志」；休眠时 on_trigger 不会改状态
                self._on_trigger(event, gid, source="at")
                return

            if "提到名字" in (msg.flag or ""):
                if self._on_trigger(event, gid, source="name"):
                    req = await self._judge_then_request(event, gid, force=True)
                    if req is not None:
                        yield req
                return

            if sleeping:
                return  # 休眠中普通消息只进缓冲，不判定、不发言

            req = await self._judge_then_request(event, gid)
            if req is not None:
                yield req
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 处理群消息异常 群={gid}：{exc!r}"[:300])

    def _on_trigger(self, event: AstrMessageEvent, gid: str, *, source: str) -> bool:
        """被叫到：进活跃 → 过闸门 → 记下要引用的消息。返回是否放行。"""
        now = time.time()
        if self._sleeping_now(gid):
            # 休眠窗口内被叫到：确保状态是 sleep（不会进活跃），但照样放行回复
            self.presence.enter_sleep(gid)
        action = self.presence.on_trigger(gid, now)
        st = self.presence.get(gid)
        allowed, why = self.gate.allow(st, now)
        if not allowed:
            event.set_extra(EXTRA_BLOCKED, True)
            logger.info(
                f"{TAG} 触发被闸门拦下 群={gid} 来源={source} "
                f"窗口内已说 {self.gate.used(st, now)} 条（{why}）"
            )
            return False
        self.gate.record(st, now)
        if self._sleeping_now(gid):
            logger.info(
                f"{TAG} 休眠中触发 群={gid} 来源={source}：正常回复，不进入活跃"
                f"（窗口 {self.presence.sleep_window_text()}）"
            )
        # 这条会引出一次管线回复，先记成「本群刚说过话」：
        # 否则同一时刻的判定会把这条当成普通新消息，自己再插一句，
        # 两句话贴在一起发出来，群里看到的就是「一个人同时说两句」。
        rt = self._rt_of(gid)
        rt["last_activity_at"] = now
        # 序号 +1：正在生成的主动接话一看到这个变化，就会把自己丢掉
        rt["trigger_seq"] = int(rt.get("trigger_seq", 0)) + 1
        quote_id = str(getattr(event.message_obj, "message_id", "") or "")
        event.set_extra(EXTRA_QUOTE, quote_id)
        logger.info(
            f"{TAG} 触发 群={gid} 来源={source} 动作={action} "
            f"剩余活跃={self.presence.remaining_sec(gid, now)}s 引用={quote_id or '无'}"
        )
        return True

    @filter.on_llm_request(priority=9000)
    async def block_when_gated(self, event: AstrMessageEvent, _req=None):
        """窗口内说太多了：直接掐掉这次请求（对应「溢出的 @ 不回复」）。"""
        try:
            if event.get_extra(EXTRA_BLOCKED):
                event.stop_event()
        except Exception:  # noqa: BLE001
            pass

    @filter.on_llm_request(priority=8000)
    async def inject_group_context(self, event: AstrMessageEvent, req=None):
        """把最近群聊（含图片转述）注入本次请求。

        AstrBot 自带的对话历史只包含「真正触发过 LLM 的消息」。群里有人先发一张图、
        隔一条再 @ 的时候，那张图不会出现在请求里，模型只看得到 [图片] 占位——
        这就是「只发图 + @、不引用图片就读不到图」的原因。
        这里把缓冲区里最近 N 条拼成 <group_context> 追加进请求：图片先走视觉模型
        转述（带缓存和每轮上限），文字原样带上，让 @ 回复也能看见之前的图和群聊。
        """
        try:
            if req is None or event.is_stopped():
                return
            gid = str(event.get_group_id() or "").strip()
            if not gid or gid not in set(self.target_groups()):
                return
            # 先裁掉 AstrBot 自带的会话历史：这个群能堆到几百条，整段发出去既贵
            # 又稀释注意力。群聊上下文改由 <group_context> + 长期记忆 + 知识库承担。
            self._trim_history(req, gid)
            recent = self.buf.recent(gid, self.cfg.integer("context.ctx_messages", 30))
            if not recent:
                return
            await self._ensure_captions(gid, recent)
            block = render(
                recent,
                recent=self.cfg.integer("context.recent_split", 8),
                max_chars=4000,
            )
            if not block:
                return
            req.extra_user_content_parts.append(
                TextPart(
                    text=group_context_block(
                        block, proactive=bool(event.get_extra(EXTRA_PROACTIVE))
                    )
                )
            )
            logger.info(f"{TAG} 注入群聊上下文 群={gid} 条数={len(recent)}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 注入群聊上下文失败：{exc!r}")

    def _trim_history(self, req, gid: str) -> None:
        """把请求里的会话历史裁到 `context.history_messages` 条（0 = 全不要）。

        AstrBot 的 `req.contexts` 是整个会话的历史，活跃群会堆到很长
        （可能远超本插件注入的条数）。群聊上下文由 `<group_context>`（最近 N 条原文）
        负责，长期记忆和知识库各走各的注入通道，所以这里可以裁掉。
        """
        try:
            keep = self.cfg.integer("context.history_messages", 0)
            old = getattr(req, "contexts", None)
            new = trim_contexts(old, keep)
            if isinstance(old, list) and len(new) < len(old):
                req.contexts = new
                logger.debug(
                    f"{TAG} 裁剪会话历史 群={gid} {len(old)} → {len(new)} 条（上限 {keep}）"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 裁剪会话历史失败：{exc!r}")

    @filter.on_llm_request(priority=-1000)
    async def snapshot_trace(self, event: AstrMessageEvent, req=None):
        """只读快照：把这次请求「看到了什么」记下来，等发送后归档。

        优先级 -1000 是故意的：数值大的先执行，所以它排在 livingmemory（priority 0）
        之后，能读到对方注入的 `<RAG-Faiss-Memory>` 记忆正文。

        **绝不修改 req**。知识库片段在这之后（build_main_agent 里）才注入，这里
        只能记下启用了哪些库，具体片段在 /溯源 时按需复算。
        """
        try:
            if req is None:
                return
            gid = str(event.get_group_id() or "").strip()
            if not gid or gid not in set(self.target_groups()):
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            parts = [
                str(getattr(p, "text", "") or "")
                for p in (getattr(req, "extra_user_content_parts", None) or [])
            ]
            memories = extract_memories(parts)
            try:
                conf = (
                    self.context.get_config(umo=umo)
                    if umo
                    else self.context.get_config()
                )
                kb_names = [str(n) for n in (conf.get("kb_names") or []) if str(n)]
            except Exception:  # noqa: BLE001
                kb_names = []
            if event.get_extra(EXTRA_PROACTIVE):
                trigger = "proactive"
            elif event.is_at_or_wake_command:
                trigger = "at"
            else:
                trigger = ""
            self._pending_trace[umo] = Trace(
                ts=time.time(),
                gid=gid,
                umo=umo,
                trigger=trigger,
                prompt=str(getattr(req, "prompt", "") or "")[:500],
                state=self._state_text(gid),
                persona=self._last_persona.get(gid, ""),
                model=str(getattr(req, "model", "") or ""),
                ctx_count=len(getattr(req, "contexts", None) or []),
                memories=memories,
                kb_names=kb_names,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 发言快照失败：{exc!r}")

    @filter.on_decorating_result()
    async def add_quote(self, event: AstrMessageEvent):
        """发送前的最后一道关：

        1. 主动接话在生成期间如果又被人 @ 了，这条就丢掉（否则两条回复会绞在一起）；
        2. 记下「马上要说完了」的时间，让下一次判定至少等 gap 秒。
        """
        try:
            gid = str(event.get_group_id() or "").strip()
            if not gid:
                return
            rt = self._rt_of(gid)

            if event.get_extra(EXTRA_PROACTIVE):
                started_seq = event.get_extra(EXTRA_TRIGGER_SEQ)
                if started_seq is not None and rt.get("trigger_seq", 0) != started_seq:
                    result = event.get_result()
                    if result and getattr(result, "chain", None):
                        result.chain.clear()
                    logger.info(
                        f"{TAG} 主动发言生成期间有人叫我，已丢弃这条 群={gid}"
                    )
                    return

            # 回复即将发出（分段发送从这里开始），从现在算「刚说过话」
            rt["last_activity_at"] = time.time()
            rt["last_judge_at"] = time.time()

            self._ensure_segments(event, gid)

            quote_id = event.get_extra(EXTRA_QUOTE)
            if not quote_id:
                return
            result = event.get_result()
            if result and insert_quote(result, quote_id):
                logger.debug(f"{TAG} 已插入引用 {quote_id}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 发送前处理异常：{exc!r}")

    def _ensure_segments(self, event: AstrMessageEvent, gid: str) -> None:
        """兜底分段：AstrBot 判定「这不是模型结果」时不会按分段词拆，这里补一刀。

        背景：`ResultDecorateStage` 的分段条件是
        `only_llm_result and result.is_model_result()`，而插件用 `request_llm`
        主动发起的回复在某些分支下拿到的是 `GENERAL_RESULT`，于是整段不拆，
        反斜杠原样发进群里。这里用同一份配置复算一次，只在那种情况才动手。

        诊断日志只在文本里真的含分段词时打，平时不吵。
        """
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            conf = (
                self.context.get_config(umo=umo) if umo else self.context.get_config()
            )
            sr = (conf.get("platform_settings") or {}).get("segmented_reply") or {}
            if not sr.get("enable"):
                return
            if str(sr.get("split_mode") or "regex") != "words":
                return
            words = [str(w) for w in (sr.get("split_words") or []) if str(w)]
            if not words:
                return
            limit = int(sr.get("words_count_threshold") or 150)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 读分段配置失败：{exc!r}")
            return

        text = "".join(
            str(getattr(c, "text", ""))
            for c in result.chain
            if type(c).__name__ == "Plain"
        )
        if not has_separator(text, words):
            return

        typ = getattr(result, "result_content_type", None)
        typ_name = str(getattr(typ, "value", typ))
        try:
            model_result = bool(result.is_model_result())
        except Exception:  # noqa: BLE001
            model_result = False

        if model_result:
            hits = sum(text.count(w) for w in words)
            logger.info(
                f"{TAG} 分段诊断 类型={typ_name} 段数={len(result.chain)} "
                f"分隔词 {hits} 处 · 交给本体拆"
            )
            return

        total = split_plain_segments(result, words, limit)
        logger.info(
            f"{TAG} 分段兜底 类型={typ_name} 原文 {len(text)} 字 → {total} 段 群={gid}"
            f"（本体认为这不是模型结果，不会自行分段）"
        )

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        """整条回复（含分段）真正发完之后：归档发言档案 + 再刷新一次时间。

        分段回复是逐条 sleep + send 的，生成时记的时间可能比最后一段早十几秒；
        在这里刷新，才能保证「刚说完」之后的 gap 是从最后一段算起。
        """
        try:
            gid = str(event.get_group_id() or "").strip()
            if not gid:
                return
            self._archive_trace(event, gid)
            if gid not in set(self.target_groups()):
                return
            rt = self._rt_of(gid)
            rt["last_activity_at"] = time.time()
            rt["last_judge_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 发送后刷新失败：{exc!r}")

    def _archive_trace(self, event: AstrMessageEvent, gid: str) -> None:
        """把快照补上「实际发出去的文本」后归档。空回复不归档。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        trace = self._pending_trace.pop(umo, None)
        if trace is None:
            return
        result = event.get_result()
        texts = [
            str(getattr(c, "text", "") or "").strip()
            for c in (getattr(result, "chain", None) or [])
            if type(c).__name__ == "Plain"
        ]
        reply = "\n".join(t for t in texts if t).strip()
        if not reply:
            return  # 被闸门拦下 / 撞车丢弃 / 空回复：不占档案位
        trace.reply = reply[:800]
        rec = self._pending_verdict.pop(gid, None)
        # 只认两分钟内的判定结果，避免上一次 SKIP 的理由贴到这次发言上
        if rec and time.time() - rec[2] < 120:
            trace.verdict = "REPLY" if rec[0] else "SKIP"
            trace.reason = rec[1]
        self.traces.add(trace)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """把 bot 自己说的话也记进缓冲。

        没有这条，判定模型就看不到「我刚说过什么、多久之前说的」，
        prompt 里「你刚说过话就别再说」那条规则等于无法执行 —— 模型只能瞎猜。
        """
        try:
            if str(getattr(resp, "role", "")) not in ("assistant", ""):
                return
            gid = str(event.get_group_id() or "").strip()
            if not gid or gid not in set(self.target_groups()):
                return
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return
            self.buf.add(
                gid,
                role="assistant",
                name=self.cfg.text("decision.bot_names", ""),
                uid=str(event.get_self_id() or "bot"),
                text=text,
            )
            # 管线刚回过话：把水位线推到自己这条、并记下「刚说过话」。
            # 不推水位线 -> 下个 tick 会把这条回复当新消息再判一次，追着自己接话。
            rt = self._rt_of(gid)
            last = self.buf.recent(gid, 1)
            if last:
                rt["watermark"] = max(rt["watermark"], last[-1].seq)
            rt["last_judge_at"] = time.time()
            rt["last_activity_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 记录自己发言失败：{exc!r}")

    # ───────── 后台心跳 ─────────

    async def _tick_loop(self):
        """1 秒醒一次：维护活跃态是否到期，并打心跳日志。

        v0.6.0 起「要不要接话」改由消息事件驱动（见 on_group_message），
        这里只剩状态维护；M4 的冷场开话题会重新用到这条循环。
        循环内任何异常都不许退出：一次报错就永久停止看群，而表面上只有一条 warning。
        """
        while self._alive:
            try:
                await asyncio.sleep(TICK_SEC)
                if not self._alive:
                    break
                self._ticks += 1
                now = time.time()
                for gid in self.target_groups():
                    if self._sleeping_now(gid):
                        if self.presence.enter_sleep(gid):
                            self._sleep_read_at.pop(gid, None)  # 进入休眠先读一次
                            logger.info(
                                f"{TAG} 进入休眠 群={gid} 窗口={self.presence.sleep_window_text()}"
                                f"（每 {self.cfg.num('sleep.read_tick_min', 30.0):g} 分钟读一次群，"
                                f"不回复、不发言）"
                            )
                        self._sleep_read_tick(gid, now)
                        continue
                    if self.presence.exit_sleep(gid, now):
                        self._sleep_read_at.pop(gid, None)
                        logger.info(f"{TAG} 退出休眠 群={gid}（回潜水）")
                    # 顺序要紧：先让探话窗口自己结算（answered / timeout），
                    # 再收回「活跃到期」。反过来的话 refresh 会先把 probing 转成
                    # dive，_probe_tick 就看不到 probing，「没人接话」那条日志永远不触发。
                    self._probe_tick(gid, now)
                    if self.presence.refresh(gid, now):
                        # 只打一行、且只在真的发生迁移时打：tick 是 1 秒一次，
                        # 无条件记日志会把「为什么突然不理人」的现场淹掉。
                        st = self.presence.get(gid)
                        left = max(0, int(st.cooldown_until - now))
                        logger.info(
                            f"{TAG} 活跃到期，回潜水 群={gid} 冷却 {left}s"
                            f"（这期间 @ 只回一条，不进活跃）"
                        )
                    self._maybe_probe(gid, now)
                if now - self._last_beat >= BEAT_LOG_SEC:
                    self._last_beat = now
                    if self.cfg.bool("debug.verbose_log", True):
                        logger.info(
                            f"{TAG} 心跳 ticks={self._ticks} 群={len(self.target_groups())} "
                            f"判定={self._judged_total} "
                            f"shadow={self.cfg.bool('debug.shadow_mode', True)}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{TAG} 心跳异常：{exc!r}"[:300])

    # ───────── 休眠 ─────────

    def _sleeping_now(self, gid: str = "") -> bool:
        """这个群现在算不算休眠：手动模式优先，否则按时间窗。

        gid 为空时只看时间窗（面板总览用），没有"某个群"可判断。
        """
        manual = self._manual_mode.get(gid, "") if gid else ""
        if manual == MANUAL_SLEEP:
            return True
        if not self.cfg.bool("sleep.enabled", False):
            return False
        return should_sleep(manual, self.presence.in_sleep_window(time.time()))

    def _sleep_read_tick(self, gid: str, now: float) -> None:
        """休眠中按 sleep.read_tick_min 节奏安排一次读群。"""
        interval = max(60.0, self.cfg.num("sleep.read_tick_min", 30.0) * 60.0)
        if now - self._sleep_read_at.get(gid, 0.0) < interval:
            return
        self._sleep_read_at[gid] = now
        task = asyncio.create_task(self._sleep_read(gid), name=f"sleep_read_{gid}")
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _sleep_read(self, gid: str) -> None:
        """休眠读群：只补图片转述、记一条日志；不判定、不发言。

        水位线故意不推进：醒来后第一次判定仍能看到休眠期间新进的消息（受注入条数上限约束）。
        """
        try:
            if not self._sleeping_now(gid):
                return
            rt = self._rt_of(gid)
            pending = self.buf.after(gid, rt["watermark"])
            recent = self.buf.recent(gid, self.cfg.integer("context.ctx_messages", 30))
            if not recent:
                logger.info(f"{TAG} 休眠读群 群={gid} 缓冲空（还没收到消息）")
                return
            await self._ensure_captions(gid, recent)
            logger.info(
                f"{TAG} 休眠读群 群={gid} 新消息={len(pending)} 上下文={len(recent)} "
                f"下次 {self.cfg.num('sleep.read_tick_min', 30.0):g} 分钟后"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 休眠读群异常 群={gid}：{exc!r}"[:300])

    def _state_text(self, gid: str) -> str:
        """给档案看的状态名。"""
        try:
            if self._sleeping_now(gid):
                return "休眠"
            return "活跃" if self.presence.is_active(gid) else "潜水"
        except Exception:  # noqa: BLE001
            return ""

    def _rt_of(self, gid: str) -> dict:
        return self._rt.setdefault(
            gid,
            {                "watermark": 0,
                "last_judge_at": 0.0,
                "last_throttle_log_at": 0.0,
                "busy": False,
                "last_activity_at": 0.0,  # 本群上一次「说话」的时间（管线回复也算）
                "trigger_seq": 0,  # 每次被 @ / 叫到名字 +1，用来发现「生成期间又有人叫我」
            },
        )

    def _bot_name_list(self) -> list[str]:
        """机器人自己的名字/外号，用来识别「有人在叫我」。"""
        raw = self.cfg.text("decision.bot_names", "")
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def _provider_exists(self, pid: str) -> bool:
        """模型 ID 是否真的还在（用户可能删了模型或改了名）。

        校验本身出错时返回 True：不能把「校验失败」误报成「模型不存在」，
        真正的调用异常由 judge 的通用重试与日志负责。
        """
        try:
            return bool(pid) and self.context.get_provider_by_id(pid) is not None
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 校验模型 {pid} 失败：{exc!r}")
            return True

    async def _judge_provider(self, umo: str) -> str:
        """判定用哪个模型：配置了且存在就用，否则跟随当前会话的对话模型。"""
        configured = self.cfg.text("decision.judge_provider_id", "").strip()
        fallback = ""
        if not configured or not self._provider_exists(configured):
            if umo:
                try:
                    pid = await self.context.get_current_chat_provider_id(umo)
                    if pid:
                        fallback = str(pid)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"{TAG} 取当前会话模型失败：{exc!r}")
        chosen, err = pick_judge_provider(configured, self._provider_exists, fallback)
        if err and self._warned_bad_judge_provider.take():
            logger.warning(
                f"{TAG} {err}：请在插件配置 decision.judge_provider_id 里重新选择；"
                "本次先跟随会话模型（没有会话模型时不会主动接话）。"
            )
        return chosen

    # ───────── 面板 ─────────

    def _register_web(self):
        try:
            import importlib.util

            if importlib.util.find_spec("astrbot.api.web") is None:
                return  # 4.29 之前的版本没有这套接口
        except Exception:  # noqa: BLE001
            return
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/state", self.web_state, ["GET"], "话匣子：状态"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/action",
                self.web_action,
                ["POST"],
                "话匣子：切换影子模式 / 强制回潜水",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 注册面板接口失败：{exc!r}"[:200])

    def snapshot(self) -> dict:
        now = time.time()
        groups = []
        # 单群是否停用直接看 disabled_groups，不要用 target_groups() 反推：
        # 全局 enabled=false 时 target_groups() 恒为空，会把所有群误标成「已停用」（C3）
        off = {
            str(x).strip() for x in self.cfg.items("disabled_groups") if str(x).strip()
        }
        for gid in self.configured_groups():
            self.presence.refresh(gid, now)
            st = self.presence.get(gid)
            v = self._last_verdict.get(gid)
            groups.append(
                {
                    "gid": gid,
                    "disabled": gid in off,
                    "state": st.state,
                    "sleeping": self._sleeping_now(gid),
                    "remaining_sec": self.presence.remaining_sec(gid, now),
                    "gate_used": self.gate.used(st, now),
                    "buffer": len(self.buf.recent(gid, 999)),
                    "last": (
                        {
                            "verdict": "REPLY" if v.yes else "SKIP",
                            "reason": v.reason,
                            "elapsed_ms": v.elapsed_ms,
                        }
                        if v
                        else None
                    ),
                }
            )
        return {
            "version": VERSION,
            "enabled": self.cfg.bool("enabled", True),
            "shadow": self.cfg.bool("debug.shadow_mode", True),
            "sleeping_now": self._sleeping_now(),
            "ticks": self._ticks,
            "judged": self._judged_total,
            "stickers_skipped": self._sticker_skipped,
            "captions_done": self._caption_done,
            "captions_pending": (self._caption_q.qsize() if self._caption_q else 0),
            "captions_dropped": self._caption_dropped,
            "traces": len(self.traces.items),
            "trace_items": [
                {
                    "ts": t.ts,
                    "gid": t.gid,
                    "trigger": t.trigger,
                    "verdict": t.verdict,
                    "reason": t.reason,
                    "mem": len(t.memories),
                    "kb": t.kb_names,
                    "reply": (t.reply or "").replace("\n", " ")[:60],
                    "prompt": (t.prompt or "").replace("\n", " ")[:40],
                    "state": t.state,
                }
                for t in self.traces.recent(limit=20)
            ],
            "replied": self._replied_total,
            "gate_max": self.cfg.integer("gate.gate_max", 3),
            "gate_window": self.cfg.num("gate.gate_window_sec", 300.0),
            "cfg": {
                "dive_tick_min": self.cfg.num("state.dive_tick_min", 3.0),
                "active_tick_sec": self.cfg.num("state.active_tick_sec", 30.0),
                "active_ttl_min": self.cfg.num("state.active_ttl_min", 30.0),
                "extend_min": self.cfg.num("state.extend_per_trigger_min", 3.0),
                "active_max_min": self.cfg.num("state.active_max_min", 120.0),
                "speak_prob": self.cfg.num("decision.speak_prob_active", 0.35),
                "judge_provider": self.cfg.text("decision.judge_provider_id", "").strip(),
                "judge_persona_chars": len(
                    self.cfg.text("decision.judge_persona", "").strip()
                ),
                "judge_in_active": self.cfg.bool("decision.judge_in_active", True),
                "history_messages": self.cfg.integer("context.history_messages", 0),
                "sleep_enabled": self.cfg.bool("sleep.enabled", False),
                "sleep_window": self.presence.sleep_window_text(),
                "sleep_read_min": self.cfg.num("sleep.read_tick_min", 30.0),
            },
            "groups": groups,
            "log": list(reversed(self._verdict_log[-40:])),
        }

    async def web_state(self):
        return {"status": "ok", "data": self.snapshot()}

    async def web_action(self):
        from astrbot.api.web import request

        body = await request.json({})
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("m") or "")

        if mode == "shadow":
            want = bool(body.get("enabled"))
            try:
                save_value(self.config, "debug.shadow_mode", want)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{TAG} 保存影子模式失败：{exc!r}"[:200])
                return {"status": "error", "message": "保存配置失败，请到插件配置页修改"}
            # 落盘成功后才同步运行态副本（面板保存不重载插件）
            self.cfg.raw.setdefault("debug", {})["shadow_mode"] = want
            logger.info(f"{TAG} 主动发言影子 → {'开' if want else '关'}（来自面板）")
            return {"status": "ok", "data": {"shadow": want}}

        if mode == "group_toggle":
            # 面板群卡片上的每群启停：停用 = 记入 disabled_groups 并持久化。
            # config 负责落盘，cfg.raw 同步一份让本次运行立即生效（面板保存不重载插件）。
            gid = str(body.get("gid") or "").strip()
            want = bool(body.get("enabled"))
            if not gid:
                return {"status": "error", "message": "缺少 gid"}
            if gid not in self.configured_groups():
                return {"status": "error", "message": f"群 {gid} 不在 groups 配置里"}
            try:
                disabled = save_disabled_groups(self.config, gid, want)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{TAG} 保存每群启停失败：{exc!r}"[:200])
                return {"status": "error", "message": "保存配置失败，请到插件配置页修改"}
            # 落盘成功后才同步运行态副本，失败时运行行为保持原样（C2）
            self.cfg.raw["disabled_groups"] = list(disabled)
            logger.info(f"{TAG} 群 {gid} {'启用' if want else '停用'}（来自面板）")
            return {"status": "ok", "data": {"gid": gid, "enabled": want}}

        if mode == "dive":
            gid = str(body.get("gid") or "").strip()
            if not gid:
                return {"status": "error", "message": "缺少 gid"}
            self.presence.force_dive(gid, time.time())
            logger.info(f"{TAG} 群 {gid} 被手动打回潜水（来自面板）")
            return {"status": "ok", "data": {"gid": gid, "state": "dive"}}

        if mode == "unsleep":
            # 解除休眠：解开手动锁定 + 回潜水。留 MANUAL_AWAKE 是故意的——
            # 否则还在时间窗内的话，下一次心跳立刻又把它打回休眠，按钮等于没用。
            gid = str(body.get("gid") or "").strip()
            if not gid:
                return {"status": "error", "message": "缺少 gid"}
            now = time.time()
            self._manual_mode[gid] = MANUAL_AWAKE
            if self.presence.is_sleeping(gid):
                self.presence.exit_sleep(gid, now)
            self.presence.force_dive(gid, now)
            logger.info(f"{TAG} 群 {gid} 解除休眠，回潜水（来自面板）")
            return {
                "status": "ok",
                "data": {"gid": gid, "state": "dive"},
                "message": "已解除休眠，回到潜水",
            }

        if mode == "active":
            # 面板手动进活跃：不受冷却限制，发言仍受闸门约束（这里只改状态，不发消息）
            gid = str(body.get("gid") or "").strip()
            if not gid:
                return {"status": "error", "message": "缺少 gid"}
            now = time.time()
            if self._sleeping_now(gid):
                if self._manual_mode.get(gid) == MANUAL_SLEEP:
                    # 面板点「进活跃」等价 /onair：先解开手动休眠
                    self._manual_mode[gid] = MANUAL_AWAKE
                    if self.presence.is_sleeping(gid):
                        self.presence.exit_sleep(gid, now)
                else:
                    return {
                        "status": "error",
                        "message": (
                            f"现在是休眠时段（{self.presence.sleep_window_text()}），"
                            "进活跃会被下一次心跳打回；要用 /onair 强制唤醒或先关掉「休眠」"
                        ),
                    }
            action = self.presence.force_active(gid, now)
            if action == "asleep":
                return {"status": "error", "message": f"群 {gid} 还在休眠态，等窗口结束再试"}
            left = self.presence.remaining_sec(gid, now)
            tip = {
                "enter": "已进入活跃",
                "extend": "本来就在活跃，已加时",
                "capped": "已到活跃硬顶，不再加时",
            }[action]
            logger.info(
                f"{TAG} 群 {gid} 手动进活跃（来自面板）动作={action} 剩余={left}s"
            )
            return {
                "status": "ok",
                "data": {"gid": gid, "action": action, "remaining_sec": left},
                "message": f"{tip}，剩 {left // 60} 分钟",
            }

        if mode == "caption":
            # 只为验收：给一张图做转述，不落上下文、不发群
            url = str(body.get("url") or "").strip()
            if not url:
                return {"status": "error", "message": "缺少 url"}
            # 传 provider 可临时换模型做 A/B，且强制不走缓存
            override = str(body.get("provider") or "").strip()
            pid = override or self._vision_provider()
            provider = self.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                return {"status": "error", "message": f"视觉模型不可用：{pid or '(未配置)'}"}
            key = make_key("img", url)
            if not override:
                cached = self.vision_cache.get(key)
                if cached:
                    return {"status": "ok", "data": {"text": cached, "cached": True, "provider": pid}}
            src = self._img_paths.get(url) or url
            if src.startswith(("http://", "https://")):
                try:
                    from astrbot.core.utils.media_utils import MediaResolver

                    src = await MediaResolver(src, media_type="image").to_path()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{TAG} 试读图下载失败：{exc!r}"[:200])
            text = await caption(provider, src, timeout=20.0)
            if text and not override:
                self.vision_cache.set(key, text)
                self.vision_cache.save()
            logger.info(f"{TAG} 试读图 provider={pid} 结果={text[:60] or '(空)'}")
            return {"status": "ok", "data": {"text": text, "cached": False, "provider": pid}}

        if mode == "trace":
            # 取一条发言档案的详情；kb=true 时用当时的提问实时复算知识库
            try:
                want = float(body.get("ts") or 0)
            except Exception:  # noqa: BLE001
                return {"status": "error", "message": "ts 不是数字"}
            rec = None
            for t in reversed(self.traces.items):
                if abs(t.ts - want) < 1e-6:
                    rec = t
                    break
            if rec is None:
                return {"status": "error", "message": "这条档案已被覆盖（只保留最近 100 条）"}
            data = {
                "ts": rec.ts,
                "gid": rec.gid,
                "trigger": rec.trigger,
                "verdict": rec.verdict,
                "reason": rec.reason,
                "state": rec.state,
                "persona": rec.persona,
                "model": rec.model,
                "ctx_count": rec.ctx_count,
                "prompt": rec.prompt,
                "reply": rec.reply,
                "memories": rec.memories,
                "kb_names": rec.kb_names,
            }
            if body.get("kb") and rec.kb_names and rec.prompt:
                try:
                    from astrbot.core.tools.knowledge_base_tools import (
                        retrieve_knowledge_base,
                    )

                    data["kb_text"] = (
                        await retrieve_knowledge_base(
                            query=rec.prompt,
                            umo=rec.umo,
                            context=self.context,
                        )
                        or ""
                    )
                except Exception as exc:  # noqa: BLE001
                    data["kb_text"] = f"（复算失败：{exc!r}）"
                logger.info(f"{TAG} 面板复算知识库 群={rec.gid} 库={rec.kb_names}")
            return {"status": "ok", "data": data}

        if mode == "segment":
            # 只为验收：用当前会话的分段配置拆一段文本，看配置读没读对
            text = str(body.get("text") or "").strip()
            if not text:
                return {"status": "error", "message": "缺少 text"}
            umo = str(body.get("umo") or "").strip()
            if not umo:
                gid = str(body.get("gid") or "").strip()
                umo = self.capture.origin.get(gid, "") if gid else ""
            conf = self.context.get_config(umo=umo) if umo else self.context.get_config()
            sr = (conf.get("platform_settings") or {}).get("segmented_reply") or {}
            words = [str(w) for w in (sr.get("split_words") or []) if str(w)]
            from .core.segment import split_by_words

            parts = split_by_words(text, words)
            return {
                "status": "ok",
                "data": {
                    "umo": umo,
                    "enable": sr.get("enable"),
                    "split_mode": sr.get("split_mode"),
                    "split_words": words,
                    "threshold": sr.get("words_count_threshold"),
                    "only_llm_result": sr.get("only_llm_result"),
                    "parts": parts,
                },
            }

        if mode == "forward":
            # 只为验收：按转发 id 展开一条合并转发，不落上下文、不发群
            fid = str(body.get("id") or "").strip()
            if not fid:
                return {"status": "error", "message": "缺少 id（转发消息 id）"}
            gid = str(body.get("gid") or "").strip()
            if not gid:
                return {"status": "error", "message": "缺少 gid（用来取平台 bot 实例）"}
            if not self._bots.get(gid):
                return {
                    "status": "error",
                    "message": f"群 {gid} 还没有收到过消息，拿不到 bot 实例；先在群里发一条消息再试",
                }
            text, model = await self._expand_forward(gid, fid)
            logger.info(f"{TAG} 试展开转发 id={fid} 结果={text[:60] or '(空)'}")
            return {
                "status": "ok" if text else "error",
                "data": {"text": text, "model": model},
                "message": "" if text else "展开为空（接口不通或 id 无效）",
            }

        if mode == "stt":
            # 只为验收：给一段本地音频做转写，不落上下文、不发群
            path = str(body.get("path") or "").strip()
            if not path:
                return {"status": "error", "message": "缺少 path（本地音频绝对路径）"}
            gid = str(body.get("gid") or "").strip()
            text, model = await self._transcribe(gid, path)
            logger.info(f"{TAG} 试转写 模型={model or '(无)'} 结果={text[:60] or '(空)'}")
            return {
                "status": "ok" if text else "error",
                "data": {"text": text, "model": model},
                "message": "" if text else f"转写为空（模型={model or '未配置'}）",
            }

        if mode == "probe":
            # 只为验收：生成一句「冷场开话题」，不发送、不改状态
            gid = str(body.get("gid") or "").strip()
            if not gid:
                return {"status": "error", "message": "缺少 gid"}
            recent = self.buf.recent(gid, self.cfg.integer("context.ctx_messages", 30))
            umo = str(body.get("umo") or "").strip() or self.capture.origin.get(gid, "")
            await self._ensure_captions(gid, recent)
            provider = await self._chat_provider(umo)
            if not provider:
                return {"status": "error", "message": "没有可用模型"}
            persona_name, persona_mask = await self._persona(umo)
            text = await compose_probe(
                self.context,
                provider,
                self.cfg.text("decision.probe_prompt", ""),
                persona_name,
                persona_mask,
                recent,
                recent=self.cfg.integer("context.recent_split", 8),
                timeout=30.0,
            )
            logger.info(
                f"{TAG} 试开话题（面板）群={gid} 上下文={len(recent)} "
                f"人设={'有' if persona_mask else '无'} 模型={provider} 结果={text[:40] or '(空)'}"
            )
            return {"status": "ok", "data": {"text": text, "provider": provider}}

        return {"status": "error", "message": f"不认识的操作：{mode[:20]}"}

    async def _judge_persona(self, umo: str) -> tuple[str, str]:
        """判定专用人格：配置里填了就用它，留空则跟随会话人格。

        完整人格卡（外貌、梗表、名字来历）对「该不该接这句话」没帮助，
        但每次判定都要付一遍 token；这里给判定一条更短的通道。
        """
        if not self.cfg.bool("decision.use_persona", True):
            return "", ""
        name, mask = await self._persona(umo)
        return pick_judge_persona(
            self.cfg.text("decision.judge_persona", ""), name, mask
        )

    async def _persona(self, umo: str) -> tuple[str, str]:
        """取 (人格名, 人格正文)；关掉 use_persona 或取不到就返回空。0 字上限 = 不截断。"""
        if not self.cfg.bool("decision.use_persona", True) or not umo:
            return "", ""
        try:
            return await self.persona_cache.get(
                self.context, umo, self.cfg.integer("decision.persona_max_chars", 0)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 取人格失败：{exc!r}")
            return "", ""

    async def _judge_then_request(self, event: AstrMessageEvent, gid: str, *, force: bool = False):
        """判定要不要接话；要说就返回一个 ProviderRequest，交给 AstrBot 管线生成。

        返回 None 表示这次不说：节奏没到 / 概率未中 / 闸门拦下 / 判定 SKIP / 影子模式。
        走管线的好处：人格、对话历史、长期记忆、内置工具全部自动生效。
        """
        if not self.cfg.bool("enabled", True):
            return None
        if self._sleeping_now(gid) and not force:
            return None  # 休眠中不主动判定、不发言；被 @/引用/叫名字（force）除外
        if not self.cfg.bool("decision.judge_enabled", True):
            logger.info(f"{TAG} 判定 群={gid} 结论=SKIP 理由=判定功能已关闭")
            return None

        now = time.time()
        self.presence.refresh(gid, now)
        active = self.presence.is_active(gid)
        rt = self._rt_of(gid)
        if rt["busy"]:
            logger.debug(f"{TAG} 判定进行中，暂时忽略新消息 群={gid}")
            return None

        # 节奏：活跃态 30s 一次，潜水态 3min 一次；被叫到时（force）不受限
        interval = (
            self.cfg.num("state.active_tick_sec", 30.0)
            if active
            else self.cfg.num("state.dive_tick_min", 3.0) * 60.0
        )
        if not force and now - rt["last_judge_at"] < max(10.0, interval):
            # 静默吞消息会让用户以为插件坏了；按群限频打一条可解释的日志（B1）
            if now - rt.get("last_throttle_log_at", 0.0) >= 30.0:
                rt["last_throttle_log_at"] = now
                logger.info(
                    f"{TAG} 判定节流 群={gid}：距上次判定 {now - rt['last_judge_at']:.0f}s，"
                    f"当前状态每 {max(10.0, interval):.0f}s 最多判定一次（正常节奏，不是故障）"
                )
            return None

        st = self.presence.get(gid)
        gap = self.cfg.num("state.active_speak_gap_sec", 25.0)
        if not force:
            allowed, why = self.gate.allow(st, now)
            if not allowed:
                logger.info(f"{TAG} 判定前被闸门拦下 群={gid}（{why}）")
                return None

            if gap_blocks(False, rt.get("last_activity_at", 0.0), now, gap):
                rt["last_judge_at"] = now
                logger.info(f"{TAG} 距上次说话不足 {gap:.0f}s，本次不判 群={gid}")
                return None

        # 活跃态先掷概率；没过就不浪费一次模型调用。
        # 命中之后要不要再问一次判定模型，看 decision.judge_in_active：
        # 关掉 → 直接按概率接话（省一次调用，也不用听模型反复说「刚刚说过话」）；
        # 打开 → 仍先过模型再决定。
        prob = self.cfg.num("decision.speak_prob_active", 0.35)
        roll = random.random()
        mode = "judge"
        if active and not force:
            mode = pick_active_action(
                roll, prob, self.cfg.bool("decision.judge_in_active", True)
            )
            if mode == "skip_prob":
                rt["last_judge_at"] = now
                logger.info(
                    f"{TAG} 判定 群={gid} 状态=活跃 结论=SKIP "
                    f"理由=概率未中（{roll:.2f}/{prob:.2f}）"
                )
                return None

        rt["busy"] = True
        rt["last_judge_at"] = now
        seq_at_start = int(rt.get("trigger_seq", 0))
        try:
            pending = self.buf.after(gid, rt["watermark"])
            min_new = self.cfg.integer("state.dive_min_new_msgs", 5)
            if not active and not force and len(pending) < min_new:
                logger.info(
                    f"{TAG} 判定 群={gid} 状态=潜水 结论=SKIP "
                    f"理由=新消息不足（{len(pending)}/{min_new}）"
                )
                return None
            recent = self.buf.recent(gid, self.cfg.integer("context.ctx_messages", 30))
            await self._ensure_captions(gid, recent)
            umo = str(getattr(event, "unified_msg_origin", "") or "") or self.capture.origin.get(
                gid, ""
            )
            provider = ""
            persona_name, persona_mask = "", ""
            if mode == "reply_prob":
                # 活跃态且关掉「引入判定模型」：概率命中就直接接话，
                # 不再问一次模型（省调用，也避免模型反复用「刚刚说过话」否掉）
                verdict = Verdict(True, "概率命中（未引入判定模型）", "", 0)
            else:
                provider = await self._judge_provider(umo)
                if not provider:
                    if self._warned_no_judge_provider.take():
                        logger.warning(
                            f"{TAG} 没有可用的判定模型：配置的判定模型不可用"
                            "（或 decision.judge_provider_id 为空），且当前会话也取不到对话模型。"
                            "请在插件配置里选择一个可用的判定模型，或先给会话配置对话模型；"
                            "在此之前不会主动接话。"
                        )
                    return None
                persona_name, persona_mask = await self._judge_persona(umo)
                hint = build_hint(pending, self.cfg.text("decision.bot_names", ""))
                verdict = await judge(
                    self.context,
                    provider,
                    self.cfg.text("decision.judge_prompt", ""),
                    self.cfg.text("decision.bot_names", ""),
                    recent,
                    persona_name=persona_name,
                    persona_mask=persona_mask,
                    hint=hint,
                    recent=self.cfg.integer("context.recent_split", 8),
                    require_reason=self.cfg.bool("decision.require_reason", True),
                    timeout=40.0,
                )
                self._judged_total += 1
            self._last_verdict[gid] = verdict
            self._pending_verdict[gid] = (bool(verdict.yes), verdict.reason, time.time())
            if persona_name:
                self._last_persona[gid] = persona_name
            shadow = self.cfg.bool("debug.shadow_mode", True)
            logger.info(
                f"{TAG} 判定 群={gid} 状态={'活跃' if active else '潜水'} 新消息={len(pending)} "
                f"结论={'REPLY' if verdict.yes else 'SKIP'} "
                f"理由={verdict.reason} 耗时={verdict.elapsed_ms}ms "
                f"模型={provider or '(未调用)'} 影子={shadow} "
                f"人设={persona_name or '无'}({len(persona_mask)}字)"
                + ("（输出解析异常）" if verdict.unparsed else "")
            )
            self._verdict_log.append(
                {
                    "ts": time.time(),
                    "gid": gid,
                    "state": "active" if active else "dive",
                    "verdict": "REPLY" if verdict.yes else "SKIP",
                    "reason": verdict.reason,
                    "ms": verdict.elapsed_ms,
                    "model": provider,
                    "shadow": shadow,
                }
            )
            if len(self._verdict_log) > 200:
                del self._verdict_log[: -200]
            if not verdict.yes:
                return None

            self._replied_total += 1
            if shadow:
                logger.info(f"{TAG} 判定 REPLY（主动发言影子，本次不主动说话）群={gid}")
                return None

            # 判定本身要 1~3 秒，这期间可能已经有人 @ 了它。
            # 这里必须重新过一次闸门，否则两条回复会同时生成、同时发出，
            # 群里看到的就是「说到一半插进来一句不相干的」。
            now2 = time.time()
            if rt.get("trigger_seq", 0) != seq_at_start:
                logger.info(f"{TAG} 判定通过，但期间有人叫我，放弃这次主动接话 群={gid}")
                return None
            allowed2, why2 = True, ""
            if not force:
                allowed2, why2 = self.gate.allow(st, now2)
            if not force and (
                not allowed2
                or gap_blocks(False, rt.get("last_activity_at", 0.0), now2, gap)
            ):
                logger.info(
                    f"{TAG} 判定通过，但期间已经说过话，放弃这次主动接话 群={gid}（{why2 or 'gap'}）"
                )
                return None

            prompt = str(getattr(event, "message_str", "") or "").strip()
            if not prompt:
                logger.info(f"{TAG} 最新消息没有文字内容，不主动接 群={gid}")
                return None

            if pending:
                rt["watermark"] = max(rt["watermark"], pending[-1].seq)
            conv = await self._current_conversation(umo)
            if not force:
                # force（叫名字）在触发阶段已经记过一次发言配额，这里不再重复记
                self.gate.record(st, time.time())
            rt["last_activity_at"] = time.time()
            # 打上标记：发送前若 trigger_seq 变了（又有人叫我），这条会被丢弃
            event.set_extra(EXTRA_PROACTIVE, True)
            event.set_extra(EXTRA_TRIGGER_SEQ, seq_at_start)
            logger.info(
                f"{TAG} 主动发言（走管线）群={gid} 状态={'活跃' if active else '潜水'} "
                f"上下文={len(recent)} 会话={'有' if conv else '无'} 触发={prompt[:30]}"
            )
            return event.request_llm(prompt=prompt, conversation=conv)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 判定异常 群={gid}：{exc!r}"[:300])
            return None
        finally:
            rt["busy"] = False

    async def _current_conversation(self, umo: str):
        """取该群当前的对话；取不到就返回 None（管线会自己处理）。"""
        if not umo:
            return None
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return None
            return await self.context.conversation_manager.get_conversation(umo, cid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 取会话失败：{exc!r}")
            return None

    # ───────── M4：冷场开话题 ─────────

    def _probe_tick(self, gid: str, now: float) -> None:
        """维护 probing 观察窗口：有人接话回活跃，到点没人理回潜水。"""
        last_user = self.capture.last_seen.get(gid, 0.0)
        moved = self.presence.tick_probe(gid, now, last_user)
        if moved == "answered":
            logger.info(
                f"{TAG} 有人接话，回到活跃 群={gid} "
                f"剩余={self.presence.remaining_sec(gid, now)}s"
            )
        elif moved == "timeout":
            st = self.presence.get(gid)
            left = max(0, int(st.cooldown_until - now))
            logger.info(
                f"{TAG} 抛的话题没人接，回潜水 群={gid} 冷却 {left}s（这期间 @ 只回一条）"
            )

    def _maybe_probe(self, gid: str, now: float) -> None:
        """活跃且静默够久时，决定要不要抛一句话题。"""
        if not self.cfg.bool("enabled", True):
            return
        if self._sleeping_now(gid):
            return
        if not self.cfg.bool("probe.enabled", False):
            return
        rt = self._rt_of(gid)
        if rt["busy"]:
            return

        st = self.presence.get(gid)
        idle_sec = max(60.0, self.cfg.num("probe.probe_idle_min", 10.0) * 60.0)
        cd_sec = max(60.0, self.cfg.num("probe.probe_cooldown_min", 15.0) * 60.0)
        last_user = self.capture.last_seen.get(gid, 0.0)
        ok, why = self.presence.can_probe(
            gid, now, idle_sec=idle_sec, cooldown_sec=cd_sec, last_user_at=last_user
        )
        if not ok:
            return

        # 进度上：只有活跃态能开话题（潜水态永远不主动说话）
        allowed, why2 = self.gate.allow(st, now)
        if not allowed:
            return
        if self.gate.count_last_hour(st, now) >= self.cfg.integer(
            "probe.lines_per_hour", 6
        ):
            return
        gap = self.cfg.num("state.active_speak_gap_sec", 25.0)
        if not self.gate.gap_ok(rt.get("last_activity_at", 0.0), now, gap):
            return

        prob = self.cfg.num("probe.probe_prob", 0.6)
        roll = random.random()
        # 掷一次就算一次尝试：没中也等冷却，避免每秒重掷、日志刷屏
        st.last_probe_at = now
        if roll >= prob:
            logger.info(
                f"{TAG} 冷场可开话题，但概率未中（{roll:.2f}/{prob:.2f}）群={gid}"
            )
            return

        rt["busy"] = True
        task = asyncio.create_task(self._run_probe(gid, now), name=f"probe_{gid}")
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _run_probe(self, gid: str, now: float) -> None:
        """生成并发送一句「开话题」。没有事件可用，只能直接发。"""
        rt = self._rt_of(gid)
        try:
            umo = self.capture.origin.get(gid, "")
            if not umo:
                logger.warning(f"{TAG} 开话题 群={gid} 跳过：没有会话 umo")
                return
            provider = await self._chat_provider(umo)
            if not provider:
                logger.warning(f"{TAG} 开话题 群={gid} 跳过：没有可用模型")
                return
            persona_name, persona_mask = await self._persona(umo)
            recent = self.buf.recent(gid, self.cfg.integer("context.ctx_messages", 30))
            await self._ensure_captions(gid, recent)
            text = await compose_probe(
                self.context,
                provider,
                self.cfg.text("decision.probe_prompt", ""),
                persona_name,
                persona_mask,
                recent,
                recent=self.cfg.integer("context.recent_split", 8),
                timeout=30.0,
            )
            if not text:
                logger.info(f"{TAG} 开话题生成失败，本次放弃 群={gid}")
                return

            if self.cfg.bool("debug.shadow_mode", True):
                logger.info(f"{TAG} 冷场开话题（主动发言影子，本次不主动说话）群={gid}：{text[:40]}")
                return

            # probe 不走 AstrBot 管线，没有 ResultDecorateStage 兜着：
            # 按同一份会话分段配置自己拆、逐条发，否则分段词会原样漏进群。
            parts = self._probe_segments(umo, text)
            sent = 0
            for i, part in enumerate(parts):
                if i:
                    await asyncio.sleep(SEGMENT_GAP_SEC)
                if not await send_text(self.context, umo, part):
                    logger.info(
                        f"{TAG} 开话题发送失败 群={gid}（第 {i + 1}/{len(parts)} 段）"
                    )
                    break
                sent += 1
            if not sent:
                return

            ts = time.time()
            wait_min = max(0.5, self.cfg.num("probe.probe_wait_min", 4.0))
            self.presence.start_probe(gid, ts, wait_min * 60.0)
            self.gate.record(self.presence.get(gid), ts)
            rt["last_activity_at"] = ts
            rt["last_judge_at"] = ts
            self.buf.add(
                gid,
                role="assistant",
                name=self.cfg.text("decision.bot_names", ""),
                uid="bot",
                text=text,
            )
            logger.info(
                f"{TAG} 冷场开话题 群={gid} 观察 {wait_min:.0f} 分钟 "
                f"分 {sent}/{len(parts)} 段：{text[:40]}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 开话题异常 群={gid}：{exc!r}"[:300])
        finally:
            rt["busy"] = False

    def _probe_segments(self, umo: str, text: str) -> list[str]:
        """开话题按会话的分段配置切成几段（probe 不走管线，得自己拆）。"""
        try:
            conf = (
                self.context.get_config(umo=umo) if umo else self.context.get_config()
            )
            sr = (conf.get("platform_settings") or {}).get("segmented_reply") or {}
            words = [str(w) for w in (sr.get("split_words") or []) if str(w)]
            limit = int(sr.get("words_count_threshold") or 150)
            return plan_segments(
                text, words, enable=bool(sr.get("enable")), limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 读分段配置失败：{exc!r}")
            return [text]

    async def _chat_provider(self, umo: str) -> str:
        """开话题用主对话模型（它是要给人看的），取不到再退回判定模型。"""
        if umo:
            try:
                pid = await self.context.get_current_chat_provider_id(umo)
                if pid:
                    return str(pid)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{TAG} 取会话模型失败：{exc!r}")
        return await self._judge_provider(umo)

    # ───────── M5：读图 ─────────

    async def _collect_media(self, event) -> tuple[list[str], list[str], list[str], int]:
        """收图片、语音、合并转发。返回 (图片地址, 语音标识, 转发 id, 跳过的表情数)。

        图片缓存 key 用原始 URL（稳定）；交给模型的是本地路径（部分视觉模型直接吃
        远程 URL 会卡住或超时）。语音同理，且必须本地下载后才能送 STT。

        表情包默认跳过：群里相当比例的「图」是收藏表情，转述价值低、数量大。
        关掉 `context.skip_stickers` 就恢复全部转述。
        """
        try:
            chain = event.get_messages() or []
        except Exception:  # noqa: BLE001
            chain = []

        urls: list[str] = []
        stickers = 0
        if media_enabled(self.cfg, "images"):
            skip_sticker = self.cfg.bool("context.skip_stickers", True)
            try:
                segs = image_segments(event)
            except Exception:  # noqa: BLE001
                segs = []
            for url, is_sticker in segs:
                if is_sticker and skip_sticker:
                    stickers += 1
                    continue
                urls.append(url)
            urls = urls[:4]
            if stickers:
                self._sticker_skipped += stickers

        # 只给「要转述的」图片找本地文件，避免为表情白下载
        if urls:
            wanted = set(urls)
            for comp in chain:
                if type(comp).__name__ != "Image":
                    continue
                url = str(
                    getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                ).strip()
                if url not in wanted:
                    continue
                try:
                    path = await comp.convert_to_file_path()
                except Exception:  # noqa: BLE001
                    path = ""
                if path:
                    self._img_paths[url] = path
            if len(self._img_paths) > 400:
                for key in list(self._img_paths)[:200]:
                    self._img_paths.pop(key, None)

        # 语音：只收前 2 段，避免一条长语音刷屏时把队列塞满
        voices: list[str] = []
        if media_enabled(self.cfg, "voice"):
            for comp in chain:
                if type(comp).__name__ != "Record":
                    continue
                ident = str(
                    getattr(comp, "url", "")
                    or getattr(comp, "file", "")
                    or getattr(comp, "path", "")
                    or ""
                ).strip()
                if not ident:
                    continue
                try:
                    path = await comp.convert_to_file_path()
                except Exception:  # noqa: BLE001
                    path = ""
                if path:
                    self._voice_paths[ident] = path
                    voices.append(ident)
            voices = voices[:2]

        # 合并转发：只收 id，内容在后台调 get_forward_msg 展开（要联网，不能在这里等）
        forwards: list[str] = []
        if media_enabled(self.cfg, "forward"):
            try:
                forwards = forward_ids(event)[:2]
            except Exception:  # noqa: BLE001
                forwards = []
        return urls, voices, forwards, stickers

    def _vision_provider(self) -> str:
        """视觉模型：配置里选了就用，留空跟随 AstrBot 的图片转述模型。"""
        pid = self.cfg.text("context.vision_provider_id", "").strip()
        if pid:
            return pid
        try:
            conf = self.context.get_config()
            return str(
                conf["provider_settings"].get("default_image_caption_provider_id") or ""
            ).strip()
        except Exception:  # noqa: BLE001
            return ""

    def _enqueue_captions(self, gid: str, msg) -> None:
        """把这条消息里的图片 / 语音丢进后台队列（不等待）。

        三种情况直接跳过：
        - 关掉了异步（异步关掉时走 _ensure_captions 的同步老路）
        - 已在缓存里（立刻写回消息，连队列都不用进）
        - 同一个标识已经在排队/处理中（重复图/语音不重复花钱）
        """
        if not self.cfg.bool("context.caption_async", True):
            return
        q = self._caption_q
        max_q = max(1, self.cfg.integer("context.caption_queue_max", 12))

        def push(kind: str, ref: str, cache) -> None:
            key = make_key(kind, ref)
            cached = cache.get(key)
            if cached:
                apply_caption(msg, cached, kind=kind)
                return
            if key in self._caption_inflight:
                return
            if q is None or q.qsize() >= max_q:
                # 队列积压（群里刷图/刷语音）→ 留着占位符，不烧钱
                self._caption_dropped += 1
                return
            self._caption_inflight.add(key)
            try:
                q.put_nowait((kind, gid, msg, ref, key))
            except Exception:  # noqa: BLE001
                self._caption_inflight.discard(key)

        if media_enabled(self.cfg, "images"):
            for url in list(getattr(msg, "images", None) or []):
                push("img", url, self.vision_cache)

        # 语音只在正文还是占位符时才转写：被 @ 的语音已由 AstrBot 预处理转成文字，
        # 再转一遍等于白花钱。
        if media_enabled(self.cfg, "voice"):
            if (msg.text or "").strip() in ("", "[语音]", "[非文字消息]"):
                for ident in list(getattr(msg, "voices", None) or []):
                    push("voice", ident, self.voice_cache)

        # 合并转发同理：只在正文还是占位符时展开
        if media_enabled(self.cfg, "forward"):
            if (msg.text or "").strip() in ("", "[合并转发]", "[非文字消息]"):
                for fid in list(getattr(msg, "forwards", None) or []):
                    push("forward", fid, self.forward_cache)

    async def _caption_loop(self) -> None:
        """后台串行转述（图片转述 / 语音转写）。

        串行是为了避免把视觉与 STT 接口打爆（并发调用容易大量超时）。
        """
        while self._alive:
            q = self._caption_q
            if q is None:
                return
            try:
                kind, gid, msg, ref, key = await q.get()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
                continue
            try:
                if kind == "voice":
                    text, model = await self._transcribe(gid, ref)
                elif kind == "forward":
                    text, model = await self._expand_forward(gid, ref)
                else:
                    text, model = await self._describe_image(ref)
                if text:
                    if kind == "voice":
                        cache = self.voice_cache
                    elif kind == "forward":
                        cache = self.forward_cache
                    else:
                        cache = self.vision_cache
                    cache.set(key, text)
                    cache.save()
                    # 消息可能已被环形缓冲挤掉，写回失败也无所谓
                    if kind == "forward":
                        apply_forward(msg, text)
                    else:
                        apply_caption(msg, text, kind=kind)
                    self._caption_done += 1
                    label = {
                        "voice": "语音转写",
                        "forward": "转发展开",
                    }.get(kind, "预转述")
                    logger.info(
                        f"{TAG} {label} 完成 模型={model} 累计 {self._caption_done} 条 "
                        f"队列 {q.qsize()} 条 · {text[:40].replace(chr(10), ' ')}"
                    )
                else:
                    # 失败也清空：否则每次判定都重试同一条
                    if kind == "voice":
                        msg.voices = []
                    elif kind == "forward":
                        msg.forwards = []
                    else:
                        msg.images = []
                    logger.info(f"{TAG} 预转述 失败（{kind}，已放弃这条）队列 {q.qsize()} 条")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{TAG} 预转述异常：{exc!r}")
            finally:
                self._caption_inflight.discard(key)
                try:
                    q.task_done()
                except Exception:  # noqa: BLE001
                    pass

    def _warn_missing_vision(self, provider_id: str) -> None:
        """读图开着却取不到视觉模型：同步/异步两条路径共用，只告警一次。"""
        if not self._warned_no_vision_provider.take():
            return
        logger.warning(
            f"{TAG} 已开启读图，但取不到视觉模型（{provider_id or '未配置'}）。"
            "图片将保持 [图片] 占位符：请在 AstrBot 配置图片转述模型，"
            "或在插件配置 context.vision_provider_id 指定。"
        )

    async def _describe_image(self, url: str) -> tuple[str, str]:
        """转述一张图。返回 (描述, 使用的模型)；失败返回 ("", 模型)。"""
        provider_id = self._vision_provider()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        if provider is None:
            self._warn_missing_vision(provider_id)
            return "", provider_id
        text = await caption(provider, self._img_paths.get(url) or url, timeout=20.0)
        return (text or ""), provider_id

    async def _stt_provider(self, gid: str):
        """语音转写模型：配置里选了就按 id 取，留空跟随 AstrBot 的 STT 设置。"""
        pid = self.cfg.text("context.stt_provider_id", "").strip()
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider is not None:
                return provider
            logger.warning(f"{TAG} 语音转写跳过：STT 模型 {pid} 不存在")
            return None
        umo = self.capture.origin.get(gid, "")
        try:
            return await self.context.get_using_stt_provider_async(umo)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 取 STT 模型失败：{exc!r}")
            return None

    async def _transcribe(self, gid: str, ident: str) -> tuple[str, str]:
        """把一段语音转成文字。返回 (文字, 模型名)；失败返回 ("", 模型名)。

        语音转写耗时取决于模型和网络，通常明显慢于图片转述。后台队列是串行的，
        所以这里必须设上限：一条卡住的语音不能把后面的图片也堵死。
        """
        provider = await self._stt_provider(gid)
        if provider is None:
            if self._warned_no_stt_provider.take():
                logger.warning(
                    f"{TAG} 已开启读语音，但取不到 STT 模型。语音将保持 [语音] 占位符："
                    "请在 AstrBot 配置 STT 模型，或在插件配置 context.stt_provider_id 指定。"
                )
            return "", ""
        model = str(getattr(provider, "provider_config", {}).get("id") or "") or "STT"
        path = self._voice_paths.get(ident) or ident
        # QQ 语音是 silk/amr，STT 接口只吃 wav；也是当初装 ffmpeg 的原因
        try:
            from astrbot.core.utils.media_utils import ensure_wav

            path = await ensure_wav(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{TAG} 语音转 wav 失败，直接送原始文件：{exc!r}")
        timeout = max(5.0, self.cfg.num("context.stt_timeout_sec", 60.0))
        started = time.time()
        try:
            text = await asyncio.wait_for(
                provider.get_text(audio_url=path), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"{TAG} 语音转写超时（>{timeout:.0f}s），放弃这条")
            return "", model
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 语音转写失败：{exc!r}"[:200])
            return "", model
        text = " ".join(str(text or "").split())
        logger.info(
            f"{TAG} 语音转写 耗时 {time.time() - started:.1f}s 模型={model} "
            f"长度 {len(text)} 字"
        )
        return text[:300], model

    async def _expand_forward(self, gid: str, fid: str) -> tuple[str, str]:
        """调平台的 get_forward_msg 展开一条合并转发。

        参数名各家不同（go-cqhttp 用 `id`，部分实现用 `message_id`），所以两个
        都试一遍。失败/超时都返回空串：正文保留 `[合并转发]`，判定照常进行。
        """
        bot = self._bots.get(gid)
        if bot is None:
            logger.debug(f"{TAG} 转发展开跳过：拿不到群 {gid} 的 bot 实例")
            return "", ""

        timeout = max(2.0, self.cfg.num("context.forward_timeout_sec", 8.0))
        payload = None
        last_error = ""
        for params in ({"id": fid}, {"message_id": fid}):
            try:
                payload = await asyncio.wait_for(
                    bot.call_action("get_forward_msg", **params), timeout=timeout
                )
            except asyncio.TimeoutError:
                last_error = f"超时（>{timeout:.0f}s）"
                payload = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)[:120]
                payload = None
                continue
            if payload:
                break

        if not payload:
            logger.info(f"{TAG} 转发展开失败（{last_error or '空响应'}）群={gid}")
            return "", ""

        max_items = max(1, self.cfg.integer("context.forward_max_items", 20))
        max_chars = max(100, self.cfg.integer("context.forward_max_chars", 600))
        text = parse_forward(payload, max_items=max_items, max_chars=max_chars)
        if not text:
            logger.info(f"{TAG} 转发展开完成但没解析出内容 群={gid}")
            return "", ""
        logger.info(
            f"{TAG} 转发展开 群={gid} 条数上限={max_items} 结果 {len(text)} 字"
        )
        return text, "get_forward_msg"

    async def _ensure_captions(self, gid: str, msgs: list) -> None:
        """把已知的描述写回消息。

        默认（`context.caption_async=true`）只读缓存，不发网络请求——真正的转述
        在后台 `_caption_loop` 里跑，判定不该为识图等待。

        关掉异步开关则退回原来的同步行为（当场转述，判定会变慢但这一轮就能看到图）。
        """
        if not (
            media_enabled(self.cfg, "images")
            or media_enabled(self.cfg, "voice")
            or media_enabled(self.cfg, "forward")
        ):
            return
        pending = [
            m
            for m in msgs
            if getattr(m, "images", None)
            or getattr(m, "voices", None)
            or getattr(m, "forwards", None)
        ]
        if not pending:
            return

        if self.cfg.bool("context.caption_async", True):
            hit = 0
            for msg in pending:
                if media_enabled(self.cfg, "images") and msg.images:
                    cached = self.vision_cache.get(make_key("img", msg.images[0]))
                    if cached:
                        apply_caption(msg, cached)
                        hit += 1
                if media_enabled(self.cfg, "voice") and msg.voices:
                    cached = self.voice_cache.get(make_key("voice", msg.voices[0]))
                    if cached:
                        apply_caption(msg, cached, kind="voice")
                        hit += 1
                if media_enabled(self.cfg, "forward") and msg.forwards:
                    cached = self.forward_cache.get(make_key("forward", msg.forwards[0]))
                    if cached:
                        apply_forward(msg, cached)
                        hit += 1
            if hit:
                logger.debug(f"{TAG} 读图/语音/转发 群={gid} 从缓存补上 {hit} 条")
            return

        # ── 同步老路（caption_async=false）：转发 → 语音 → 图片 ──
        if media_enabled(self.cfg, "forward"):
            for msg in pending:
                for fid in list(msg.forwards or []):
                    key = make_key("forward", fid)
                    cached = self.forward_cache.get(key)
                    if not cached:
                        cached, _model = await self._expand_forward(gid, fid)
                        if cached:
                            self.forward_cache.set(key, cached)
                    if cached:
                        apply_forward(msg, cached)
                    else:
                        msg.forwards = []
            self.forward_cache.save()

        if media_enabled(self.cfg, "voice"):
            for msg in pending:
                for ident in list(msg.voices or []):
                    key = make_key("voice", ident)
                    cached = self.voice_cache.get(key)
                    if not cached:
                        cached, _model = await self._transcribe(gid, ident)
                        if cached:
                            self.voice_cache.set(key, cached)
                    if cached:
                        apply_caption(msg, cached, kind="voice")
                    else:
                        msg.voices = []
            self.voice_cache.save()

        if not media_enabled(self.cfg, "images"):
            return
        pending = [m for m in pending if getattr(m, "images", None)]
        if not pending:
            return
        budget = max(0, self.cfg.integer("context.images_per_tick", 3))
        if budget <= 0:
            return
        provider_id = self._vision_provider()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        if provider is None:
            # 同步路径（caption_async=false）也要给出同一条可操作告警（C4）
            self._warn_missing_vision(provider_id)
            return

        hit = 0
        todo: list[tuple[object, str, str]] = []
        for msg in pending[-budget:]:
            url = msg.images[0]
            key = make_key("img", url)
            cached = self.vision_cache.get(key)
            if cached:
                apply_caption(msg, cached)
                hit += 1
            else:
                todo.append((msg, url, key))

        new = 0
        if todo:
            results = await asyncio.gather(
                *(
                    caption(provider, self._img_paths.get(url) or url, timeout=15.0)
                    for _, url, _ in todo
                ),
                return_exceptions=True,
            )
            for (msg, url, key), text in zip(todo, results):
                if isinstance(text, str) and text.strip():
                    apply_caption(msg, text)
                    self.vision_cache.set(key, text)
                    new += 1
                else:
                    # 失败也清空：否则每一轮都重试同一张图，白烧钱
                    msg.images = []
        self.vision_cache.save()
        if new or hit:
            logger.info(
                f"{TAG} 读图 群={gid} 新转述 {new} 张 缓存命中 {hit} 张 "
                f"模型={provider_id} 缓存共 {len(self.vision_cache)} 条 "
                f"累计跳过表情 {self._sticker_skipped} 张"
            )

    # ───────── 群范围 ─────────

    def configured_groups(self) -> list[str]:
        """配置里填了的群号（不管是否被停用），给面板群列表和启动提示用。"""
        return resolve_groups(self.cfg.items("groups"), [])

    def target_groups(self) -> list[str]:
        """当前实际生效的群：groups 配置去掉面板上停用（disabled_groups）的。"""
        if not self.cfg.bool("enabled", True):
            return []
        return resolve_groups(self.cfg.items("groups"), self.cfg.items("disabled_groups"))

    # ───────── 指令（v0.19.0，按需求文件重写） ─────────

    @staticmethod
    def _fmt_left(sec: int) -> str:
        """把剩余秒数说成人话：<60 秒说秒，否则说分。"""
        sec = max(0, int(sec))
        return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"

    def _wake_prefixes(self) -> list[str]:
        """全局唤醒前缀（配置里是列表，兜底 /）。"""
        try:
            conf = self.context.get_config()
            wp = conf.get("wake_prefix")
            if isinstance(wp, list):
                return [str(x) for x in wp if str(x)]
            return [str(wp)] if wp else []
        except Exception:  # noqa: BLE001
            return ["/"]

    def _is_command(self, event: AstrMessageEvent) -> bool:
        """这条消息是不是本插件的指令。"""
        try:
            return is_plugin_command(
                str(getattr(event, "message_str", "") or ""), self._wake_prefixes()
            )
        except Exception:  # noqa: BLE001
            return False

    def _cmd_group(self, event: AstrMessageEvent) -> tuple[str, str]:
        """取当前群号；不合法/不受管返回空 gid + 提示语。"""
        gid = str(event.get_group_id() or "").strip()
        if not gid:
            return "", f"{TAG} 这条指令要在群里发"
        if gid not in set(self.target_groups()):
            return "", f"{TAG} 群 {gid} 不在话匣子管理范围"
        return gid, ""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("chatterbox")
    async def cmd_chatterbox(self, event: AstrMessageEvent):
        """当前群的心跳数 + 状态；活跃附带剩余时长。"""
        gid, err = self._cmd_group(event)
        if err:
            yield event.plain_result(err)
            return
        now = time.time()
        self.presence.refresh(gid, now)
        st = self.presence.get(gid)
        manual = self._manual_mode.get(gid, "")
        if self._sleeping_now(gid):
            why = "手动锁定，发 /onair 或 /lurk 退出" if manual == MANUAL_SLEEP else self.presence.sleep_window_text()
            state = f"休眠（{why}）"
        elif st.state == "probing":
            state = f"活跃·等人接话，剩 {self._fmt_left(self.presence.remaining_sec(gid, now))}"
        elif st.state == "active":
            state = f"活跃，剩 {self._fmt_left(self.presence.remaining_sec(gid, now))}"
        else:
            state = "潜水"
        yield event.plain_result(f"{TAG} 群 {gid}\n心跳：{self._ticks} 次\n状态：{state}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("onair")
    async def cmd_onair(self, event: AstrMessageEvent):
        """进入活跃：解除休眠，开始按活跃节奏看群。"""
        gid, err = self._cmd_group(event)
        if err:
            yield event.plain_result(err)
            return
        now = time.time()
        self._manual_mode[gid] = MANUAL_AWAKE
        if self.presence.is_sleeping(gid):
            self.presence.exit_sleep(gid, now)
        action = self.presence.force_active(gid, now)
        left = self.presence.remaining_sec(gid, now)
        tip = {
            "enter": "已进入活跃",
            "extend": "本来就在活跃，已加时",
            "capped": "已到活跃硬顶，不再加时",
        }[action]
        logger.info(f"{TAG} 指令 onair 群={gid} 动作={action} 剩余={left}s")
        yield event.plain_result(f"{TAG} 群 {gid} {tip}，剩 {self._fmt_left(left)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("lurk")
    async def cmd_lurk(self, event: AstrMessageEvent):
        """回到潜水：不主动接话，被 @/引用照回。"""
        gid, err = self._cmd_group(event)
        if err:
            yield event.plain_result(err)
            return
        now = time.time()
        self._manual_mode[gid] = MANUAL_AWAKE  # 醒着，但不活跃
        if self.presence.is_sleeping(gid):
            self.presence.exit_sleep(gid, now)
        self.presence.force_dive(gid, now)
        cd = max(0, int(self.presence.get(gid).cooldown_until - now))
        logger.info(f"{TAG} 指令 lurk 群={gid} 冷却={cd}s")
        yield event.plain_result(
            f"{TAG} 群 {gid} 已回到潜水（被 @/引用照回；{self._fmt_left(cd)} 内不再进活跃）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("offair")
    async def cmd_offair(self, event: AstrMessageEvent):
        """进入休眠：一直保持，直到手动发 /onair 或 /lurk。"""
        gid, err = self._cmd_group(event)
        if err:
            yield event.plain_result(err)
            return
        self._manual_mode[gid] = MANUAL_SLEEP
        self.presence.enter_sleep(gid)
        logger.info(f"{TAG} 指令 offair 群={gid} 手动锁休眠")
        yield event.plain_result(
            f"{TAG} 群 {gid} 已进入休眠：不判定、不主动说话、被 @ 照回；"
            "时间窗不会自动解除，发 /onair 或 /lurk 才退出"
        )
