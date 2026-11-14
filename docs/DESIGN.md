# 话匣子插件 · 逻辑设计

> 状态：历史设计稿（2026-11-08 起），部分内容与当前实现不同；配置与行为以 `_conf_schema.json`、README 和代码为准。
> 命名：`astrbot_plugin_chatterbox`，显示名「话匣子」（2026-11-09 由 `humanlike_chat` 更名，避免与其他插件重名）
> 最后更新：2026-11-08

---

## 0. 目标与非目标

**目标**：让机器人在**指定群**里的存在方式接近真人——不总抢话、不总沉默、会被叫到就答、会自己找话题、说完会退场。

**非目标（明确不做，避免和现有插件打架）**：

| 不做 | 理由 |
|---|---|
| 长期记忆 / 回忆检索 | 交给 AstrBot 的长期记忆类能力（可选，未启用也能用） |
| 表情包、图片对话 | 交给可选的图片/表情类能力（未启用也能用） |
| 省 token 预算曲线 | 属独立的预算控制能力，可后续单独移植 |
| 去 AI 腔 / 打字节奏 | 属表达层，二期（M5 之后） |
| 多平台兼容 | 第一版只保证 `aiocqhttp`（NapCat） |

---

## 1. 状态模型（每个群一份，互不影响）

```
state ∈ { dive, active, probing }
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `gid` | str | 群号 |
| `state` | enum | 当前状态 |
| `entered_active_at` | ts | 本次进入活跃的时刻（用于算硬顶） |
| `expires_at` | ts | 活跃到期时刻 |
| `watermark` | ts + seq | 已判断到哪条**群友**消息；bot 自己的消息不推进它 |
| `empty_ticks` | int | 连续空 tick 计数（仅活跃态使用） |
| `last_probe_at` | ts | 上次主动开话题的时刻 |
| `probe_deadline` | ts | 主动话题的观察窗口截止 |
| `speak_ts` | list[ts] | 发言时间戳（闸门用，滚动保留一个窗口） |
| `busy` | bool | 正在生成/发送中（群级互斥） |
| `next_tick_at` | ts | 下次看群时刻 |

**持久化**：以上字段除 `busy` 外全部落 `data/plugin_data/.../state.json`，随改随存（1~5s 节流）。重启后按 `expires_at` / `probe_deadline` 与当前时间比较决定恢复成哪个状态，不重置为潜水。

---

## 2. 两条通路

```
通路 A（消息驱动，实时）：被 @ 或引用  → 立刻处理，不等 tick
通路 B（时间驱动，后台）：每 1s 醒一次 → 挑到点的群 → 看群 → 可能发言
```

两条路**共用 `busy` 与闸门**。A 优先级高于 B：A 到达时若该群正在 tick 生成，标记本次 tick 结果作废。

### 2.1 通路 A：被 @ / 引用

```
on_trigger(g, msg):
    g.last_trigger_at = now

    # 状态迁移：进活跃 / 续期
    if g.state == dive:
        enter_active(g)
    else:
        extend(g)                      # +extend_per_trigger_min，受硬顶限制

    # 硬顶：从首次进入活跃算起，不超过 active_max_min
    cap = g.entered_active_at + active_max_min
    g.expires_at = min(g.expires_at, cap)

    # 必回：不掷概率、不过介入模型，但**必须过闸门**
    if not gate_allow(g):
        log("触发被闸门拦下，静默丢弃")
        return
    speak(g, quote=msg.id)             # 引用被 @/引用的那条
```

**要点**

- 「必定回复」= **跳过决策**，不是跳过闸门。闸门是硬性上限（对应需求「溢出的 @ 不回复」）。
- 引用对象：单条触发就引它；同一 tick 内多条触发合并回复时，引**最后一条**。
- 到达硬顶后：强制回落 `dive`，并进入 `extend_cooldown_min` 冷却期——期间自动续期失效，但 @ 仍回一次。

### 2.2 通路 B：后台 tick

```
loop every 1s:
    if not enabled: continue
    for g in enabled_groups:
        if g.busy or now < g.next_tick_at: continue
        g.next_tick_at = now + (active_tick if g.state!=dive else dive_tick_min)

        fresh = messages_after(g.watermark, exclude_self=True)

        if g.state == dive:
            if not fresh: continue                  # 潜水态空 tick：什么都不做
            if judge_should_reply(g, fresh):        # 介入模型判断
                if gate_allow(g): speak(g)
            continue

        # ---- active / probing ----
        if fresh:
            g.state = active
            g.empty_ticks = 0
            if rng() < speak_prob and model_says_ok(g, fresh):
                if gate_allow(g): speak(g)
            continue

        if g.state == probing:
            if now >= g.probe_deadline:
                to_dive(g)                          # 观察窗口结束仍无人理 → 回潜水
            continue

        g.empty_ticks += 1
        if probe_allowed(g):                        # 见 §3
            probe(g)
```

---

## 3. 主动发起话题（probe）

对应需求第二次编辑：*「tick 时没有新消息，让 bot 尝试主动发起一个话题；下一个 tick 依旧没有新消息，则回到潜水」*。

### 3.1 我做的两处收敛（需确认）

| 需求原文 | 设计的做法 | 为什么 |
|---|---|---|
| 「tick 时没有新消息」就试 | 要求**静默满 `probe_idle_min`** 才试 | tick 间隔只有 30s~3min，普通群安静 3 分钟很正常。按字面执行等于每小时抛十几次话题，群友会当刷屏 |
| 「下一个 tick 依旧没新消息就回潜水」 | 改为**观察窗口 `probe_wait_min`** | 活跃态 tick 是 30s，抛完话题 30 秒没人理就掉线，观感是「说完就跑」，比不说更尴尬 |

两者都可配置；若坚持字面语义，把 `probe_idle_min` 设为 tick 间隔、`probe_wait_min` 设为 tick 间隔即可。

### 3.2 允许条件

```
probe_allowed(g):
    probe_enabled
    and g.state == active
    and now - last_group_message_at(g) >= probe_idle_min
    and now - g.last_probe_at >= probe_cooldown_min
    and last_message_is_not_self(g)        # 最后一条是 bot 自己 → 不接自己话
    and lines_last_hour(g) < lines_per_hour
    and rng() < probe_prob
```

### 3.3 执行

```
probe(g):
    if not gate_allow(g): return
    text = llm_generate(probe_prompt(g))       # 独立 prompt，与「接话」不同
    send(g, text, quote=None)                   # 不引用
    g.state = probing
    g.last_probe_at = now
    g.probe_deadline = now + probe_wait_min
```

观察窗口内若有人回应（`fresh` 非空）→ `state=active`、`expires_at = now + extend_per_trigger_min`（视为被接上了）。

**两种判断不能复用 prompt**：潜水/活跃是「别人说了话，我要不要接」；probe 是「群里没人说话，我要抛什么」。后者必须额外约束「没有具体事由时宁可不说 / 禁止复述上一条 / 禁止问句套话」。

---

## 4. 上下文注入

### 4.1 组成

| 类型 | 来源 | 处理 |
|---|---|---|
| 文本 | 会话消息表 | 取最近 `ctx_messages` 条，带 `昵称(QQ): 内容` |
| 图片 | 消息组件 `Image` | 视觉模型转述 → 缓存（见 4.2） |
| 语音 | 组件 `Record` | STT 转写 → 缓存 |
| 转发 | 组件 `Forward` | 第一版只取摘要 + 条数，**不展开**（平台 API 有时限与权限风险） |

### 4.2 缓存（成本控制的关键）

```
转述缓存 key = sha1(图片 url | 文件 id)
STT 缓存 key = sha1(语音 url | 文件 id)
```

- 同一张图/同一段语音**全生命周期只处理一次**，结果落 sqlite（`ctx_cache`），带 TTL。
- 单轮最多处理 `ctx_images_per_tick` 张新图，超出部分以 `[图片]` 占位，留到下一轮。
- 不做缓存的话，多群高频图片会反复调用视觉模型，费用和延迟都会失控。

### 4.3 不重复注入

长期记忆类能力（若已启用）会注入记忆召回。本插件只注入**本轮场景上下文（原始近 N 条）**，两者在 prompt 里分区放置，不合并成一份历史。

---

## 5. 决策

| 场景 | 判定链 | 用哪个模型 |
|---|---|---|
| 潜水态，有新消息 | 规则预筛（非 bot / 非 AI / 非指令）→ 介入模型 REPLY/SKIP | 判断模型（可留空跟随会话模型） |
| 活跃态，有新消息 | 规则预筛 → `rng() < speak_prob` → 模型确认（可关） | 判断模型 |
| 活跃态，静默够久 | 见 §3 允许条件 | 判断模型 + 生成模型 |
| 被 @ / 引用 | 只过闸门，不过判断 | 生成模型 |

**概率与模型的顺序**：先概率再模型。概率决定「想不想说」，模型决定「该不该说」；两者都过才开口。

---

## 6. 闸门

```
gate_allow(g):
    prune(g.speak_ts, now - gate_window_sec)
    if len(g.speak_ts) >= gate_max:
        log_reject(g, reason="gate")
        return False
    g.speak_ts.append(now)
    return True
```

- 窗口 `gate_window_sec` 内最多 `gate_max` 条，**主动发言与被动回复共享配额**（否则刷屏防护有洞）。
- 溢出即丢弃，**不排队、不补发**。
- 被拦下时静默，不向群友解释。

---

## 7. 配置项草案

```jsonc
{
  "enabled": true,
  "groups": [],                     // 空 = 未配置，不在任何群生效（建议显式填群号）
  "disabled_groups": [],            // 面板上停用的群，不参与看群与发言

  "state": {
    "dive_tick_min": 3,
    "active_tick_sec": 30,
    "active_ttl_min": 30,
    "extend_per_trigger_min": 3,
    "active_max_min": 120,
    "extend_cooldown_min": 5
  },

  "context": {
    "ctx_messages": 30,
    "read_images": false,            // 读图默认关闭（模型调用有费用）
    "read_voice": false,             // 读语音默认关闭（需要 STT 模型 + ffmpeg）
    "read_forward": true,
    "images_per_tick": 3,
    "vision_provider_id": "",        // 留空跟随 AstrBot 的图片转述模型
    "stt_provider_id": "",           // 留空跟随 AstrBot 的 STT 模型
    "cache_ttl_hours": 168
  },

  "decision": {
    "judge_provider_id": "",         // 留空跟随会话模型
    "judge_enabled": true,
    "speak_prob_active": 0.35,
    "judge_prompt": "",
    "probe_prompt": ""
  },

  "probe": {
    "enabled": false,
    "probe_idle_min": 10,
    "probe_prob": 0.6,
    "probe_wait_min": 4,
    "probe_cooldown_min": 15,
    "lines_per_hour": 6
  },

  "gate": {
    "gate_window_sec": 300,
    "gate_max": 3
  },

  "debug": {
    "shadow_mode": true,            // 只停主动发言，@/引用仍正常回复
    "verbose_log": true
  }
}
```

---

## 8. 与现有插件的边界（启用矩阵）

| 插件 | 本插件启用后 | 原因 |
|---|---|---|
| 其他群历史增强类插件 | **关** | 否则群历史被注入两次 |
| 其他主动回复类插件 | **关** | 两套「概率 + 模型判断」，必然双回 |
| 其他表情/提及解析类插件 | 保留 | 与本插件不冲突 |
| 其他联网搜索类插件 | 保留 | 工具能力 |
| 其他场景推断类插件 | 关 | 场景推断逻辑被本插件吸收 |
| 其他决策/节奏类插件 | 关 | 决策/节奏被本插件吸收 |
| 长期记忆类能力 | 保留 | 启用后自动生效，本插件不依赖 |
| 表情包类能力 | 保留 | 与本插件不冲突 |
| 群开关类能力 | 不再需要 | 群范围与每群启停已内置 |

---

## 9. 验收标准（分阶段）

| 阶段 | 交付 | 验收（在灰度群 `<示例群>` 实测） |
|---|---|---|
| **M0 骨架** | 插件可加载、配置页可保存、状态文件生成 | 面板看到插件、改配置后重启仍在 |
| **M1 影子** | 只停主动发言（`shadow_mode`，@/引用仍正常回复） | 聊一天，日志对每条消息都有「接/不接 + 理由」，群内主动行为零变化 |
| **M2 状态机** | 潜水/活跃/回落/加时 + @ 必回带引用 | @ 后立刻回且引用该条；30min 后日志出现回落；连续 @ 会续期但不超过 120min |
| **M3 闸门** | 限流生效 | 1 分钟内连 @ 5 次，只回 3 条，其余日志记为 gate 拦截 |
| **M4 主动话题** | probe 全流程 | 群静 10min 后抛一次；4min 无人接自动回潜水；自己说完话不会立刻再抛 |
| **M5 多模态** | 图/语音/转发注入 + 缓存 | 发一张图后两轮内能引用图内容；同一张图只出现一次转述调用 |

每个阶段单独提交 git，验收不过不进入下一阶段。

---

## 10. 风险

| 风险 | 缓解 |
|---|---|
| 被群友当刷屏 | 闸门 + `lines_per_hour` + 灰度单群 + shadow 先跑 |
| 视觉模型账单 | 转述缓存 + `images_per_tick` 上限 |
| 重启后状态错乱 | 持久化 + 启动时按时间戳判定，不无脑重置 |
| 转发消息 API 无权限 | 第一版不展开，降级为摘要 |
| 与别人家 AI 互相对话 | 规则预筛：`sender` 是 bot 或消息含 `[CQ:at,qq=其他bot]` 时跳过（可配名单） |
| 定时循环泄漏 | `terminate()` 里 `cancel` + `await`；循环内 `except Exception` 不退出 |

---

## 11. 已确认决策（2026-11-08）

| # | 事项 | 决定 |
|---|---|---|
| 1 | probe 的两个时间语义 | 采用收敛版：`probe_idle_min=10min`、`probe_wait_min=4min` |
| 2 | 潜水态是否 probe | **不 probe**。只有活跃态会主动开话题；潜水态空 tick 什么都不做 |
| 3 | 灰度群 | `<示例群>`（一群），单群先跑 |
| 4 | 插件名 | `astrbot_plugin_chatterbox`，显示名「话匣子」 |

---

## 12. 实现结构

### 12.1 目录

```
astrbot_plugin_chatterbox/
├── metadata.yaml
├── _conf_schema.json
├── main.py                # 入口：装配组件、注册钩子、启停 tick loop
├── core/
│   ├── config.py          # 配置读取 + 默认值 + 类型兜底
│   ├── store.py           # 每群状态持久化（state.json，1s 节流写盘）
│   ├── presence.py        # 状态机：dive / active / probing 转移与到期判定
│   ├── gate.py            # 发言闸门 + 每小时计数
│   ├── ctx.py             # 上下文构建（文本 / 图片 / 语音 / 转发）
│   ├── cache.py           # 转述与转写缓存（sqlite，按内容 hash）
│   ├── decide.py          # 决策：接话判断 / 概率闸 / probe 资格
│   ├── speak.py           # 两条发送路径（管线内回复 / 主动发送）
│   └── prompts.py         # 三个 prompt 模板（接话 / 概率确认 / 开话题）
├── modules/
│   ├── scheduler.py       # 后台 tick loop
│   ├── capture.py         # 群消息捕获：维护 watermark、最后发言时间
│   └── trigger.py         # @ / 引用 通路
└── tests/
    ├── test_presence.py   # 纯逻辑，不依赖 AstrBot
    ├── test_gate.py
    └── test_decide.py
```

设计原则：`core/` 全部是**不依赖 AstrBot 的纯逻辑**，`modules/` 才碰 `event` / `context`。这样状态机、闸门、决策都能单测，不用起机器人。

### 12.2 关键接口

```python
# core/presence.py
@dataclass
class GroupState:
    gid: str
    state: str                    # dive | active | probing
    entered_active_at: float
    expires_at: float
    watermark_ts: float
    watermark_seq: int
    empty_ticks: int
    last_probe_at: float
    probe_deadline: float
    speak_ts: list[float]
    busy: bool
    next_tick_at: float

class Presence:
    def get(self, gid: str) -> GroupState
    def enter_active(self, gid, now) -> None
    def extend(self, gid, now) -> bool          # False = 已被硬顶截断
    def to_probe(self, gid, now, wait_sec) -> None
    def to_dive(self, gid, now) -> None
    def tick_due(self, gid, now) -> bool        # 到点则推进 next_tick_at 并返回 True
    def snapshot(self) -> dict                  # 落盘用

# core/gate.py
def allow(self, gid, now) -> tuple[bool, str]   # (放行, 拒绝原因)
def lines_last_hour(self, gid, now) -> int

# core/ctx.py
@dataclass
class Bundle:
    messages: list[dict]        # [{nick, uid, text, ts, kind}]
    captions: dict[str, str]    # 消息 id -> 图片转述
    voices: dict[str, str]      # 消息 id -> 语音转写
    forwards: dict[str, str]    # 消息 id -> 转发摘要

async def build(gid, limit: int, *, with_media: bool) -> Bundle

# core/decide.py
@dataclass
class Verdict:
    yes: bool
    reason: str
    mode: str                   # rule | prob | model | trigger

async def should_reply(gid, bundle: Bundle, rng) -> Verdict
async def probe_allowed(gid, now, rng) -> Verdict
async def draft_probe(gid, bundle: Bundle) -> str | None

# core/speak.py
async def reply_in_pipeline(event, quote_id: str | None) -> None   # 被 @ / 引用
async def speak_proactive(gid, text: str) -> bool                  # probe
def remember_bot_msg_id(gid, msg_id) -> None                       # 登记，供引用检测
```

### 12.3 两条发送路径（重要差异）

| 路径 | 用于 | 怎么做 | 后果 |
|---|---|---|---|
| **管线内回复** | 被 @ / 引用 | 设 `event.should_call_llm(True)`，让 AstrBot 正常跑完整链路；在 `on_decorating_result` 里往 `MessageChain` 头部插 `Reply(id=quote_id)` | 人格、长期记忆（如已启用）、内置工具全部自动生效 |
| **主动发送** | probe | `context.llm_generate(...)` + `context.send_message(umo, chain)` | 完全可控，但**记忆召回不会自动触发**，需要自己拼上下文（§4 那套） |

结论：**被 @ 的回复必须走管线**，否则等于绕过了长期记忆和人格；主动开话题才自己生成。

### 12.4 钩子注册表

| 钩子 | 优先级 | 用途 | 注意 |
|---|---|---|---|
| `event_message_type(ALL)` | 高（负数，早于其他插件） | 捕获群消息 → 更新 `watermark` / `last_msg_at` | 只读不改，禁止 `stop_event` |
| `event_message_type(ALL)` | 次高 | 识别 `is_at_or_wake_command` 与「引用的是 bot 消息」→ 走通路 A | 需 `remember_bot_msg_id` 的 id 集合 |
| `on_decorating_result` | 默认 | 给本插件触发的回复插 `Reply` | 只处理自己标记过的事件（`event.get_extra`） |
| `after_message_sent` | 默认 | 登记 bot 发出的消息 id、推进水位线、`busy=False` | 主动发送路径也要登记 |

**优先级按本插件功能需要设置**，不依赖其他插件的优先级数字；与其他群聊类插件同时启用时，按 §8 的建议只保留其中一套决策逻辑。

### 12.5 生命周期

```python
async def initialize(self):
    self._task = asyncio.create_task(self._scheduler_loop())   # 1s 醒一次
    await self._store.load()

async def terminate(self):
    self._alive = False
    if self._task:
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
    await self._store.flush()          # 确保状态落盘
```

循环体内 `except Exception: logger.warning(...)` 后继续，**任何单个群的异常都不能中断整个循环**（否则一次报错就永久停止看群，且表面看不出来）。

---

## 13. 开发顺序（下一步）

| 步 | 内容 | 产出 |
|---|---|---|
| 1 | 骨架：`metadata.yaml` + `main.py` + 配置 schema + 空 tick loop | 插件能加载、能保存配置（M0） |
| 2 | `presence.py` + `store.py` + 单测 | 状态机可单测通过（M2 的前提） |
| 3 | `capture.py` + `gate.py` + 影子日志 | 跑一天看判断记录（M1） |
| 4 | `speak.py` 管线内路径 + `trigger.py` | @ 必回带引用（M2） |
| 5 | `ctx.py` + `cache.py` | 多模态注入（M5） |
| 6 | `decide.py` 的 probe 分支 + `speak_proactive` | 主动开话题（M4） |

按此顺序，**每一步都能独立验收**，且第 2 步之后即使其他全没写，插件也不会破坏现有群聊行为（主动发言影子）。
