# -*- coding: utf-8 -*-
"""合并转发展开与渲染。纯逻辑，不 import astrbot，可单独单测。

为什么需要它：AstrBot 把 `{"type":"forward","data":{"id":...}}` 解析成
`Forward(id=...)`，正文里只剩一个占位符，模型完全看不到转发内容。
要拿内容只能自己调平台的 `get_forward_msg`，而各家返回结构并不统一
（go-cqhttp / NapCat 有的是 `messages`，有的是 `message`，每项可能
又包一层 `{"type":"node","data":{...}}`），所以这里做一层宽容解析。
"""

from __future__ import annotations

from typing import Any

MAX_ITEM_CHARS = 80


def _iter_nodes(payload: Any) -> list:
    """把各种返回结构里的「节点列表」找出来。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "message", "nodes"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value
    data = payload.get("data")
    if isinstance(data, (dict, list)):
        return _iter_nodes(data)
    return []


def _unwrap(node: Any) -> dict:
    """有的实现会把节点包成 {"type":"node","data":{...}}。"""
    if not isinstance(node, dict):
        return {}
    if node.get("type") == "node" and isinstance(node.get("data"), dict):
        return node["data"]
    return node


def _sender_name(node: dict) -> str:
    sender = node.get("sender")
    if isinstance(sender, dict):
        name = sender.get("card") or sender.get("nickname") or sender.get("name")
        if name:
            return str(name).strip()
    for key in ("name", "nickname"):
        value = node.get(key)
        if value:
            return str(value).strip()
    return ""


def content_text(content: Any) -> str:
    """把一条转发里的消息段拼成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for seg in content:
        if isinstance(seg, str):
            out.append(seg)
            continue
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            out.append(str(data.get("text") or ""))
        elif seg_type in ("image", "mface"):
            out.append("[图片]")
        elif seg_type == "face":
            out.append("[表情]")
        elif seg_type in ("record", "audio"):
            out.append("[语音]")
        elif seg_type == "at":
            out.append(f"@{data.get('qq') or ''}")
        elif seg_type == "video":
            out.append("[视频]")
        elif seg_type in ("forward", "node", "nodes"):
            out.append("[嵌套转发]")
    return "".join(out)


def parse_forward(
    payload: Any,
    *,
    max_items: int = 20,
    max_chars: int = 600,
) -> str:
    """把 `get_forward_msg` 的返回渲染成一段可读文本。

    产出形如：
        [转发 3 条]
        小明: 你们看这个
        小红: [图片]
        小明: 笑死 - 80 字/条截断，超过 max_items 条只取前 max_items 条，
        整体超过 max_chars 再截一次（判定模型不需要看完整转发）。
    """
    nodes = _iter_nodes(payload)
    if not nodes:
        return ""

    limit = max(1, int(max_items))
    lines: list[str] = []
    for node in nodes[:limit]:
        flat = _unwrap(node)
        text = " ".join(content_text(flat.get("content") or flat.get("message")).split())
        if not text:
            continue
        if len(text) > MAX_ITEM_CHARS:
            text = text[:MAX_ITEM_CHARS] + "…"
        name = _sender_name(flat) or "某人"
        lines.append(f"{name}: {text}")
    if not lines:
        return ""

    total = len(nodes)
    out = f"[转发 {total} 条]\n" + "\n".join(lines)
    if total > limit:
        out += f"\n…（还有 {total - limit} 条未展开）"
    if len(out) > max_chars:
        out = out[:max_chars] + "…"
    return out


__all__ = ["content_text", "parse_forward"]
