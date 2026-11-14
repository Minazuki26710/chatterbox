# -*- coding: utf-8 -*-
"""图片转述：把一张图交给视觉模型，换回一句能塞进上下文的描述。

只做「把图变成文字」，不做缓存、不管限流——那些在调用方（main）。
"""

from __future__ import annotations

import asyncio
import uuid

from astrbot.api import logger

TAG = "[话匣子]"

DEFAULT_CAPTION_PROMPT = (
    "用中文简洁描述这张图片：画面主体是什么、在做什么、有没有值得注意的文字。"
    "不超过 50 字，不要客套话，不要「这张图片显示」这种开头。"
)


async def caption(
    provider,
    url: str,
    prompt: str = DEFAULT_CAPTION_PROMPT,
    *,
    timeout: float = 15.0,
) -> str:
    """调一次视觉模型。失败/超时返回空串（调用方保留 [图片] 占位符即可）。"""
    url = (url or "").strip()
    if not url or provider is None:
        return ""
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt or DEFAULT_CAPTION_PROMPT,
                session_id=uuid.uuid4().hex,
                image_urls=[url],
                persist=False,
            ),
            timeout=max(3.0, timeout),
        )
    except asyncio.TimeoutError:
        logger.warning(f"{TAG} 读图超时（>{timeout:.0f}s）：{url[:80]}")
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{TAG} 读图失败：{exc!r}"[:300])
        return ""
    text = str(getattr(resp, "completion_text", "") or "").strip()
    usage = getattr(resp, "usage", None)
    if usage is not None:
        logger.info(
            f"{TAG} 读图用量 in={getattr(usage, 'input_other', 0)} "
            f"cached={getattr(usage, 'input_cached', 0)} "
            f"out={getattr(usage, 'output', 0)}"
        )
    return " ".join(text.split())[:200]


__all__ = ["DEFAULT_CAPTION_PROMPT", "caption"]
