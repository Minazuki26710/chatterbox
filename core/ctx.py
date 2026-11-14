# -*- coding: utf-8 -*-
"""上下文缓冲与渲染。纯逻辑，不 import astrbot，可单独单测。

设计要点：
- 每个群一个环形缓冲，seq 全局单调递增，用「水位线」取增量，避免同一条消息被判定两次。
- 只存文本；图片/语音/转发等非文本内容在第一版以占位符记录（M5 再接多模态）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Msg:
    seq: int
    role: str  # user | assistant
    name: str
    uid: str
    text: str
    ts: float
    flag: str = ""  # 触发标记：叫我 / 提到名字，渲染成 [叫我]
    # 这张消息里带的图片 URL。转述成功后会清空，并把描述写进 text，避免重复调用视觉模型。
    images: list[str] = field(default_factory=list)
    # 语音片段标识（URL 或本地路径）。转写成功后清空，正文变 [语音: 文字]。
    voices: list[str] = field(default_factory=list)
    # 合并转发的 id。展开成功后清空，正文变成 [转发 N 条] + 逐条内容。
    forwards: list[str] = field(default_factory=list)


class CtxBuffer:
    def __init__(self, maxlen_per_group: int = 120):
        self._buf: dict[str, deque[Msg]] = {}
        self._maxlen = max(10, int(maxlen_per_group))
        self._seq = 0

    def add(
        self,
        gid: str,
        *,
        role: str,
        name: str,
        uid: str,
        text: str,
        ts: float | None = None,
        flag: str = "",
        images: list[str] | None = None,
        voices: list[str] | None = None,
        forwards: list[str] | None = None,
    ) -> Msg:
        self._seq += 1
        msg = Msg(
            self._seq,
            role,
            name,
            uid,
            text,
            ts or time.time(),
            flag,
            list(images or []),
            list(voices or []),
            list(forwards or []),
        )
        self._buf.setdefault(gid, deque(maxlen=self._maxlen)).append(msg)
        return msg

    def recent(self, gid: str, limit: int) -> list[Msg]:
        buf = self._buf.get(gid)
        if not buf:
            return []
        return list(buf)[-max(1, int(limit)) :]

    def after(self, gid: str, seq: int) -> list[Msg]:
        """取水位线之后的新消息（不含 bot 自己发的）。"""
        buf = self._buf.get(gid)
        if not buf:
            return []
        return [m for m in buf if m.seq > seq and m.role != "assistant"]

    def last_seq(self, gid: str) -> int:
        buf = self._buf.get(gid)
        return buf[-1].seq if buf else 0

    def forget(self, gid: str) -> None:
        self._buf.pop(gid, None)


PLACEHOLDER_TEXTS = ("", "[图片]", "[语音]", "[视频]", "[文件]", "[合并转发]")


def apply_caption(msg: Msg, text: str, *, kind: str = "img") -> None:
    """把图片描述 / 语音转写写进正文，并清空对应的 media 标记。

    纯逻辑，可单测：调用方拿到模型结果后只需调它。
    kind="img" → [图片: …]；kind="voice" → [语音: …]
    """
    text = (text or "").strip()
    if not text:
        return
    label = "语音" if kind == "voice" else "图片"
    base = (msg.text or "").strip()
    if base in PLACEHOLDER_TEXTS:
        msg.text = f"[{label}: {text}]"
    else:
        msg.text = f"{base} [{label}: {text}]"
    if kind == "voice":
        msg.voices = []
    else:
        msg.images = []


def apply_forward(msg: Msg, text: str) -> None:
    """把展开后的转发内容替换掉占位符。

    与图片/语音不同：转发文本自带 `[转发 N 条]` 抬头，不再套一层 `[…]`。
    """
    text = (text or "").strip()
    if not text:
        return
    base = (msg.text or "").strip()
    if base in PLACEHOLDER_TEXTS:
        msg.text = text
    else:
        msg.text = f"{base}\n{text}"
    msg.forwards = []


def human_time(ts: float, now: float | None = None) -> str:
    """把时间戳说成人话。跨天用「昨天 / 月-日」，避免 23:58→00:03 被当成相邻。

    相对时间比 HH:MM:SS 更适合「我是不是刚说过话」这个判断。
    """
    import datetime

    now = now or time.time()
    delta = now - ts
    if delta < 0:
        delta = 0
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"

    d_now = datetime.datetime.fromtimestamp(now)
    d_ts = datetime.datetime.fromtimestamp(ts)
    same_day = d_now.date() == d_ts.date()
    if same_day:
        hours = int(delta // 3600)
        # 超过 6 小时用绝对时间更有信息量（「03:50」比「11小时前」更能定位）
        return f"{hours}小时前" if hours < 6 else d_ts.strftime("%H:%M")
    if (d_now.date() - d_ts.date()).days == 1:
        return d_ts.strftime("昨天 %H:%M")
    return d_ts.strftime("%m-%d %H:%M")


def render_split(
    messages: list[Msg],
    *,
    recent: int = 8,
    max_chars: int = 4000,
    now: float | None = None,
) -> tuple[str, str]:
    """返回 (更早的上下文, 当前话题)。对应 enhance-mode 的 history_context / messages。"""
    valid = [m for m in messages if (m.text or "").strip()]
    if not valid:
        return "", ""

    def line(m: Msg) -> str:
        who = "我" if m.role == "assistant" else f"{m.name}({m.uid})"
        tag = f"[{m.flag}]" if m.flag else ""
        text = (m.text or "").replace("\n", " ").strip()
        return f"{who}/{human_time(m.ts, now)}{tag}: {text[:200]}"

    split = max(1, min(int(recent), len(valid)))
    older, current = valid[:-split], valid[-split:]

    def clip(lines: list[str]) -> str:
        out = "\n".join(lines)
        return "…（前略）\n" + out[-max_chars:] if len(out) > max_chars else out

    return clip([line(m) for m in older]), clip([line(m) for m in current])


def render(
    messages: list[Msg],
    *,
    recent: int = 8,
    max_chars: int = 4000,
    now: float | None = None,
) -> str:
    """把两段拼成带标题的整段文本（试判回显、单测用）。"""
    older, current = render_split(messages, recent=recent, max_chars=max_chars, now=now)
    if not current:
        return ""
    parts: list[str] = []
    if older:
        parts.append("【更早的背景】")
        parts.append(older)
        parts.append("────────")
    parts.append("【当前话题】")
    parts.append(current)
    return "\n".join(parts)


def trim_contexts(contexts, keep: int):
    """把 OpenAI 格式的会话历史裁到最近 keep 条；keep<=0 表示全部丢弃。

    AstrBot 的 `req.contexts` 是整个会话的历史（群里能堆到 700+ 条），
    每次请求都整段发出去既贵又稀释注意力。群聊的上下文改由
    `<group_context>`（最近 N 条原文）+ 长期记忆 + 知识库承担。
    """
    if not isinstance(contexts, list):
        return contexts
    keep = max(0, int(keep))
    if keep == 0:
        return []
    return contexts[len(contexts) - keep :] if len(contexts) > keep else contexts


PROACTIVE_GUIDE = (
    "下面是你所在的群聊最近的对话。你不一定要回应最后一条——"
    "可以接任何一条你感兴趣的、或顺着正在聊的话题说，也可以换个角度插一句。"
)


def group_context_block(rendered: str, *, proactive: bool = False) -> str:
    """包成 `<group_context>` 注入块。

    主动接话时多给一句引导：真人插话未必冲着最后一条去。不加这句的话，
    模型会把「群里最后一句」当成「用户对我说的话」，8/8 都直接回应它。
    被 @ / 引用时**不加**——那种场合就是要回应叫你的那条。
    """
    body = (rendered or "").strip()
    head = f"{PROACTIVE_GUIDE}\n\n" if proactive and body else ""
    return f"<group_context>\n{head}{body}\n</group_context>"
