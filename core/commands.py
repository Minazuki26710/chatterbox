# -*- coding: utf-8 -*-
"""指令识别（纯逻辑，可单测）。

为什么需要它：`/onair`、`/lurk`、`/offair` 这些指令本身也是「群里有人说话」，
如果不先认出来，`on_group_message` 会把命令消息当成「被叫到」，先替它进活跃，
等命令真正执行时状态已经是 active —— 于是 `/onair` 永远回「本来就在活跃，已加时」。
"""

from __future__ import annotations

COMMAND_NAMES = ("chatterbox", "onair", "lurk", "offair")


def strip_wake_prefix(text: str, prefixes: list[str]) -> str:
    """去掉开头的唤醒前缀（`/`、`!` 之类）。没有前缀就原样返回。"""
    raw = (text or "").strip()
    for p in prefixes or []:
        p = str(p or "")
        if p and raw.startswith(p):
            return raw[len(p) :].strip()
    return raw


def is_plugin_command(
    text: str, prefixes: list[str], names: tuple[str, ...] = COMMAND_NAMES
) -> bool:
    """这条文本是不是本插件的指令（大小写不敏感，只认第一个词）。"""
    body = strip_wake_prefix(text, prefixes)
    if not body:
        return False
    head = body.split()[0].lower()
    return head in {n.lower() for n in names}
