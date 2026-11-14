# -*- coding: utf-8 -*-
"""群消息捕获：把群聊消息放进上下文缓冲，供 tick 阶段判定使用。

三条铁律：
1. 只处理群消息（私聊直接返回，插件永远不碰私聊）
2. 不记录 bot 自己发的消息（否则水位线会被自己推进，判定永远看到「有新消息」）
3. 不存指令（以唤醒前缀开头的消息），避免把 /help 之类当成聊天内容

这里只读事件、不改事件、不 stop_event，对现有链路零影响。
"""

from __future__ import annotations

import time
from typing import Any

from ..core.ctx import CtxBuffer, Msg

PLACEHOLDER = {
    "Image": "[图片]",
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Forward": "[合并转发]",
}


def _placeholder(event: Any) -> str:
    """纯非文字消息给个占位符，让模型知道「这里有人发了东西」。"""
    try:
        chain = event.get_messages() or []
    except Exception:  # noqa: BLE001
        return "[非文字消息]"
    for comp in chain:
        name = type(comp).__name__
        if name in PLACEHOLDER:
            return PLACEHOLDER[name]
    return "[非文字消息]"


def _image_urls(event: Any) -> list[str]:
    """把消息里的图片地址捞出来，供上层做视觉转述。"""
    urls: list[str] = []
    try:
        chain = event.get_messages() or []
    except Exception:  # noqa: BLE001
        return urls
    for comp in chain:
        if type(comp).__name__ != "Image":
            continue
        url = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "").strip()
        if url:
            urls.append(url)
    return urls


def voice_ids(event: Any) -> list[str]:
    """返回消息里每段语音的标识（优先 URL，其次文件名）。

    只做「标出有哪些语音」，真正的本地路径由上层异步取（`convert_to_file_path`
    要下载，不能在同步的 feed 里做）。
    """
    out: list[str] = []
    try:
        chain = event.get_messages() or []
    except Exception:  # noqa: BLE001
        return out
    for comp in chain:
        if type(comp).__name__ != "Record":
            continue
        ident = str(
            getattr(comp, "url", "")
            or getattr(comp, "file", "")
            or getattr(comp, "path", "")
            or ""
        ).strip()
        if ident:
            out.append(ident)
    return out


def forward_ids(event: Any) -> list[str]:
    """返回消息里每个合并转发的 id，供上层调 get_forward_msg 展开。"""
    out: list[str] = []
    try:
        chain = event.get_messages() or []
    except Exception:  # noqa: BLE001
        return out
    for comp in chain:
        if type(comp).__name__ != "Forward":
            continue
        ident = str(getattr(comp, "id", "") or "").strip()
        if ident:
            out.append(ident)
    return out


def is_sticker_data(data: Any) -> bool:
    """判断一个 OneBot 图片段是不是「表情包」。

    NapCat / go-cqhttp 对商城表情、收藏表情会带 `sub_type=1` 或
    `summary="[动画表情]"`。这类图数量极大、单张价值低，
    默认不转述只留 [表情]，跳过这类低价值图片可降低调用成本。
    """
    if not isinstance(data, dict):
        return False
    if str(data.get("sub_type") or "") == "1":
        return True
    summary = str(data.get("summary") or "")
    return "动画表情" in summary or "表情" in summary


def image_segments(event: Any) -> list[tuple[str, bool]]:
    """按顺序返回 [(图片地址, 是否表情)]。

    优先读原始 OneBot 消息段（能拿到 sub_type/summary）；
    拿不到就退回组件列表，此时一律当作普通图片。
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    segs = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(segs, list) and segs:
        out: list[tuple[str, bool]] = []
        for seg in segs:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
            url = str(data.get("url") or data.get("file") or "").strip()
            if not url:
                continue
            out.append((url, is_sticker_data(data)))
        if out:
            return out
    return [(u, False) for u in _image_urls(event)]


class Capture:
    def __init__(
        self,
        buf: CtxBuffer,
        targets,
        *,
        self_id: str = "",
        bot_names=None,
        skip_prefixes: tuple[str, ...] = ("/",),
    ):
        self.buf = buf
        self._targets = targets  # 可调用对象，返回当前管理的群号列表
        self._self_id = self_id
        self._bot_names = bot_names or (lambda: [])
        self._skip_prefixes = tuple(skip_prefixes or ())
        self.last_seen: dict[str, float] = {}  # 群号 -> 最后一条群友消息时间
        self.origin: dict[str, str] = {}  # 群号 -> unified_msg_origin（取人格要用）

    def _flags(self, event: Any, text: str, self_id: str) -> str:
        """标出这条消息是不是在叫 bot。判定模型看不到 @ 组件，必须靠这里补。"""
        flags: list[str] = []
        try:
            for comp in event.get_messages() or []:
                if type(comp).__name__ == "At":
                    qq = str(getattr(comp, "qq", "") or "")
                    if qq and self_id and qq == self_id:
                        flags.append("叫我")
                        break
        except Exception:  # noqa: BLE001
            pass
        if "叫我" not in flags:
            try:
                names = [str(n).strip() for n in (self._bot_names() or []) if str(n).strip()]
            except Exception:  # noqa: BLE001
                names = []
            if names and any(n in text for n in names):
                flags.append("提到名字")
        return ",".join(flags)

    def _is_target(self, gid: str) -> bool:
        try:
            return gid in set(self._targets() or [])
        except Exception:  # noqa: BLE001
            return False

    def feed(
        self,
        event: Any,
        *,
        log=None,
        images: list[str] | None = None,
        voices: list[str] | None = None,
        forwards: list[str] | None = None,
        sticker_only: bool = False,
    ) -> Msg | None:
        gid = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if not gid:  # 私聊：永远不处理
            return None
        if not self._is_target(gid):
            return None

        uid = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        self_id = str(getattr(event, "get_self_id", lambda: "")() or self._self_id or "")
        if uid and self_id and uid == self_id:
            return None  # bot 自己的消息不进水位线

        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if text and any(text.startswith(p) for p in self._skip_prefixes if p):
            return None  # 指令不当聊天
        if not text:
            text = "[表情]" if sticker_only else _placeholder(event)

        name = str(getattr(event, "get_sender_name", lambda: "")() or uid or "群友").strip()
        msg = self.buf.add(
            gid,
            role="user",
            name=name,
            uid=uid,
            text=text,
            flag=self._flags(event, text, self_id),
            images=images if images is not None else _image_urls(event),
            voices=voices if voices is not None else voice_ids(event),
            forwards=forwards if forwards is not None else forward_ids(event),
        )
        self.last_seen[gid] = time.time()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            self.origin[gid] = umo
        if log:
            log.debug(f"[话匣子] 捕获 {gid} {name}: {text[:40]}")
        return msg
