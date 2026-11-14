# -*- coding: utf-8 -*-
"""M1 纯逻辑单测：不启动 AstrBot，直接跑。

    python tests/test_core.py

覆盖：上下文缓冲/水位线、时间格式化、两段式渲染、判定结果解析、捕获过滤器。
"""

from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

import types

# 仓库根就是插件目录（AstrBot 按目录加载插件）。测试里把它映射成
# metadata.yaml 里固定的包名，避免依赖 clone 出来的目录名。
_ROOT = Path(__file__).resolve().parent.parent
_pkg = types.ModuleType("astrbot_plugin_chatterbox")
_pkg.__path__ = [str(_ROOT)]  # 当作包，让 core/ modules/ 能被找到
sys.modules["astrbot_plugin_chatterbox"] = _pkg

from astrbot_plugin_chatterbox.core import decide  # noqa: E402
from astrbot_plugin_chatterbox.core.ctx import (  # noqa: E402
    CtxBuffer,
    Msg,
    human_time,
    render,
)
from astrbot_plugin_chatterbox.modules.capture import Capture  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ───────── 上下文缓冲 ─────────

def test_buffer() -> None:
    buf = CtxBuffer(maxlen_per_group=10)
    a = buf.add("g1", role="user", name="小明", uid="10001", text="在吗")
    b = buf.add("g1", role="user", name="小红", uid="10002", text="在")
    c = buf.add("g1", role="assistant", name="话匣子", uid="bot", text="我在")

    check("缓冲条数", len(buf.recent("g1", 99)) == 3)
    check("seq 单调递增", a.seq < b.seq < c.seq)
    check("after 排除 bot 自己的消息", [m.text for m in buf.after("g1", a.seq)] == ["在"])
    check("last_seq 取最新", buf.last_seq("g1") == c.seq)
    check("空群不报错", buf.recent("nope", 5) == [] and buf.after("nope", 0) == [])

    for i in range(20):
        buf.add("g1", role="user", name="x", uid="1", text=f"m{i}")
    check("环形缓冲不超上限", len(buf.recent("g1", 999)) == 10)

    # 会话历史裁剪（v0.18.0）：0 = 全不要，N = 只留最近 N 条
    from astrbot_plugin_chatterbox.core.ctx import trim_contexts

    hist = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    check("history=0 全丢", trim_contexts(hist, 0) == [])
    check("history 负数也全丢", trim_contexts(hist, -3) == [])
    check("history=2 只留最近两条", trim_contexts(hist, 2) == hist[1:])
    check("条数不足时原样返回", trim_contexts(hist, 9) is hist)
    check("非列表原样返回", trim_contexts("x", 0) == "x")

    # 手动模式优先于休眠时间窗（v0.19.0：/offair 锁定、/onair 与 /lurk 强制醒着）
    from astrbot_plugin_chatterbox.core.presence import should_sleep

    check("没手动过 -> 跟时间窗（窗内睡）", should_sleep("", True) is True)
    check("没手动过 -> 跟时间窗（窗外醒）", should_sleep("", False) is False)
    check("手动 offair -> 窗外也睡", should_sleep("sleep", False) is True)
    check("手动 onair/lurk -> 窗内也醒", should_sleep("awake", True) is False)
    check("未知取值按时间窗处理", should_sleep("xxx", True) is True)

    # 指令识别（v0.19.1）：命令消息不能被当成「被叫到」，否则 /onair 一进来就变「已加时」
    from astrbot_plugin_chatterbox.core.commands import is_plugin_command, strip_wake_prefix

    check("去唤醒前缀", strip_wake_prefix("/onair", ["/"]) == "onair")
    check("没有前缀原样返回", strip_wake_prefix("onair", ["/"]) == "onair")
    check("/onair 算指令", is_plugin_command("/onair", ["/"]))
    check("大小写不敏感", is_plugin_command("/OnAir", ["/"]))
    check("指令带参数也算", is_plugin_command("/onair 123", ["/"]))
    check("普通消息不算指令", not is_plugin_command("/今天天气不错", ["/"]))
    check("空文本不算指令", not is_plugin_command("   ", ["/"]))
    check("没配前缀也能认", is_plugin_command("lurk", []))
    check("多前缀任选其一", is_plugin_command("!offair", ["/", "!"]))

    # 主动接话的上下文引导（v0.19.2）：不要求回应最后一条
    from astrbot_plugin_chatterbox.core.ctx import group_context_block

    pro = group_context_block("【当前话题】\n甲: 白饭呢", proactive=True)
    check("主动接话带「不必回应最后一条」", "不一定" in pro and "最后一条" in pro)
    check("主动接话仍包在 group_context 里", pro.startswith("<group_context>") and pro.endswith("</group_context>"))
    atm = group_context_block("【当前话题】\n甲: 白饭呢", proactive=False)
    check("被 @ 时不加引导", "不一定" not in atm)
    check("空上下文不产生只剩引导的块", "不一定" not in group_context_block("", proactive=True))


# ───────── 时间格式化 ─────────

def test_time() -> None:
    base = datetime.datetime(2026, 11, 9, 15, 0, 0)
    now = base.timestamp()
    cases = [
        (now - 10, "刚刚"),
        (now - 59, "刚刚"),
        (now - 180, "3分钟前"),
        (now - 59 * 60, "59分钟前"),
        (now - 2 * 3600, "2小时前"),
        (now - 4 * 3600, "4小时前"),
        (now - 11 * 3600 - 10, datetime.datetime.fromtimestamp(now - 11 * 3600 - 10).strftime("%H:%M")),
        (
            datetime.datetime(2026, 11, 8, 22, 3).timestamp(),
            "昨天 22:03",
        ),
        (
            datetime.datetime(2026, 11, 6, 15, 20).timestamp(),
            "11-06 15:20",
        ),
    ]
    for ts, want in cases:
        got = human_time(ts, now)
        check(f"时间 {want}", got == want, f"got={got}")
    check("未来时间不炸", human_time(now + 60, now) == "刚刚")


# ───────── 两段式渲染 ─────────

def test_render() -> None:
    now = time.time()
    buf = CtxBuffer()
    for i in range(12):
        buf.add("g", role="user", name=f"人{i}", uid=str(100 + i), text=f"话{i}", ts=now - (12 - i) * 60)
    buf.add("g", role="assistant", name="话匣子", uid="bot", text="我说的", ts=now - 30)
    msgs = buf.recent("g", 99)

    text = render(msgs, recent=5, now=now)
    check("有【更早的背景】段", "【更早的背景】" in text, text[:80])
    check("有【当前话题】段", "【当前话题】" in text)
    check("有分隔线", "────────" in text)
    check("bot 显示为「我」", "我/刚刚: 我说的" in text, text)
    check("带相对时间", "人11(111)/1分钟前: 话11" in text, text)
    check("当前话题只包含最近 5 条", text.split("【当前话题】")[1].count("\n") == 5, text.split("【当前话题】")[1])

    one = render([msgs[-1]], recent=5, now=now)
    check("全在当前话题时不出现背景段", "【更早的背景】" not in one and "【当前话题】" in one, one)

    fat = CtxBuffer(maxlen_per_group=50)
    for i in range(30):
        fat.add("g", role="user", name="x", uid="1", text="啊" * 300, ts=now)
    long_text = render(fat.recent("g", 99), now=now)
    check("超长整体截断", "…（前略）" in long_text, long_text[:40])
    check("单条超长先被截到 200 字", ("啊" * 250) not in long_text)
    check("空列表返回空串", render([]) == "")


# ───────── 判定提示词与解析 ─────────

def test_prompt_and_parse() -> None:
    prompt = decide.build_prompt(
        "",
        bot_names="测试机器人",
        messages="小明(1/刚刚): 在吗",
        persona_name="测试角色",
        persona_mask="设定甲",
    )
    check("提示词含人格名", "人格面具是：测试角色" in prompt, prompt[:80])
    check("提示词含人格正文", "设定甲" in prompt)
    check("提示词含上下文", "小明(1/刚刚): 在吗" in prompt)

    no_persona = decide.build_prompt("", bot_names="x", messages="m")
    check("无人设时用兜底文案", "普通的群友" in no_persona)

    custom = decide.build_prompt(
        '{"a":1} 看 {messages} 判断', bot_names="x", messages="M1"
    )
    check("自定义提示词里的花括号不炸", '{"a":1}' in custom and "M1" in custom, custom)

    for raw, yes, reason, unparsed in [
        ("REPLY | 他在叫我", True, "他在叫我", False),
        ("SKIP | 他们在互相聊", False, "他们在互相聊", False),
        ("reply|lower case", True, "lower case", False),
        ("SKIP：他们在互相聊", False, "他们在互相聊", False),
        ("", False, "模型空回复", True),
        ("我觉得应该回复吧", False, "我觉得应该回复吧", True),
    ]:
        got = decide._parse(raw)
        check(f"解析 {raw[:16]!r}", got == (yes, reason, unparsed), f"-> {got}")


# ───────── 捕获过滤 ─────────

class FakeEvent:
    def __init__(self, gid="123456789", uid="10001", self_id="999", text="你好", chain=None):
        self._gid, self._uid, self._self, self._text = gid, uid, self_id, text
        self._chain = chain or []

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_self_id(self):
        return self._self

    def get_sender_name(self):
        return "小明"

    def get_message_str(self):
        return self._text

    def get_messages(self):
        return self._chain


def test_capture() -> None:
    buf = CtxBuffer()
    cap = Capture(buf, lambda: ["123456789"])

    check("私聊不收（group_id 为空）", cap.feed(FakeEvent(gid="")) is None)
    check("非目标群不收", cap.feed(FakeEvent(gid="777")) is None)
    check("bot 自己的消息不收", cap.feed(FakeEvent(uid="999")) is None)
    check("指令不收", cap.feed(FakeEvent(text="/help")) is None)
    check("正常群消息收下", cap.feed(FakeEvent()) is not None)
    check("收下后缓冲有 1 条", len(buf.recent("123456789", 99)) == 1)

    class Image:
        pass

    msg = cap.feed(FakeEvent(uid="10002", text="", chain=[Image()]))
    check("纯图片记占位符", msg is not None and msg.text == "[图片]", repr(msg and msg.text))


def test_flags_and_hint() -> None:
    now = time.time()
    buf = CtxBuffer()
    buf.add("g", role="user", name="小明", uid="1", text="这个怎么弄", ts=now, flag="叫我")
    buf.add("g", role="user", name="小红", uid="2", text="测试机器人在吗", ts=now, flag="提到名字")
    text = render(buf.recent("g", 9), recent=5, now=now)
    check("渲染带触发标记", "[叫我]" in text and "[提到名字]" in text, text)

    check(
        "hint 识别 @",
        decide.build_hint([buf.recent("g", 9)[0]], "测试机器人") == "最新这几条里有人 @ 了你。",
        decide.build_hint([buf.recent("g", 9)[0]], "测试机器人"),
    )
    check("hint 识别名字", "提到了你的名字" in decide.build_hint([buf.recent("g", 9)[1]], "测试机器人"))
    check("无标记时 hint 为空", decide.build_hint([Msg(1, "user", "x", "1", "嗯", now)]) == "")


def test_prompt_v2() -> None:
    p = decide.build_prompt("", bot_names="测试机器人", messages="M", persona_mask="设定甲", hint="有人@了你。")
    check("提示词不再说「不懂就不说」", "不感兴趣" not in p, p[:120])
    check("提示词含 stack_size", "最近 0 条" in p or "（无）" in p)
    check("提示词含通用接话规则", "有人直接问你" in p and "补充有用信息" in p, p[:120])
    check("判断依据整段已删除", "判断依据" not in p and "你刚说过话不久" not in p)
    check("提示词含输出格式", "REPLY" in p and "SKIP" in p)
    check("提示词带入触发提示", "注意：有人@了你。" in p)
    check("关掉理由后输出规格变简单", "不超过 15 字的理由" not in decide.build_prompt("", bot_names="x", messages="m", require_reason=False))
    check("判定提示词不再自带图片说明", "[图片: …]" not in p)

    # D14：无人格、不同人格下通用规则和输出格式都稳定，默认不含作者身份/风格
    no_persona = decide.build_prompt("", bot_names="机器人", messages="m")
    role_a = decide.build_prompt("", bot_names="机器人", messages="m", persona_name="角色A", persona_mask="冷静")
    role_b = decide.build_prompt("", bot_names="机器人", messages="m", persona_name="角色B", persona_mask="活泼")
    rules = ("有人直接问你", "补充有用信息", "REPLY", "SKIP")
    check("无人格也含通用规则", all(x in no_persona for x in rules), no_persona[:120])
    check("不同人格下规则一致", all(x in role_a and x in role_b for x in rules))
    check("人格只影响表达不覆盖规则", "人格只影响表达" in role_a and "人格只影响表达" in role_b)
    check("默认提示词无作者身份/风格", all(
        x not in role_a for x in ("话匣子", "说话很短", "不超过 40 字")
    ))

    # 场景检查：普通闲聊、可推进的闲聊、别人私密对话都按「相关性 + 补充价值」判断，
    # 不再用「只是闲聊 → SKIP」一刀切（C1）
    plain = decide.build_prompt("", bot_names="机器人", messages="甲: 今天好热\n乙: 是啊")
    useful = decide.build_prompt("", bot_names="机器人", messages="甲: 有人知道这个报错怎么修吗")
    private = decide.build_prompt("", bot_names="机器人", messages="甲: 你晚上吃啥\n乙: 随便")
    check("普通闲聊不再一刀切 SKIP", "只是闲聊" not in plain, plain[:120])
    check("SKIP 条件限定为无关/无价值/打断", "与你无关" in plain and "打断" in plain)
    check("可推进的闲聊保留 REPLY 条件", "补充有用信息" in useful and "报错怎么修" in useful)
    check("别人私密对话仍可判 SKIP", "打断别人的闲聊" in private and "你晚上吃啥" in private)
    check("三种场景输出格式一致", all(
        "REPLY" in p and "SKIP" in p for p in (plain, useful, private)
    ))

    # 判定专用人格（v0.14.0）：填了就只用它，留空/纯空白则跟随会话人格
    name, mask = decide.pick_judge_persona("判定用 200 字", "角色A", "完整人格 1362 字")
    check("填了判定人格就用它", (name, mask) == ("判定专用", "判定用 200 字"), f"{name} {mask}")
    check("留空回退会话人格", decide.pick_judge_persona("", "角色A", "完整") == ("角色A", "完整"))
    check("只有空白也算没填", decide.pick_judge_persona("  \n\t ", "角色A", "完整") == ("角色A", "完整"))

    # 判定模型存在性校验（C3）：无效 ID 回退会话模型并给出可操作说明
    exists = lambda pid: pid == "有效模型"
    check("配置的模型有效就用它", decide.pick_judge_provider("有效模型", exists, "会话模型") == ("有效模型", ""))
    check("未配置则跟随会话模型", decide.pick_judge_provider("", exists, "会话模型") == ("会话模型", ""))
    check("无效 ID 回退会话模型",
          decide.pick_judge_provider("已删除模型", exists, "会话模型") ==
          ("会话模型", "判定模型 已删除模型 不存在或已失效"))
    check("无效 ID 且无会话模型返回空", decide.pick_judge_provider("已删除模型", exists, "")[0] == "")

    # 活跃态是否引入判定模型（v0.15.0）
    check("概率没过就不管开关", decide.pick_active_action(0.9, 0.5, True) == "skip_prob")
    check("概率刚好等于阈值算没过", decide.pick_active_action(0.5, 0.5, False) == "skip_prob")
    check("概率过了 + 引入模型 -> 判定", decide.pick_active_action(0.1, 0.5, True) == "judge")
    check("概率过了 + 不引入 -> 直接接", decide.pick_active_action(0.1, 0.5, False) == "reply_prob")


def test_retry() -> None:
    import asyncio

    class FakeResp:
        completion_text = "REPLY | 有人在叫我"

    class FakeCtx:
        def __init__(self):
            self.calls = 0

        async def llm_generate(self, **_kw):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError()
            return FakeResp()

    ctx = FakeCtx()
    v = asyncio.run(
        decide.judge(ctx, "p", "", "话匣子", [Msg(1, "user", "x", "1", "在吗", time.time())], timeout=2)
    )
    check("超时后自动重试并成功", v.yes and ctx.calls == 2, f"yes={v.yes} calls={ctx.calls}")

    class DeadCtx:
        async def llm_generate(self, **_kw):
            raise asyncio.TimeoutError()

    v2 = asyncio.run(
        decide.judge(DeadCtx(), "p", "", "话匣子", [Msg(1, "user", "x", "1", "在吗", time.time())], timeout=1)
    )
    check("两次都超时判 SKIP 且标记异常", (not v2.yes) and v2.unparsed and "超时" in v2.reason, v2.reason)


def test_at_detection() -> None:
    """@ 组件的识别：判定模型看不到 @，全靠这里补标记。"""

    class At:
        def __init__(self, qq):
            self.qq = qq

    class Plain:
        pass

    buf = CtxBuffer()
    cap = Capture(buf, lambda: ["123456789"], bot_names=lambda: ["话匣子", "小匣"])

    ev = FakeEvent(uid="10001", text="这个怎么弄", chain=[At("999")])
    msg = cap.feed(ev)
    check("被 @ 时打上「叫我」标记", msg is not None and msg.flag == "叫我", repr(msg and msg.flag))

    ev2 = FakeEvent(uid="10002", text="话匣子在吗", chain=[Plain()])
    msg2 = cap.feed(ev2)
    check("提到名字时打标记", msg2 is not None and msg2.flag == "提到名字", repr(msg2 and msg2.flag))

    ev3 = FakeEvent(uid="10003", text="小匣帮我看下", chain=[At("12345")])
    msg3 = cap.feed(ev3)
    check("外号也能识别", msg3 is not None and msg3.flag == "提到名字", repr(msg3 and msg3.flag))

    ev4 = FakeEvent(uid="10004", text="@别人 你看", chain=[At("12345")])
    msg4 = cap.feed(ev4)
    check("@ 别人不算叫我", msg4 is not None and msg4.flag == "", repr(msg4 and msg4.flag))


def test_public_defaults() -> None:
    """D6/D7：对外默认值不带作者习惯。"""
    import json

    from astrbot_plugin_chatterbox.core.presence import Presence

    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    check("bot_names 默认留空", schema["decision"]["items"]["bot_names"]["default"] == "")
    check("休眠默认关闭", schema["sleep"]["items"]["enabled"]["default"] is False)

    class FakeCfg:
        def bool(self, path, default=False):
            return default

        def text(self, path, default=""):
            return default

        def num(self, path, default=0.0):
            return default

        def integer(self, path, default=0):
            return default

    check("缺省不启用休眠", Presence(FakeCfg()).sleep_enabled() is False)

    # 名字唤醒是可选的：不配名字时，只有 @ / 引用会触发
    buf = CtxBuffer()
    cap = Capture(buf, lambda: ["123456789"], bot_names=lambda: [])
    msg = cap.feed(FakeEvent(uid="10005", text="话匣子在吗"))
    check("未配置名字不误触发", msg is not None and msg.flag == "", repr(msg and msg.flag))


def test_presence_and_gate() -> None:
    from astrbot_plugin_chatterbox.core.gate import Gate
    from astrbot_plugin_chatterbox.core.presence import Presence

    class FakeCfg:
        def __init__(self, **kw):
            self.kw = kw

        def num(self, path, default=0.0):
            return self.kw.get(path, default)

        def integer(self, path, default=0):
            return self.kw.get(path, default)

    cfg = FakeCfg(
        **{
            "state.active_ttl_min": 30.0,
            "state.extend_per_trigger_min": 3.0,
            "state.active_max_min": 120.0,
            "state.extend_cooldown_min": 5.0,
            "gate.gate_window_sec": 300.0,
            "gate.gate_max": 3,
        }
    )
    p = Presence(cfg)
    t = 1_000_000.0

    check("默认潜水", p.state_of("g1") == "dive")
    check("首次触发进活跃", p.on_trigger("g1", t) == "enter" and p.is_active("g1"))
    check("活跃 30 分钟", p.remaining_sec("g1", t) == 1800)
    check("再触发是续期", p.on_trigger("g1", t + 60) == "extend")
    check("续期 +3 分钟", abs(p.remaining_sec("g1", t + 60) - (1800 - 60 + 180)) <= 1)

    p2 = Presence(cfg)
    p2.on_trigger("g2", t)
    check("未到期不回落", p2.refresh("g2", t + 1700) is False)
    check("到期回落", p2.refresh("g2", t + 1801) is True and p2.state_of("g2") == "dive")
    check("回落即冷却", p2.on_trigger("g2", t + 1802) == "cooldown")
    # tick 每秒调一次 refresh，靠「返回值只 True 一次」决定要不要打日志；
    # 幂等性错了就会每秒刷一行（或一行都不打，见 v0.12.2 的 probe 日志顺序修复）
    check("回落只报一次", p2.refresh("g2", t + 1803) is False)
    check("冷却时长按配置 5 分钟", abs(p2.get("g2").cooldown_until - (t + 1801) - 300) <= 1)
    check("冷却期触发不改状态", p2.state_of("g2") == "dive" and p2.get("g2").expires_at == 0.0)

    p3 = Presence(cfg)
    p3.on_trigger("g3", t)
    for i in range(1, 60):
        p3.on_trigger("g3", t + 60 * i)
    check("续期受硬顶限制", p3.remaining_sec("g3", t + 59 * 60) <= 120 * 60)
    check("到顶后不再续期", p3.on_trigger("g3", t + 60 * 60) == "capped")

    # 面板手动进活跃（v0.13.0）：人不看冷却，点了就进；但发言仍由闸门管
    p4 = Presence(cfg)
    p4.on_trigger("g4", t)
    p4.refresh("g4", t + 1801)  # 到期回潜水并进入冷却
    check("冷却期手动也能进活跃", p4.force_active("g4", t + 1802) == "enter")
    check("手动进活跃走默认 TTL 30 分钟", p4.remaining_sec("g4", t + 1802) == 1800)
    check("手动进活跃清掉冷却", p4.get("g4").cooldown_until == 0.0)
    check("已在活跃再点算加时", p4.force_active("g4", t + 1810) == "extend")
    check("加时后还在活跃", p4.remaining_sec("g4", t + 1810) > 1800)
    p4.enter_sleep("g4")
    check(
        "休眠态不给进活跃",
        p4.force_active("g4", t + 1820) == "asleep" and p4.state_of("g4") == "sleep",
    )

    g = Gate(cfg)
    st = p.get("g4")
    st.speak_ts.clear()
    check("空窗口放行", g.allow(st, t) == (True, ""))
    for _ in range(3):
        g.record(st, t)
    ok, why = g.allow(st, t + 1)
    check("超额拦截", ok is False and why == "gate", f"{ok} {why}")
    check("窗口滑过后放行", g.allow(st, t + 301) == (True, ""))

    # 最小间隔：防止 @ 的分段回复和主动接话贴在一起
    check("从没说过 -> 放行", g.gap_ok(0.0, t, 25.0) is True)
    check("刚说过 -> 拦住", g.gap_ok(t - 3, t, 25.0) is False)
    check("隔够 25 秒 -> 放行", g.gap_ok(t - 25, t, 25.0) is True)
    check("间隔为 0 -> 总是放行", g.gap_ok(t - 0.1, t, 0.0) is True)


def test_probe() -> None:
    """M4 冷场开话题的状态迁移。"""
    from astrbot_plugin_chatterbox.core.decide import build_probe_prompt
    from astrbot_plugin_chatterbox.core.presence import Presence

    class FakeCfg:
        def __init__(self, **kw):
            self.kw = kw

        def num(self, path, default=0.0):
            return self.kw.get(path, default)

        def integer(self, path, default=0):
            return self.kw.get(path, default)

    cfg = FakeCfg(
        **{
            "state.active_ttl_min": 30.0,
            "state.extend_per_trigger_min": 3.0,
            "state.active_max_min": 120.0,
            "state.extend_cooldown_min": 5.0,
        }
    )
    p = Presence(cfg)
    t = 2_000_000.0
    idle = 600.0
    cd = 900.0

    check("潜水态不能开话题", p.can_probe("p1", t, idle_sec=idle, cooldown_sec=cd, last_user_at=t) == (False, "not_active"))

    p.on_trigger("p1", t)
    check("刚活跃且刚有人说话 -> 不够静默", p.can_probe("p1", t + 60, idle_sec=idle, cooldown_sec=cd, last_user_at=t + 60) == (False, "not_idle"))
    check("静默够久 -> 可开", p.can_probe("p1", t + 700, idle_sec=idle, cooldown_sec=cd, last_user_at=t) == (True, ""))
    check("没有历史发言记录 -> 不贸然开口", p.can_probe("p1", t + 700, idle_sec=idle, cooldown_sec=cd, last_user_at=0) == (False, "no_history"))
    check("活跃快结束 -> 不开口", p.can_probe("p1", t + 1750, idle_sec=idle, cooldown_sec=cd, last_user_at=t) == (False, "active_expiring"))

    p.start_probe("p1", t + 700, 240)
    check("进入 probing", p.is_probing("p1") and p.state_of("p1") == "probing")
    check("probing 也算在场", p.is_active("p1"))
    check("probing 剩余时间按观察窗口算", p.remaining_sec("p1", t + 700) == 240)

    check("冷却内不能再开", p.can_probe("p1", t + 800, idle_sec=idle, cooldown_sec=cd, last_user_at=t + 750)[1] == "not_active")

    # 有人接话 -> 回活跃并续期
    moved = p.tick_probe("p1", t + 800, last_user_at=t + 750)
    check("有人接话回活跃", moved == "answered" and p.state_of("p1") == "active", moved)
    check("接话后续期", p.remaining_sec("p1", t + 800) > 0)

    # 没人接 -> 到点回潜水
    p2 = Presence(cfg)
    p2.on_trigger("p2", t)
    p2.start_probe("p2", t + 700, 240)
    check("窗口内无人理 -> 尚未回落", p2.tick_probe("p2", t + 900, last_user_at=t) == "")
    moved2 = p2.tick_probe("p2", t + 950, last_user_at=t)
    check("到点没人理 -> 回潜水", moved2 == "timeout" and p2.state_of("p2") == "dive", moved2)
    check("回潜水后进入冷却", p2.on_trigger("p2", t + 960) == "cooldown")
    check("探话超时也进冷却", abs(p2.get("p2").cooldown_until - (t + 950) - 300) <= 1)

    # probing 到期也能被 refresh 兜住（tick 顺序不同也不会卡住）
    p3 = Presence(cfg)
    p3.on_trigger("p3", t)
    p3.start_probe("p3", t + 700, 240)
    check("refresh 兜住 probing 到期", p3.refresh("p3", t + 1000) is True and p3.state_of("p3") == "dive")

    tmpl = build_probe_prompt("", "角色A", "设定甲", [], 8)
    check("开话题提示词含人格", "角色A" in tmpl and "设定甲" in tmpl)
    check("开话题提示词无空群也成句", "你想自然地起个话头" in tmpl, tmpl[-40:])
    check("开话题提示词按用户要求精简", "不超过 30 字" not in tmpl)
    fallback = build_probe_prompt("", "", "", [], 8)
    check("开话题无人格时用中性兜底", "这个机器人" in fallback and "话匣子" not in fallback and "说话很短" not in fallback, fallback)
    custom = build_probe_prompt('{messages}|{persona_name}|{"x":1}', "名", "", [], 8)
    check("自定义模板花括号不炸", '{"x":1}' in custom and "名" in custom)


def test_sleep() -> None:
    """休眠窗口（含跨午夜）与状态迁移。"""
    from astrbot_plugin_chatterbox.core.presence import Presence, parse_hhmm

    class FakeCfg:
        def __init__(self, **kw):
            self.kw = kw

        def bool(self, path, default=False):
            return self.kw.get(path, default)

        def text(self, path, default=""):
            return self.kw.get(path, default)

        def num(self, path, default=0.0):
            return self.kw.get(path, default)

    def ts(hour: int, minute: int) -> float:
        return datetime.datetime(2026, 11, 10, hour, minute).timestamp()

    base = {
        "state.active_ttl_min": 30.0,
        "state.extend_per_trigger_min": 3.0,
        "state.active_max_min": 120.0,
        "state.extend_cooldown_min": 5.0,
    }

    p = Presence(
        FakeCfg(
            **base,
            **{"sleep.enabled": True, "sleep.start": "00:00", "sleep.end": "08:00"},
        )
    )
    check("23:59 未休眠", p.in_sleep_window(ts(23, 59)) is False)
    check("00:00 进休眠", p.in_sleep_window(ts(0, 0)) is True)
    check("07:59 仍在休眠", p.in_sleep_window(ts(7, 59)) is True)
    check("08:00 结束休眠", p.in_sleep_window(ts(8, 0)) is False)
    check("中午不休眠", p.in_sleep_window(ts(12, 0)) is False)

    # 跨午夜窗口 23:00–07:00
    cross = Presence(
        FakeCfg(
            **base,
            **{"sleep.enabled": True, "sleep.start": "23:00", "sleep.end": "07:00"},
        )
    )
    check("跨午夜 22:59 未休眠", cross.in_sleep_window(ts(22, 59)) is False)
    check("跨午夜 23:00 休眠", cross.in_sleep_window(ts(23, 0)) is True)
    check("跨午夜 03:00 休眠", cross.in_sleep_window(ts(3, 0)) is True)
    check("跨午夜 06:59 休眠", cross.in_sleep_window(ts(6, 59)) is True)
    check("跨午夜 07:00 结束", cross.in_sleep_window(ts(7, 0)) is False)

    off = Presence(
        FakeCfg(
            **base,
            **{"sleep.enabled": False, "sleep.start": "00:00", "sleep.end": "08:00"},
        )
    )
    check("关掉休眠则全天不休眠", off.in_sleep_window(ts(3, 0)) is False)

    check("HH:MM 解析", parse_hhmm("8:05") == 485 and parse_hhmm("00:00") == 0)
    check("非法时间回默认", parse_hhmm("xx", 42) == 42 and parse_hhmm("25:00", 7) == 7)
    check("窗口文案", p.sleep_window_text() == "00:00–08:00", p.sleep_window_text())

    check("进休眠", p.enter_sleep("s1") is True and p.state_of("s1") == "sleep")
    check("休眠不算活跃", p.is_active("s1") is False and p.is_sleeping("s1") is True)
    check(
        "休眠中 @ 不进活跃",
        p.on_trigger("s1", ts(3, 0)) == "cooldown" and p.state_of("s1") == "sleep",
    )
    check("退出休眠回潜水", p.exit_sleep("s1", ts(8, 0)) is True and p.state_of("s1") == "dive")
    check("已在潜水时退出无迁移", p.exit_sleep("s1", ts(8, 1)) is False)


def test_vision() -> None:
    """M5：图片地址捕获、描述注入、缓存。"""
    import tempfile
    from pathlib import Path as _P

    from astrbot_plugin_chatterbox.core.cache import JsonCache, make_key
    from astrbot_plugin_chatterbox.core.ctx import apply_caption

    buf = CtxBuffer()
    cap = Capture(buf, lambda: ["123456789"])

    class Image:
        def __init__(self, url=""):
            self.url = url
            self.file = url

    msg = cap.feed(FakeEvent(uid="10002", text="", chain=[Image("https://cdn/a.jpg")]))
    check("捕获图片地址", msg is not None and msg.images == ["https://cdn/a.jpg"], repr(msg and msg.images))
    check("纯图片正文仍是占位符", msg is not None and msg.text == "[图片]", repr(msg and msg.text))

    apply_caption(msg, "一只蓝色的鱼在吃白米饭")
    check("描述替换占位符", msg.text == "[图片: 一只蓝色的鱼在吃白米饭]", msg.text)
    check("描述写入后清空 images", msg.images == [])

    m2 = buf.add("g", role="user", name="x", uid="1", text="看这个", images=["u"])
    apply_caption(m2, "一张表情包")
    check("带文字的消息追加描述", m2.text == "看这个 [图片: 一张表情包]", m2.text)

    apply_caption(m2, "")
    check("空描述不改写", m2.text == "看这个 [图片: 一张表情包]")

    tmp = _P(tempfile.mkdtemp()) / "c.json"
    c = JsonCache(tmp, ttl_sec=3600, max_items=5)
    check("缓存未命中返回 None", c.get("k") is None)
    c.set("k", "一只鱼")
    check("写入后可读", c.get("k") == "一只鱼")
    c.save()
    check("落盘后可读", JsonCache(tmp, ttl_sec=3600).get("k") == "一只鱼")

    old = JsonCache(tmp, ttl_sec=1)
    old._data["k"]["ts"] = time.time() - 100
    check("过期失效", old.get("k") is None)

    # ── 语音 ──
    from astrbot_plugin_chatterbox.modules.capture import voice_ids

    class Record:
        def __init__(self, url="", file=""):
            self.url = url
            self.file = file

    msg_v = cap.feed(FakeEvent(uid="10003", text="", chain=[Record("https://cdn/a.silk")]))
    check("捕获语音标识", msg_v is not None and msg_v.voices == ["https://cdn/a.silk"], repr(msg_v and msg_v.voices))
    check("纯语音正文是 [语音]", msg_v is not None and msg_v.text == "[语音]", repr(msg_v and msg_v.text))

    apply_caption(msg_v, "今天下午开会", kind="voice")
    check("转写替换占位符", msg_v.text == "[语音: 今天下午开会]", msg_v.text)
    check("转写后清空 voices", msg_v.voices == [])

    m3 = buf.add("g", role="user", name="x", uid="1", text="听这个", voices=["v"], images=["i"])
    apply_caption(m3, "他说晚安", kind="voice")
    check("语音追加到已有文字", m3.text == "听这个 [语音: 他说晚安]", m3.text)
    check("语音只清 voices 不动 images", m3.voices == [] and m3.images == ["i"], f"{m3.voices} {m3.images}")

    check("无 URL 时退回 file", voice_ids(type("E", (), {"get_messages": lambda s: [Record(file="x.amr")]})()) == ["x.amr"])
    check("没有语音返回空", voice_ids(FakeEvent()) == [])
    check("非 Record 不算语音", voice_ids(type("E", (), {"get_messages": lambda s: [Image("u")]})()) == [])

    # ── 合并转发 ──
    from astrbot_plugin_chatterbox.core.ctx import apply_forward
    from astrbot_plugin_chatterbox.core.forward import content_text, parse_forward
    from astrbot_plugin_chatterbox.modules.capture import forward_ids

    class Forward:
        def __init__(self, fid=""):
            self.id = fid

    msg_f = cap.feed(FakeEvent(uid="10004", text="", chain=[Forward("fw-123")]))
    check("捕获转发 id", msg_f is not None and msg_f.forwards == ["fw-123"], repr(msg_f and msg_f.forwards))
    check("纯转发正文是 [合并转发]", msg_f is not None and msg_f.text == "[合并转发]", repr(msg_f and msg_f.text))

    go_cqhttp = {
        "messages": [
            {
                "sender": {"nickname": "小明"},
                "content": [{"type": "text", "data": {"text": "你们看这个"}}],
            },
            {
                "sender": {"nickname": "小红"},
                "content": [{"type": "image", "data": {"file": "a.jpg"}}],
            },
        ]
    }
    out = parse_forward(go_cqhttp)
    check("解析 messages 结构", out.startswith("[转发 2 条]") and "小明: 你们看这个" in out, out[:80])
    check("图片段渲染为 [图片]", "小红: [图片]" in out, out)

    napcat = {
        "data": {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "sender": {"card": "卡片名"},
                        "content": [{"type": "text", "data": {"text": "嵌套"}}],
                    },
                }
            ]
        }
    }
    out2 = parse_forward(napcat)
    check("解析 data.messages + node 包裹", "卡片名: 嵌套" in out2, out2)

    out3 = parse_forward({"messages": [{"sender": {"nickname": "A"}, "message": "用 message 字段"}]})
    check("兼容 message 字段", "A: 用 message 字段" in out3, out3)

    out4 = parse_forward(
        {"messages": [{"sender": {"nickname": "A"}, "content": "字符串内容"}]}
    )
    check("content 是字符串也能读", "A: 字符串内容" in out4, out4)

    out5 = parse_forward(
        {
            "messages": [
                {
                    "sender": {"nickname": "A"},
                    "content": [
                        {"type": "at", "data": {"qq": "123"}},
                        {"type": "record", "data": {}},
                        {"type": "face", "data": {}},
                    ],
                }
            ]
        }
    )
    check("at/语音/表情段", "A: @123[语音][表情]" in out5, out5)

    many = {"messages": [{"sender": {"nickname": f"u{i}"}, "content": "x"} for i in range(30)]}
    out6 = parse_forward(many, max_items=5)
    check("超过上限只取前 5 条", out6.count(": x") == 5, str(out6.count(": x")))
    check("标注剩余条数", "还有 25 条未展开" in out6, out6[-40:])

    long_one = {"messages": [{"sender": {"nickname": "A"}, "content": "啊" * 200}]}
    out7 = parse_forward(long_one)
    check("单条超长截断", "…" in out7 and len(out7) < 140, f"len={len(out7)}")

    out8 = parse_forward(long_one, max_chars=50)
    check("总字数上限生效", len(out8) <= 51, f"len={len(out8)}")

    check("空 payload 返回空串", parse_forward({}) == "" and parse_forward(None) == "")
    check("全是空内容返回空串", parse_forward({"messages": [{"content": []}]}) == "")
    check("content_text 处理非列表", content_text(None) == "" and content_text("原样") == "原样")

    apply_forward(msg_f, "[转发 2 条]\n小明: hi")
    check("展开替换占位符", msg_f.text.startswith("[转发 2 条]"), msg_f.text[:40])
    check("展开后清空 forwards", msg_f.forwards == [])

    m4 = buf.add("g", role="user", name="x", uid="1", text="看这个", forwards=["f"])
    apply_forward(m4, "[转发 1 条]\nA: hi")
    check("已有文字时换行追加", m4.text == "看这个\n[转发 1 条]\nA: hi", m4.text)

    check("Forward 组件取 id", forward_ids(type("E", (), {"get_messages": lambda s: [Forward("f1")]})()) == ["f1"])
    check("空 id 不算", forward_ids(type("E", (), {"get_messages": lambda s: [Forward("")]})()) == [])
    check("没有转发返回空", forward_ids(FakeEvent()) == [])

    # ── 分段兜底 ──
    from astrbot_plugin_chatterbox.core.segment import has_separator, split_by_words

    check("按反斜杠拆分", split_by_words("甲\\乙\\丙", ["\\"]) == ["甲", "乙", "丙"])
    check("无分隔词返回一段", split_by_words("一整段话", ["\\"]) == ["一整段话"])
    check("空分隔词列表原样返回", split_by_words("abc", []) == ["abc"])
    check("多个分隔词", split_by_words("你好。再见！嗯", ["。", "！"]) == ["你好", "再见", "嗯"])
    check("空文本不崩", split_by_words("", ["\\"]) == [""])
    check("开头就是分隔词", split_by_words("\\开头", ["\\"]) == ["开头"])
    check("长分隔词优先", split_by_words("a。。b", ["。", "。。"]) == ["a", "b"])
    check("连续分隔词不产生空段", split_by_words("a\\\\b", ["\\"]) == ["a", "b"])
    check("has_separator 命中", has_separator("a\\b", ["\\"]))
    check("has_separator 不命中", not has_separator("ab", ["\\"]))

    # ── 开话题的分段计划（v0.16.0，probe 不走管线，得自己拆） ──
    from astrbot_plugin_chatterbox.core.segment import plan_segments

    check("开话题按分段词拆", plan_segments("甲\\乙\\丙", ["\\"], enable=True) == ["甲", "乙", "丙"])
    check("分段开关关掉就不拆", plan_segments("甲\\乙", ["\\"], enable=False) == ["甲\\乙"])
    check("没有分段词不拆", plan_segments("一整段话", ["\\"], enable=True) == ["一整段话"])
    check("超过阈值不拆", plan_segments("甲\\乙", ["\\"], enable=True, limit=2) == ["甲\\乙"])
    check("恰好等于阈值会拆", plan_segments("甲\\乙", ["\\"], enable=True, limit=3) == ["甲", "乙"])
    check("空文本返回空列表", plan_segments("   ", ["\\"], enable=True) == [])

    # ── 发言档案 ──
    from astrbot_plugin_chatterbox.core.trace import Trace, TraceStore, extract_memories

    tpath = _P(tempfile.mkdtemp()) / "trace.json"
    store = TraceStore(tpath, max_items=3)
    check("初始为空", store.items == [])
    store.add(Trace(ts=1, gid="g1", reply="甲"))
    check("归档一条", len(store.items) == 1)
    store.add(Trace(ts=2, gid="g1", reply="乙"))
    store.add(Trace(ts=3, gid="g2", reply="丙"))
    store.add(Trace(ts=4, gid="g2", reply="丁"))
    check("环形只留 3 条", len(store.items) == 3, str(len(store.items)))
    check("丢掉最老的", store.items[0].reply == "乙", store.items[0].reply)
    check("recent 按群过滤", [t.reply for t in store.recent("g1")] == ["乙"])
    check("recent 倒序", [t.reply for t in store.recent()][0] == "丁")
    check("按回复精确匹配", store.find_by_reply("丙", "g2").reply == "丙")
    check("按回复包含匹配", store.find_by_reply("丙丙丙", "g2").reply == "丙")
    check("空文本退化为最近一条", store.find_by_reply("", "g2").reply == "丁")
    check("未知群返回 None", store.find_by_reply("x", "nope") is None)
    store.save()
    reopened = TraceStore(tpath, max_items=10)
    check("落盘后可读回", len(reopened.items) == 3 and reopened.items[-1].reply == "丁")

    block = "<RAG-Faiss-Memory>\n- 他爱吃白饭\n- 他说要减肥\n</RAG-Faiss-Memory>"
    got = extract_memories([block])
    check("提取记忆块", len(got) == 1 and "他爱吃白饭" in got[0], str(got))
    check("无标记返回空", extract_memories(["普通文本"]) == [])
    check("多块分别提取", len(extract_memories([block, block])) == 2)
    check("超过上限截断", len(extract_memories([block] * 5, max_items=3)) == 3)
    check("空输入不崩", extract_memories([]) == [] and extract_memories(None) == [])

    real_block = (
        "<RAG-Faiss-Memory>Historical memories recalled by LivingMemory for the assistant. "
        "记忆 #1 · 重要性 0.34 · 话题 白饭\nParticipants: 小鱼\n小鱼只认白饭 "
        "记忆 #2 · 重要性 0.33 · 话题 记账\nParticipants: 小潜\n账别记他头上"
        "</RAG-Faiss-Memory>"
    )
    real = extract_memories([real_block])
    check("按 记忆#N 切成多条", len(real) == 2, str(len(real)))
    check("丢掉头部说明", "Historical" not in real[0], real[0][:40])
    check("条目以编号开头", real[0].startswith("记忆 #1"), real[0][:20])
    check("key 稳定", make_key("img", "http://a") == make_key("img", "http://a"))
    check("不同输入不同 key", make_key("img", "http://a") != make_key("img", "http://b"))

    # ── 表情包识别 ──
    from astrbot_plugin_chatterbox.modules.capture import image_segments, is_sticker_data

    check("sub_type=1 是表情", is_sticker_data({"sub_type": 1}))
    check("sub_type 字符串也认", is_sticker_data({"sub_type": "1"}))
    check("summary 动画表情", is_sticker_data({"summary": "[动画表情]"}))
    check("普通图片不是表情", not is_sticker_data({"sub_type": 0, "summary": "[图片]"}))
    check("非 dict 不崩", not is_sticker_data(None))

    msg_s = cap.feed(FakeEvent(uid="10005", text="", chain=[]), sticker_only=True)
    check("纯表情记 [表情]", msg_s is not None and msg_s.text == "[表情]", repr(msg_s and msg_s.text))

    class RawEvent(FakeEvent):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.message_obj = type(
                "MO",
                (),
                {
                    "raw_message": {
                        "message": [
                            {"type": "image", "data": {"url": "http://a/1.jpg", "sub_type": 0}},
                            {"type": "image", "data": {"url": "http://a/2.gif", "sub_type": 1}},
                        ]
                    }
                },
            )()

    segs = image_segments(RawEvent())
    check("原始消息段解析出 2 张", len(segs) == 2, str(segs))
    check("第二张识别为表情", segs[1][1] is True and segs[0][1] is False, str(segs))


# ───────── 群范围解析 ─────────


def test_resolve_groups() -> None:
    from astrbot_plugin_chatterbox.core.config import resolve_groups

    # 空 groups = 未配置，不在任何群生效（不再有「跟随其他插件」语义）
    check("空 groups 返回空", resolve_groups([], []) == [])
    check("groups 缺省返回空", resolve_groups(None, None) == [])
    # 基本过滤：非数字丢弃、去空白
    check("过滤非数字", resolve_groups(["123", "abc", " 456 ", ""], []) == ["123", "456"])
    # disabled_groups 剔除停用群，非数字停用项忽略
    check(
        "停用群被剔除",
        resolve_groups(["1", "2", "3"], ["2", "abc"]) == ["1", "3"],
    )
    check("全停用返回空", resolve_groups(["1", "2"], ["1", "2"]) == [])
    # 去重保序
    check("重复群号去重保序", resolve_groups(["9", "8", "9"], []) == ["9", "8"])
    # 支持逗号字符串写法（Cfg.items 的 to_list 行为）
    check("逗号字符串也能解析", resolve_groups("111,222", "222") == ["111"])


def test_media_and_group_config() -> None:
    from astrbot_plugin_chatterbox.core.config import (
        Cfg,
        OnceFlag,
        media_enabled,
        save_disabled_groups,
        save_value,
    )

    # 媒体默认值（D3/C1）：配置键缺失时读图/语音必须为关，转发为开
    empty = Cfg({})
    check("缺省不读图", media_enabled(empty, "images") is False)
    check("缺省不读语音", media_enabled(empty, "voice") is False)
    check("缺省读转发", media_enabled(empty, "forward") is True)
    on = Cfg({"context": {"read_images": True, "read_voice": "true"}})
    check("显式开启读图生效", media_enabled(on, "images") is True)
    check("显式开启读语音生效", media_enabled(on, "voice") is True)

    # 一次性告警（C4）：同步/异步路径共用，只放行第一次
    flag = OnceFlag()
    check("一次性告警只放行一次", [flag.take(), flag.take(), flag.take()] == [True, False, False])

    # 每群启停保存（C2）：成功才生效，失败回滚内存值
    class FakeConfig(dict):
        def __init__(self, data, fail=False):
            super().__init__(data)
            self.fail = fail

        def save_config(self):
            if self.fail:
                raise RuntimeError("disk full")

    ok = FakeConfig({"disabled_groups": []})
    check("停用写入配置", save_disabled_groups(ok, "123", False) == ["123"])
    check("重复停用不重复", save_disabled_groups(ok, "123", False) == ["123"])
    check("启用移除配置", save_disabled_groups(ok, "123", True) == [])

    bad = FakeConfig({"disabled_groups": ["456"]}, fail=True)
    raised = False
    try:
        save_disabled_groups(bad, "123", False)
    except RuntimeError:
        raised = True
    check("保存失败抛出", raised)
    check("保存失败回滚内存值", bad["disabled_groups"] == ["456"])

    # 影子模式（D8）：嵌套键写入，失败同样回滚
    sc = FakeConfig({})
    save_value(sc, "debug.shadow_mode", True)
    check("嵌套保存写入", sc["debug"]["shadow_mode"] is True)
    sf = FakeConfig({"debug": {"shadow_mode": True}}, fail=True)
    raised = False
    try:
        save_value(sf, "debug.shadow_mode", False)
    except RuntimeError:
        raised = True
    check("嵌套保存失败抛出", raised)
    check("嵌套保存失败回滚", sf["debug"]["shadow_mode"] is True)


if __name__ == "__main__":
    print("== 上下文缓冲 ==")
    test_buffer()
    print("== 时间格式化 ==")
    test_time()
    print("== 两段式渲染 ==")
    test_render()
    print("== 提示词与解析 ==")
    test_prompt_and_parse()
    print("== 捕获过滤 ==")
    test_capture()
    print("== 触发标记与提示 ==")
    test_flags_and_hint()
    print("== 新提示词 ==")
    test_prompt_v2()
    print("== 超时重试 ==")
    test_retry()
    print("== @ 识别 ==")
    test_at_detection()
    print("== 状态机与闸门 ==")
    test_presence_and_gate()
    print("== 冷场开话题 ==")
    test_probe()
    print("== 休眠 ==")
    test_sleep()
    print("== 读图 ==")
    test_vision()
    print("== 群范围解析 ==")
    test_resolve_groups()
    print("== 媒体默认值与群开关 ==")
    test_media_and_group_config()
    print("== 公开默认值 ==")
    test_public_defaults()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)
