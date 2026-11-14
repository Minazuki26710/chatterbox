# -*- coding: utf-8 -*-
"""接话判定：把人格 + 当前话题 + 更早上下文交给判定模型，要一个 REPLY / SKIP。

v0.4.0 改动（按用户要求对齐 enhance-mode 的填充方式）：
- 人格不再截断，按「人格面具 + 人格设定原文」注入
- 上下文拆两段注入：{messages}（当前话题）+ {history_context}（更早的背景）
- 用 {stack_size} / {history_count} 告诉模型两段各有几条

仍然保留两样东西（都是踩过坑换来的）：
1. {trigger_hint} —— 模型看不到 @ 组件，必须明说「有人 @ 了你」
2. 理由字段（可用 decision.require_reason 关掉）—— 影子阶段靠它调参

v0.17.0 按用户要求删掉了「判断依据」四条的整段：原来那句「你刚说过话不久 → 不回」被小模型
逐字照抄，导致窗口里一出现自己的发言就连续 SKIP。图片那条提示
（[图片: …] 可以接 / 只有 [图片] 不要猜）也随之删掉，改由 inject_group_context
注入的 <group_context> 承担；若发现模型开始瞎猜图片，把那条单独加回来即可。

v0.20.0（D14）：判定提示词改为「通用规则优先、人格只影响表达」的三条简短规则，
避免不同人格改变基本判断；同时删除没有生产调用的主动接话旧路径（PROACTIVE_PROMPT /
build_proactive_prompt / compose），只保留冷场开话题。

占位符用 str.replace 而不是 str.format：自定义提示词里可能有 JSON 花括号。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .ctx import Msg, render, render_split

DEFAULT_PROMPT = """你在一个群聊里，先按下面的通用规则判断，人格只影响表达、不改变判断：
- 有人直接问你、@ 你，或话题与你明显相关 → REPLY
- 你能补充有用信息或推进话题，且不会打断别人 → REPLY
- 与你无关、没有可补充价值，且插话会打断别人的闲聊 → SKIP

你当前的人格面具是：{persona_name}
人格设定如下：
{persona_mask}

当前话题（最近 {stack_size} 条）：
{messages}

更早的上下文（最近 {history_count} 条，仅作背景）：
{history_context}
{trigger_hint}
只输出一行：{output_spec}"""


@dataclass
class Verdict:
    yes: bool
    reason: str
    raw: str
    elapsed_ms: int
    unparsed: bool = False


def _parse(raw: str) -> tuple[bool, str, bool]:
    text = (raw or "").strip()
    if not text:
        return False, "模型空回复", True
    head = text.splitlines()[0].strip()
    upper = head.upper()
    yes = upper.startswith("REPLY") or " REPLY" in upper[:12]
    ok = upper.startswith("REPLY") or upper.startswith("SKIP")
    reason = ""
    for sep in ("|", "｜", ":", "："):
        if sep in head:
            reason = head.split(sep, 1)[1].strip()
            break
    if not reason:
        reason = head[:30]
    return yes, reason[:40], (not ok)


def build_prompt(
    template: str,
    *,
    bot_names: str,
    messages: str,
    history_context: str = "",
    persona_name: str = "",
    persona_mask: str = "",
    stack_size: int = 0,
    history_count: int = 0,
    hint: str = "",
    require_reason: bool = True,
) -> str:
    spec = (
        "REPLY | 不超过 15 字的理由，或 SKIP | 不超过 15 字的理由"
        if require_reason
        else "REPLY 或 SKIP"
    )
    text = template or DEFAULT_PROMPT
    text = text.replace("{bot_names}", bot_names or "这个机器人")
    text = text.replace("{persona_name}", persona_name or bot_names or "这个机器人")
    text = text.replace("{persona_mask}", persona_mask or "暂无特别设定，就是一个普通的群友。")
    text = text.replace("{stack_size}", str(stack_size))
    text = text.replace("{history_count}", str(history_count))
    text = text.replace("{messages}", messages or "（无）")
    text = text.replace("{history_context}", history_context or "（无）")
    text = text.replace("{output_spec}", spec)
    block = f"\n人格设定如下：\n{persona_mask}" if persona_mask else ""
    text = text.replace("{persona_block}", block)
    return text.replace("{trigger_hint}", f"\n注意：{hint}\n" if hint else "")


async def _call(context, provider_id: str, prompt: str, timeout: float) -> tuple[str, int, str]:
    """调一次模型，失败重试一次。返回 (文本, 耗时ms, 错误)。"""
    started = time.time()
    last_err = ""
    for attempt in range(2):
        try:
            resp = await asyncio.wait_for(
                context.llm_generate(chat_provider_id=provider_id, prompt=prompt),
                timeout=timeout,
            )
            return (
                getattr(resp, "completion_text", "") or "",
                int((time.time() - started) * 1000),
                "",
            )
        except asyncio.TimeoutError:
            last_err = f"判定超时（>{timeout:.0f}s）"
        except Exception as exc:  # noqa: BLE001
            last_err = f"判定失败：{exc!r}"[:80]
        if attempt == 0:
            await asyncio.sleep(1.0)
    return "", int((time.time() - started) * 1000), last_err


async def judge(
    context,
    provider_id: str,
    prompt_template: str,
    bot_names: str,
    messages: list[Msg],
    *,
    persona_name: str = "",
    persona_mask: str = "",
    hint: str = "",
    recent: int = 8,
    require_reason: bool = True,
    timeout: float = 40.0,
) -> Verdict:
    """调判定模型。context 只用到 llm_generate（鸭子类型，便于测试时替换）。"""
    older, current = render_split(messages, recent=recent)
    prompt = build_prompt(
        prompt_template,
        bot_names=bot_names,
        messages=current,
        history_context=older,
        persona_name=persona_name,
        persona_mask=persona_mask,
        stack_size=len([x for x in current.splitlines() if x.strip()]),
        history_count=len([x for x in older.splitlines() if x.strip()]),
        hint=hint,
        require_reason=require_reason,
    )
    raw, elapsed, err = await _call(context, provider_id, prompt, timeout)
    if err:
        return Verdict(False, err, "", elapsed, True)
    if not raw.strip():
        return Verdict(False, "模型空回复", "", elapsed, True)
    yes, reason, unparsed = _parse(raw)
    return Verdict(yes, reason, raw, elapsed, unparsed)


def pick_judge_provider(
    configured: str, is_valid, fallback: str = ""
) -> tuple[str, str]:
    """选判定模型：配置了且存在就用；配置了但不存在则回退并返回错误说明。"""
    cid = (configured or "").strip()
    if not cid:
        return fallback, ""
    if is_valid(cid):
        return cid, ""
    return fallback, f"判定模型 {cid} 不存在或已失效"


def pick_active_action(roll: float, prob: float, judge_in_active: bool) -> str:
    """活跃态这一次机会怎么处理：skip_prob / reply_prob / judge。

    - 概率没过 → skip_prob（不花任何调用）
    - 概率过了且关掉判定模型 → reply_prob（直接接话）
    - 概率过了且开着判定模型 → judge（先问模型值不值得说）
    """
    if roll >= prob:
        return "skip_prob"
    return "judge" if judge_in_active else "reply_prob"


def pick_judge_persona(override: str, name: str, mask: str) -> tuple[str, str]:
    """判定用哪份人格：配置里填了「判定专用人格」就用它，否则跟着会话人格走。

    判定只关心「这个角色会怎么看这句话」，完整人格卡里的外貌设定、梗表、
    名字来历对判断没有帮助，却每次判定都要付一遍 token。
    """
    text = (override or "").strip()
    if text:
        return "判定专用", text
    return name, mask


def build_hint(pending: list[Msg], bot_names: str = "") -> str:
    """把触发信号说成人话，喂给判定模型。"""
    if not pending:
        return ""
    flags: set[str] = set()
    for msg in pending:
        for f in (msg.flag or "").split(","):
            if f.strip():
                flags.add(f.strip())
    parts: list[str] = []
    if "叫我" in flags:
        parts.append("最新这几条里有人 @ 了你")
    if "提到名字" in flags:
        parts.append(f"有人提到了你的名字（{bot_names or '你的名字'}）")
    return "；".join(parts) + "。" if parts else ""


# ───────── M4：冷场开话题 ─────────

DEFAULT_PROBE_PROMPT = """你当前的人格面具是：{persona_name}
人格设定如下：
{persona_mask}
这个群安静了一阵子了。最近的话题（最近 {stack_size} 条）：
{messages}
你想自然地起个话头"""


def build_probe_prompt(
    template: str,
    persona_name: str,
    persona_mask: str,
    messages: list[Msg],
    recent: int = 8,
) -> str:
    _, current = render_split(messages, recent=recent)
    text = template or DEFAULT_PROBE_PROMPT
    text = text.replace("{persona_name}", persona_name or "这个机器人")
    text = text.replace("{persona_mask}", persona_mask or "一个普通的群友。")
    text = text.replace(
        "{stack_size}", str(len([x for x in current.splitlines() if x.strip()]))
    )
    return text.replace("{messages}", current or "（最近没人说话）")


async def compose_probe(
    context,
    provider_id: str,
    template: str,
    persona_name: str,
    persona_mask: str,
    messages: list[Msg],
    *,
    recent: int = 8,
    timeout: float = 30.0,
) -> str:
    """生成一句「开话题」。返回空串表示放弃。"""
    prompt = build_probe_prompt(template, persona_name, persona_mask, messages, recent)
    raw, _elapsed, err = await _call(context, provider_id, prompt, timeout)
    if err or not raw.strip():
        return ""
    line = raw.strip().splitlines()[0].strip()
    line = line.strip("\"“”'「」")
    return line[:80]


__all__ = [
    "DEFAULT_PROBE_PROMPT",
    "DEFAULT_PROMPT",
    "Verdict",
    "build_hint",
    "build_probe_prompt",
    "build_prompt",
    "compose_probe",
    "judge",
    "pick_judge_provider",
    "render",
]

