# -*- coding: utf-8 -*-
"""按「分段词」切分回复文本。纯逻辑，可单测。

为什么插件要自己实现一份：AstrBot 的 `ResultDecorateStage` 只在
`segmented_reply.only_llm_result` 与 `result.result_content_type` 同时满足时
才分段。插件主动发起的回复（`event.request_llm`）在某些分支下拿到的是
`GENERAL_RESULT`，于是整段不拆、反斜杠原样发出去。这里做兜底：
算法与 AstrBot 完全一致，配置也从同一处读，避免两边规则打架。
"""

from __future__ import annotations

import re


def split_by_words(text: str, words: list[str]) -> list[str]:
    """按分段词切分；没有命中就原样返回一段。

    >>> split_by_words("甲\\乙\\丙", ["\\\\"])
    ['甲', '乙', '丙']
    """
    text = text or ""
    cleaned = [str(w) for w in (words or []) if str(w)]
    if not text.strip() or not cleaned:
        return [text]
    pattern = "|".join(
        re.escape(word) for word in sorted(cleaned, key=len, reverse=True)
    )
    parts = re.split(pattern, text)
    return [p.strip() for p in parts if p and p.strip()]


def has_separator(text: str, words: list[str]) -> bool:
    """文本里是否含有配置的分段词（诊断用）。"""
    return any(str(w) and str(w) in (text or "") for w in (words or []))


def plan_segments(
    text: str, words: list[str], *, enable: bool, limit: int = 150
) -> list[str]:
    """把一段文本切成「要逐条发出去的几段」。

    语义与 AstrBot 的分段回复一致：开关关掉、没命中分段词、或整段超过阈值，
    都按一段发。冷场开话题（probe）用这个——它不走管线，没有
    `ResultDecorateStage` 兜着，不自己拆就会把反斜杠原样发进群。
    """
    text = (text or "").strip()
    if not text:
        return []
    if not enable or len(text) > max(1, int(limit)):
        return [text]
    parts = split_by_words(text, words)
    return parts if len(parts) > 1 else [text]


__all__ = ["has_separator", "plan_segments", "split_by_words"]
