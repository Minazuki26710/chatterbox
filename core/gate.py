# -*- coding: utf-8 -*-
"""发言闸门：窗口内最多说几句，超出即静默丢弃。

设计文档 §6：
- 主动发言与被动回复**共用**配额（否则刷屏防护有洞）
- 溢出丢弃，不排队、不补发
- 被拦下时静默，不向群友解释

纯逻辑，可单测。
"""

from __future__ import annotations

EXIT_GATE = "gate"


class Gate:
    def __init__(self, cfg):
        self._cfg = cfg

    def _window(self) -> float:
        return max(10.0, self._cfg.num("gate.gate_window_sec", 300.0))

    def _max(self) -> int:
        return max(1, self._cfg.integer("gate.gate_max", 3))

    @staticmethod
    def _prune(st, now: float, window: float) -> None:
        if not st.speak_ts:
            return
        cut = now - window
        st.speak_ts[:] = [t for t in st.speak_ts if t >= cut]

    def allow(self, st, now: float) -> tuple[bool, str]:
        """能不能说。返回 (是否放行, 拒绝原因)。"""
        self._prune(st, now, self._window())
        if len(st.speak_ts) >= self._max():
            return False, EXIT_GATE
        return True, ""

    def record(self, st, now: float) -> None:
        st.speak_ts.append(now)
        st.last_speak_at = now

    def used(self, st, now: float) -> int:
        self._prune(st, now, self._window())
        return len(st.speak_ts)

    @staticmethod
    def gap_ok(last_activity_at: float, now: float, gap: float) -> bool:
        """距「本群上一次说话」（含管线回复）够不够久。

        闸门只管「窗口内几条」，管不住「@ 回复和主动接话贴在一起」：
        管线把一条回复拆成三段发出来时，tick 会在中间插一句，看起来就像
        机器人语无伦次。所以主动发言额外加一道最小间隔。
        """
        if not last_activity_at:
            return True
        return (now - last_activity_at) >= max(0.0, gap)

    def count_last_hour(self, st, now: float) -> int:
        return len([t for t in st.speak_ts if t >= now - 3600])
