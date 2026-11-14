# -*- coding: utf-8 -*-
"""配置读取：给 AstrBotConfig 套一层「带默认值 + 类型兜底」的取值器。

AstrBot 的配置是嵌套 dict（AstrBotConfig 继承 dict），面板保存回来的值类型
不一定和 schema 一致（比如数字被存成 str），所以这里统一做一次转换，
避免各处散落 int()/float() 的 try。
"""

from __future__ import annotations

from typing import Any


def plain(raw: Any) -> Any:
    """转成纯 dict/list，去掉 AstrBotConfig 的子类行为，方便递归与拷贝。"""
    if isinstance(raw, dict):
        return {k: plain(v) for k, v in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [plain(v) for v in raw]
    return raw


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "开", "是"):
            return True
        if text in ("0", "false", "no", "off", "关", "否", ""):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_list(value: Any) -> list:
    """支持 list、"a,b,c"、单值三种写法，统一成 list。"""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
    if value is None:
        return []
    return [value]


def resolve_groups(configured: Any, disabled: Any) -> list[str]:
    """群管理范围：groups 里的数字群号，去掉 disabled_groups 停用的，去重保序。

    空 groups 表示未配置（返回空，插件不在任何群生效），不再有
    「留空跟随其他插件」的语义。disabled 里的非数字项直接忽略。
    """
    off = {str(x).strip() for x in to_list(disabled) if str(x).strip().isdigit()}
    out: list[str] = []
    seen: set[str] = set()
    for x in to_list(configured):
        gid = str(x).strip()
        if not gid.isdigit() or gid in off or gid in seen:
            continue
        seen.add(gid)
        out.append(gid)
    return out


def save_disabled_groups(config: Any, gid: str, enabled: bool) -> list[str]:
    """在 config 上更新 disabled_groups 并落盘；保存失败回滚内存值后重新抛出。

    面板每群启停用它：只有保存成功才返回新列表，调用方再同步运行态副本，
    避免「面板报保存失败、但当前运行已经停用这个群」。
    """
    old = config.get("disabled_groups")
    off = [str(x).strip() for x in to_list(old) if str(x).strip()]
    if enabled:
        new = [g for g in off if g != gid]
    else:
        new = off if gid in off else off + [gid]
    config["disabled_groups"] = new
    try:
        config.save_config()
    except Exception:
        if old is None:
            config.pop("disabled_groups", None)
        else:
            config["disabled_groups"] = old
        raise
    return new


def save_value(config: Any, path: str, value: Any) -> None:
    """按点号路径写 config 并落盘；保存失败回滚内存值后重新抛出。"""
    parts = path.split(".")
    node = config
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    key = parts[-1]
    had = key in node
    old = node.get(key)
    node[key] = value
    try:
        config.save_config()
    except Exception:
        if had:
            node[key] = old
        else:
            node.pop(key, None)
        raise


class Cfg:
    """点号路径取值：cfg.bool("probe.enabled", True)。"""

    def __init__(self, raw: Any):
        self.raw = plain(raw)

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def bool(self, path: str, default: bool = False) -> bool:
        return to_bool(self.get(path, default), default)

    def num(self, path: str, default: float = 0.0) -> float:
        return to_float(self.get(path, default), default)

    def integer(self, path: str, default: int = 0) -> int:
        return to_int(self.get(path, default), default)

    def text(self, path: str, default: str = "") -> str:
        value = self.get(path, default)
        return default if value is None else str(value)

    def items(self, path: str) -> list:
        return to_list(self.get(path, []))


# 媒体能力默认值（D3）：读图/语音涉及外部传输与模型费用，默认关闭；
# 转发只调平台接口，默认开启。所有读取点都走 media_enabled，避免再出现
# 「某条路径忘了改默认值」。
MEDIA_DEFAULTS = {"images": False, "voice": False, "forward": True}


def media_enabled(cfg: Cfg, kind: str) -> bool:
    """读图/语音/转发是否开启；配置键缺失时按 MEDIA_DEFAULTS。"""
    return cfg.bool(f"context.read_{kind}", MEDIA_DEFAULTS[kind])


class OnceFlag:
    """一次性开关：第一次 take() 返回 True，之后一直 False（用于只告警一次）。"""

    __slots__ = ("_taken",)

    def __init__(self) -> None:
        self._taken = False

    def take(self) -> bool:
        if self._taken:
            return False
        self._taken = True
        return True
