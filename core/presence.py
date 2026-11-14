# -*- coding: utf-8 -*-
"""每个群独立的状态机：潜水 (dive) / 活跃 (active) / 探话 (probing) / 休眠 (sleep)。

规则（对应设计文档 §1、§2、§3.1）：
- 上线默认潜水
- 被 @ / 引用 → 进活跃；活跃中再被 @ → 续期
- 活跃到期 → 回潜水，并进入冷却期（冷却期内 @ 仍回一次，但不会因为 @ 重新变活跃）
- 硬顶：从首次进入活跃算起不超过 active_max_min，到顶后不再续期
- 活跃且群里静默够久 → 可以抛一句话题（probing）；
  窗口内有人接话 → 回活跃，到点没人理 → 回潜水并冷却
- 每天固定时间窗（默认 00:00–08:00，可跨午夜）进入休眠：不回复、不主动说话，
  被 @ 也不进活跃；窗口外自动回潜水

纯逻辑，不依赖 astrbot，可单测。配置通过 Cfg 每次读取，改配置无需重启。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def parse_hhmm(text: str, default: int = 0) -> int:
    """把 "00:00" / "8:00" / "24:00" 解析成当天分钟数；非法值返回 default。"""
    raw = str(text or "").strip().replace("：", ":")
    if not raw:
        return default
    parts = raw.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return default
    if hour < 0 or hour > 24 or minute < 0 or minute > 59:
        return default
    return hour * 60 + minute


@dataclass
class GroupState:
    gid: str
    state: str = "dive"  # dive | active | probing
    entered_active_at: float = 0.0
    expires_at: float = 0.0
    cooldown_until: float = 0.0
    speak_ts: list[float] = field(default_factory=list)
    last_speak_at: float = 0.0
    busy: bool = False
    # 冷场开话题
    probe_sent_at: float = 0.0  # 这次抛话题的时间
    probe_deadline: float = 0.0  # 观察窗口截止
    last_probe_at: float = 0.0  # 上一次抛话题（做冷却用）


class Presence:
    def __init__(self, cfg):
        self._cfg = cfg
        self._states: dict[str, GroupState] = {}

    # ── 配置 ──
    def _ttl(self) -> float:
        return max(60.0, self._cfg.num("state.active_ttl_min", 30.0) * 60.0)

    def _extend(self) -> float:
        return max(0.0, self._cfg.num("state.extend_per_trigger_min", 3.0) * 60.0)

    def _cap(self) -> float:
        return max(60.0, self._cfg.num("state.active_max_min", 120.0) * 60.0)

    def _cooldown(self) -> float:
        return max(0.0, self._cfg.num("state.extend_cooldown_min", 5.0) * 60.0)

    # ── 休眠 ──
    def sleep_enabled(self) -> bool:
        return self._cfg.bool("sleep.enabled", False)

    def sleep_window(self) -> tuple[int, int]:
        """返回 (开始分钟, 结束分钟)；相等表示窗口为空。"""
        return (
            parse_hhmm(self._cfg.text("sleep.start", "00:00"), 0),
            parse_hhmm(self._cfg.text("sleep.end", "08:00"), 480),
        )

    def sleep_window_text(self) -> str:
        start, end = self.sleep_window()
        return f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"

    def in_sleep_window(self, now: float) -> bool:
        """按本地时间判断是否在休眠窗口内；start > end 表示跨午夜。"""
        if not self.sleep_enabled():
            return False
        start, end = self.sleep_window()
        if start == end:
            return False
        lt = time.localtime(now)
        minutes = lt.tm_hour * 60 + lt.tm_min
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end

    def is_sleeping(self, gid: str) -> bool:
        return self.get(gid).state == "sleep"

    def enter_sleep(self, gid: str) -> bool:
        """切到休眠态：清掉活跃/探话计时与冷却。返回是否发生迁移。"""
        st = self.get(gid)
        if st.state == "sleep":
            return False
        st.state = "sleep"
        st.entered_active_at = 0.0
        st.expires_at = 0.0
        st.probe_deadline = 0.0
        st.probe_sent_at = 0.0
        st.cooldown_until = 0.0
        return True

    def exit_sleep(self, gid: str, now: float) -> bool:
        """休眠窗口结束：直接回潜水（不带冷却）。返回是否发生迁移。"""
        st = self.get(gid)
        if st.state != "sleep":
            return False
        st.state = "dive"
        st.entered_active_at = 0.0
        st.expires_at = 0.0
        st.probe_deadline = 0.0
        st.probe_sent_at = 0.0
        st.cooldown_until = 0.0
        return True

    # ── 状态 ──
    def get(self, gid: str) -> GroupState:
        st = self._states.get(gid)
        if st is None:
            st = GroupState(gid=gid)
            self._states[gid] = st
        return st

    def state_of(self, gid: str) -> str:
        return self.get(gid).state

    def is_active(self, gid: str) -> bool:
        """probing 也算「在场」——它是活跃态的观察窗口，被接话就回到 active。"""
        return self.get(gid).state in ("active", "probing")

    def is_probing(self, gid: str) -> bool:
        return self.get(gid).state == "probing"

    def refresh(self, gid: str, now: float) -> bool:
        """到点回落。返回是否发生了迁移。"""
        st = self.get(gid)
        if st.state == "probing":
            if now >= st.probe_deadline:
                return self._fall_to_dive(st, now)
            return False
        if st.state == "active" and now >= st.expires_at:
            return self._fall_to_dive(st, now)
        return False

    def _fall_to_dive(self, st: GroupState, now: float) -> bool:
        st.state = "dive"
        st.entered_active_at = 0.0
        st.expires_at = 0.0
        st.probe_deadline = 0.0
        st.probe_sent_at = 0.0
        st.cooldown_until = now + self._cooldown()
        return True

    def on_trigger(self, gid: str, now: float) -> str:
        """被 @ / 引用时调用。返回 enter / extend / capped / cooldown。"""
        st = self.get(gid)
        self.refresh(gid, now)

        if st.state == "sleep":
            return "cooldown"  # 休眠中不因 @ 进活跃（主流程会先拦住，这里只是兜底）

        if st.state != "active":
            if now < st.cooldown_until:
                return "cooldown"  # 冷却期：回一次，但不重进活跃
            st.state = "active"
            st.entered_active_at = now
            st.expires_at = now + min(self._ttl(), self._cap())
            return "enter"

        cap = st.entered_active_at + self._cap()
        if st.expires_at >= cap - 1e-6:
            return "capped"
        st.expires_at = min(st.expires_at + self._extend(), cap)
        return "extend"

    def force_dive(self, gid: str, now: float) -> None:
        st = self.get(gid)
        st.state = "dive"
        st.entered_active_at = 0.0
        st.expires_at = 0.0
        st.cooldown_until = now + self._cooldown()

    def force_active(self, gid: str, now: float) -> str:
        """面板手动进活跃。返回 enter / extend / capped / asleep。

        与 on_trigger 的区别：不受冷却限制（人是主动点的），并清掉探话窗口。
        休眠窗口内不动状态——那边由 tick 每秒 enter_sleep 打回，答应下来只会骗人。
        """
        st = self.get(gid)
        if st.state == "sleep":
            return "asleep"
        st.cooldown_until = 0.0
        st.probe_deadline = 0.0
        st.probe_sent_at = 0.0
        if st.state == "active":
            cap = st.entered_active_at + self._cap()
            if st.expires_at >= cap - 1e-6:
                return "capped"
            st.expires_at = min(st.expires_at + self._extend(), cap)
            return "extend"
        st.state = "active"
        st.entered_active_at = now
        st.expires_at = now + min(self._ttl(), self._cap())
        return "enter"

    def remaining_sec(self, gid: str, now: float) -> int:
        st = self.get(gid)
        if st.state == "probing":
            return max(0, int(st.probe_deadline - now))
        if st.state != "active":
            return 0
        return max(0, int(st.expires_at - now))

    # ── 冷场开话题（M4） ──

    def can_probe(
        self,
        gid: str,
        now: float,
        *,
        idle_sec: float,
        cooldown_sec: float,
        last_user_at: float,
        min_remain_sec: float = 60.0,
    ) -> tuple[bool, str]:
        """能不能主动开话题。返回 (是否允许, 拒绝原因)。"""
        st = self.get(gid)
        if st.state != "active":
            return False, "not_active"
        if last_user_at <= 0:
            return False, "no_history"  # 没观察过这个群的发言，不贸然开口
        if now - last_user_at < idle_sec:
            return False, "not_idle"
        if st.last_probe_at and now - st.last_probe_at < cooldown_sec:
            return False, "probe_cooldown"
        if st.expires_at - now < min_remain_sec:
            return False, "active_expiring"  # 活跃快结束了，别开一个注定被截断的话题
        return True, ""

    def start_probe(self, gid: str, now: float, wait_sec: float) -> None:
        st = self.get(gid)
        st.state = "probing"
        st.probe_sent_at = now
        st.probe_deadline = now + max(30.0, wait_sec)
        st.last_probe_at = now

    def tick_probe(self, gid: str, now: float, last_user_at: float) -> str:
        """维护 probing 窗口。返回 answered（有人接话）/ timeout（没人理）/ ""。"""
        st = self.get(gid)
        if st.state != "probing":
            return ""
        if last_user_at > st.probe_sent_at:
            # 有人接话：回到活跃，并续期一点，让对话能继续
            st.state = "active"
            st.probe_deadline = 0.0
            st.expires_at = max(st.expires_at, now + self._extend())
            return "answered"
        if now >= st.probe_deadline:
            self._fall_to_dive(st, now)
            return "timeout"
        return ""

MANUAL_SLEEP = "sleep"
MANUAL_AWAKE = "awake"


def should_sleep(manual: str, in_window: bool) -> bool:
    """这个群现在算不算休眠。手动模式优先于时间窗。

    - 手动 /offair 过 → 一直睡，直到手动切换（用户要求「必须我手动切换才能退出」）
    - 手动 /onair 或 /lurk 过 → 一直醒着，时间窗不再生效
    - 没手动过 → 跟时间窗走
    """
    if manual == MANUAL_SLEEP:
        return True
    if manual == MANUAL_AWAKE:
        return False
    return in_window
