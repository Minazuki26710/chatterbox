# -*- coding: utf-8 -*-
"""取当前会话的人格设定，喂给判定模型。

三级回退（顺序与 AstrBot 一致）：
1. 会话级设置 `session_service_config.persona_id`
2. 当前对话记录里的 `conversation.persona_id`
3. 全局 `provider_settings.default_personality`

拿到 persona_id 后再从 persona_manager 取 `name` 和 `prompt`。
取不到就返回空，判定 prompt 里就不会出现人设段落（不影响判定）。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger, sp


async def _persona_id(context: Any, umo: str) -> str:
    try:
        session_cfg = await sp.get_async(
            scope="umo", scope_id=umo, key="session_service_config", default={}
        )
        if isinstance(session_cfg, dict):
            pid = str(session_cfg.get("persona_id") or "").strip()
            if pid:
                return pid
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[话匣子] 读会话 persona 失败：{exc!r}")

    try:
        manager = context.conversation_manager
        cid = await manager.get_curr_conversation_id(umo)
        if cid:
            conv = await manager.get_conversation(umo, cid)
            if conv and getattr(conv, "persona_id", None):
                return str(conv.persona_id).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[话匣子] 读对话 persona 失败：{exc!r}")

    try:
        cfg = context.get_config(umo=umo)
        pid = str((cfg.get("provider_settings") or {}).get("default_personality") or "").strip()
        if pid and pid != "[%None]":
            return pid
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[话匣子] 读全局 persona 失败：{exc!r}")

    return ""


async def resolve_persona(
    context: Any, umo: str, *, max_chars: int = 300
) -> tuple[str, str]:
    """返回 (名字, 人设正文)。正文按 max_chars 截断，避免挤掉聊天上下文。"""
    pid = await _persona_id(context, umo)
    persona: dict | None = None

    if pid:
        try:
            persona = next(
                (p for p in context.persona_manager.personas_v3 if p.get("name") == pid),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[话匣子] 按 id 找 persona 失败：{exc!r}")

    if not persona:
        try:
            persona = await context.persona_manager.get_default_persona_v3(umo)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[话匣子] 取默认 persona 失败：{exc!r}")

    if not isinstance(persona, dict):
        return "", ""

    name = str(persona.get("name") or "").strip()
    prompt = str(persona.get("prompt") or "").strip()
    if max_chars > 0 and len(prompt) > max_chars:
        prompt = prompt[:max_chars] + "…"
    return name, prompt


class PersonaCache:
    """判定每 3 分钟一次，没必要每次都查库。60 秒缓存足够。"""

    def __init__(self, ttl: float = 60.0):
        self._ttl = ttl
        self._cache: dict[str, tuple[float, tuple[str, str]]] = {}

    async def get(self, context: Any, umo: str, max_chars: int) -> tuple[str, str]:
        now = time.time()
        hit = self._cache.get(umo)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        value = await resolve_persona(context, umo, max_chars=max_chars)
        self._cache[umo] = (now, value)
        return value
