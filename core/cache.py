# -*- coding: utf-8 -*-
"""极简 JSON 缓存：图片转述、语音转写都靠它省钱。

同一张图在群里会被反复引用（引用消息、转发、重发），没有缓存的话
每判一次就调一次视觉模型，账单会失控。

纯逻辑，可单测。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def make_key(*parts: str) -> str:
    """把 URL / 文件标识压成一个短 key（不引入 hashlib 的加密强度需求）。"""
    raw = "\u0001".join(p or "" for p in parts)
    h = 0
    for ch in raw:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFFFFFFFFF
    return f"{h:016x}"


class JsonCache:
    """带 TTL 的键值缓存，按需落盘（不在每次读写都写文件）。"""

    def __init__(self, path: Path, ttl_sec: float = 168 * 3600, max_items: int = 3000):
        self.path = Path(path)
        self.ttl = max(60.0, float(ttl_sec))
        self.max_items = max(10, int(max_items))
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            self._data = {}

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if not item:
            return None
        if time.time() - float(item.get("ts", 0)) > self.ttl:
            self._data.pop(key, None)
            self._dirty = True
            return None
        text = str(item.get("text") or "").strip()
        return text or None

    def set(self, key: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._data[key] = {"text": text[:500], "ts": time.time()}
        self._dirty = True
        if len(self._data) > self.max_items:
            # 按时间淘汰最旧的一批，避免文件无限长大
            ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in ordered[: len(self._data) - self.max_items]:
                self._data.pop(k, None)

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)
            self._dirty = False
        except Exception:
            pass

    def __len__(self) -> int:
        return len(self._data)


__all__ = ["JsonCache", "make_key"]
