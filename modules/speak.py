# -*- coding: utf-8 -*-
"""发送：给管线内的回复插引用、以及主动发一条消息。

两种发送路径（设计文档 §12.3）：
- 被 @ / 引用的回复 —— 走 AstrBot 自己的管线，我们只在 on_decorating_result 里插 Reply，
  这样人格、长期记忆、内置工具全部自动生效。
- 主动接话 —— 没有事件可复用，只能 context.send_message 直接发。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Reply

TAG = "[话匣子]"


def insert_quote(result, msg_id) -> bool:
    """在回复链最前面插入对 msg_id 的引用。已有引用则不重复插。"""
    if not msg_id:
        return False
    try:
        chain = getattr(result, "chain", None)
        if not chain:
            return False
        if any(type(c).__name__ == "Reply" for c in chain):
            return False
        chain.insert(0, Reply(id=msg_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{TAG} 插引用失败：{exc!r}")
        return False


async def send_text(context, umo: str, text: str) -> bool:
    """主动发一条纯文本消息。"""
    text = (text or "").strip()
    if not text or not umo:
        return False
    try:
        return bool(await context.send_message(umo, MessageChain().message(text)))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{TAG} 主动发送失败：{exc!r}"[:200])
        return False


def split_plain_segments(result, words: list[str], limit: int) -> int:
    """把结果链里含分段词的 Plain 拆成多个 Plain。返回拆分后的总段数。

    只处理 `isinstance(Plain)` 的段，不动引用/图片等其它组件。
    单段超过 limit 时保持原样（与 AstrBot 的阈值语义一致）。
    """
    from ..core.segment import split_by_words

    chain = getattr(result, "chain", None)
    if not chain:
        return 0
    new_chain = []
    for comp in chain:
        if type(comp).__name__ == "Plain":
            original = str(getattr(comp, "text", "") or "")
            if original and len(original) <= limit:
                parts = split_by_words(original, words)
                if len(parts) > 1:
                    new_chain.extend(Plain(p) for p in parts)
                    continue
        new_chain.append(comp)
    result.chain = new_chain
    return len(new_chain)
