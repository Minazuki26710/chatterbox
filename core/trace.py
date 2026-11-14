# -*- coding: utf-8 -*-
"""发言档案：记下 bot 每次说话时看到了什么。纯逻辑，不 import astrbot，可单测。

为什么需要它：调参时最常问的是「它为什么这么说」，而日志只给结论不给依据。
这里把能拿到的东西按条归档——提问原文、群上下文条数、长期记忆片段、
知识库名字、判定理由、人格、回复——再用 /溯源 或面板按时间回看。

拿不到的东西：知识库片段（AstrBot 在 build_main_agent 里用 copy.copy 之后才注入，
插件钩子看不到），所以按需复算，不在抓取时算。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# LivingMemory 注入长期记忆时用的边界标记，抓取时按它切片
MEMORY_OPEN = "<RAG-Faiss-Memory>"
MEMORY_CLOSE = "</RAG-Faiss-Memory>"


def _split_items(body: str, max_chars: int = 400) -> list[str]:
    """把一块记忆正文切成「记忆 #N」条目。

    LivingMemory 的格式是：
        Historical memories recalled ... （一句免责说明）
        记忆 #1 · 重要性 0.34 · 2026-11-07 21:25 · 话题 …
        Participants: …
        正文…
        记忆 #2 · …
    直接整块展示会把那句英文说明也当成记忆，所以按 `记忆 #N` 切。
    """
    import re

    chunks = re.split(r"(?=记忆\s*#\d+)", body)
    items: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or not re.match(r"^记忆\s*#\d+", chunk):
            continue  # 头部说明 / 空块
        items.append(chunk[:max_chars])
    if items:
        return items
    # 没有 `记忆 #N` 标记（换了版本或格式）→ 整块保留，别丢
    text = body.strip()[:max_chars]
    return [text] if text else []


def extract_memories(texts: list[str], max_items: int = 8) -> list[str]:
    """从注入的文本块里抠出记忆条目。

    记忆插件把召回结果包在 `<RAG-Faiss-Memory>…</RAG-Faiss-Memory>` 里塞进
    `req.extra_user_content_parts`；这里只读不改，抠不到就返回空。
    """
    out: list[str] = []
    for raw in texts or []:
        text = str(raw or "")
        if MEMORY_OPEN not in text:
            continue
        for chunk in text.split(MEMORY_OPEN)[1:]:
            body = chunk.split(MEMORY_CLOSE)[0].strip()
            if not body:
                continue
            out.extend(_split_items(body))
            if len(out) >= max_items:
                return out[:max_items]
    return out[:max_items]


@dataclass
class Trace:
    """一次发言的档案。字段都能缺，缺了就是那时候没拿到。"""

    ts: float = 0.0
    gid: str = ""
    umo: str = ""
    trigger: str = ""  # at / name / proactive / ""
    prompt: str = ""  # 触发这次回复的用户原文
    reply: str = ""  # bot 说出去的话（分段合并后）
    verdict: str = ""  # REPLY / SKIP
    reason: str = ""
    state: str = ""  # 活跃 / 潜水 / 休眠
    persona: str = ""
    model: str = ""
    ctx_count: int = 0
    memories: list[str] = field(default_factory=list)
    kb_names: list[str] = field(default_factory=list)

    def brief(self) -> str:
        """列表里显示的一行。"""
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(self.ts or time.time()))
        head = (self.reply or self.prompt or "").replace("\n", " ")[:40]
        return f"{when} [{self.gid}] {head}"


class TraceStore:
    """环形档案：只留最近 max_items 条，落一个 json 文件。"""

    def __init__(self, path: Path | str, max_items: int = 100):
        self.path = Path(path)
        self.max_items = max(1, int(max_items))
        self.items: list[Trace] = []
        self.load()

    # ── 持久化 ──

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(raw, list):
            return
        out: list[Trace] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            known = {k: v for k, v in row.items() if k in Trace.__dataclass_fields__}
            try:
                out.append(Trace(**known))
            except Exception:  # noqa: BLE001
                continue
        self.items = out[-self.max_items :]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(t) for t in self.items[-self.max_items :]]
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    # ── 读写 ──

    def add(self, trace: Trace) -> Trace:
        if not trace.ts:
            trace.ts = time.time()
        self.items.append(trace)
        if len(self.items) > self.max_items:
            del self.items[: len(self.items) - self.max_items]
        self.save()
        return trace

    def recent(self, gid: str = "", limit: int = 10) -> list[Trace]:
        """按时间倒序返回。gid 为空则不限群。"""
        rows = [t for t in self.items if not gid or t.gid == gid]
        return list(reversed(rows))[: max(1, int(limit))]

    def find_by_reply(self, text: str, gid: str = "") -> Trace | None:
        """用「被引用消息的文本」找回对应的档案。

        QQ 的引用段会带 message_str，但可能被截断、也可能带前导空白，
        所以先精确匹配，再退化为互相包含，最后退化为该群最近一条。
        """
        text = (text or "").strip()
        pool = [t for t in self.items if not gid or t.gid == gid]
        if not pool:
            return None
        if text:
            for t in reversed(pool):
                if (t.reply or "").strip() == text:
                    return t
            for t in reversed(pool):
                reply = (t.reply or "").strip()
                if reply and (text in reply or reply in text):
                    return t
        return pool[-1]


__all__ = ["Trace", "TraceStore", "extract_memories"]
