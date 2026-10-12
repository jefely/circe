# MCP 挂载这一轮抓到的东西

**日期**：2026-10-10
**触发**：把一个**真实的本地 LLM**（Ollama）接到 `server.py` 的另一端，让它**自己**从
`tools/list` 的 description 里选工具——补上 `demo_mcp_client.py` 那一步
（那里选工具靠的是脚本自己的关键词打分，不是模型在读）。

**新增验证器**：[`test_ollama_agent.py`](test_ollama_agent.py)
**判据修正**：[`挂载指南.md`](挂载指南.md) §三 检查 3（原文的判据是错的）

> **已进论文**：本文件的 **F1 已写进论文 §5.2.4，作为「洞五：核剖面的确定性，
> 依赖于调用方输入字段的受控性」**，与洞一~洞四并列（系统层第五条）。
> 论文那里的表述以此处为准：机制、量化、修法与两条边界。
> （章节号 2026-10-12 随论文重排：v0.3 的 §5.2.4 即 v0.2 的 §6.2。）
> **所以下面 §二 F1 不是最终归宿，是证据底稿。**

---

## 零、先说什么被证明、什么没有

| 命题 | 状态 | 证据 |
|---|---|---|
| CIRCE 的 MCP 服务能被**真实的第三方客户端**接上 | ✅ **已证** | Claude Code 2.1.296 `√ Connected`；`真实客户端协议序列.log` 7 行原始 JSON-RPC |
| 一个 **LLM 只凭四个 description 能选对工具** | ✅ **已证（限 qwen2.5:7b / qwen3-vl:8b）** | `test_ollama_agent.py` **23 / 0**，三组配置全过 |
| 「记录一次调用 → 再查剖面 → density 非 0」（洞四回归） | ✅ **已证** | S5：`density=0.562092`，两次不同模型同样是这个值 |
| **Claude** 能否正确选工具 | ❌ **仍未证** | 账号暂停（§四），`claude -p` 不可用 |
| VS Code **真的启动了**服务器进程 | ✅ **已证（2026-10-11 00:13:53）** | 进程树判据：`python.exe` ← `Code.exe`(2432) ← `Code.exe`(34744)，命令行与配置**逐字相同**；退出码 0。见 §三之二 |
| VS Code **完成了 MCP 握手 / 列出了四个工具** | ✅ **已证（00:13:56）** | 服务器连接日志：`Connection state: Running` + **`Discovered 4 tools`**；`[server stderr] [mcp] CIRCE server 启动: agent=agent-B` |
| VS Code **能用这四**个工具 | ✅ **已证（00:22:08，改名之后）** | 最近一次连接：`Discovered 4 tools` **且无 `is invalid`**；判据脚本退出码 0。**改名前 4 个全被判无效（可用 = 0）**，见 §二 F6 |
| **服务器把工具发出去**（挂载第 ④ 层） | ✅ **已证（2026-10-12 22:54:06）** | 服务器侧协议追踪：`tools/list` 被调用，**四个工具 + 完整 inputSchema 全部发出**。见 §三之四 |
| **客户端把工具交给模型**（挂载第 ⑤ 层） | ✅ **已证（2026-10-12 23:09:33），但只对原生模型成立** | 同一客户端 `copilot-cli` 1.0.92：BYOK 本地模型只拿到 18 个内置工具，**换成原生 `gpt-6-luna` 后四个工具都在**。见 §三之四 |
| **第三方客户端真的调用了 circe** | ✅ **已证——第一次（23:09:33）** | 追踪：`tools/call → circe_ledger_verify`，服务端回 `{"entries": 5, "head": "4366966748c55661…"}` |
| **改过的 description 能被真实客户端遵循** | ✅ **已证，且是受控 A/B（2026-10-12 23:42 / 23:47）** | 同一句**未命名**提示词、同一客户端与模型、两次都是新会话，**只改描述**：旧描述 → **不查直接写**；新描述 → **先查清单再复用旧名**（`core_profile_query` → 34 秒后 `trust_update`）。**注意结果层两边 `span` 都是 `0.305556`，区分不出来——判据必须在过程层。** 见 §三之四 |

> ⚠️ **第二行不能读成第四行。** 它证明的是「**一个** LLM 能够」，不是「Claude 能够」。
> 论文里要用，必须写成前者并标明模型与量化等级，换模型要重测。

---

## 一、验证器跑出来的结果

```
python CIRCE/mcp/test_ollama_agent.py --model qwen2.5:7b               → 23 / 0
python CIRCE/mcp/test_ollama_agent.py --model qwen2.5:7b --no-time     → 23 / 0
python CIRCE/mcp/test_ollama_agent.py --model qwen3-vl:8b              → 23 / 0
```

链路每一层都是真的：**本地模型（HTTP）→ 本脚本（MCP 客户端）→ stdio 管道 → `server.py`
→ 账本写入 → 剖面重算 → 原路返回**。握手走 `server/discover`（新协议），
与 Claude Code 实测发的那条**同一条路径**。

五个场景全部选对工具，包括**刻意分开的那一对**：

| 场景 | 用户原话 | 期望工具 | 结果 |
|---|---|---|---|
| S1 | 「`agent-ollama` 现在能接活吗？」 | `readiness_probe` | ✅ |
| S2 | 「我准备依赖它。删掉它，我会失去什么？」 | `core_profile_query` | ✅ |
| S3 | 「我刚调用过它，成功，耗时 500 毫秒，帮我记一下。」 | `trust_update`（success=true, latency=500） | ✅ |
| S4 | 「它的账本有没有被人改过？」 | `ledger_verify` | ✅ |
| S5 | 「再查一次它的核剖面。」 | `core_profile_query`，**且 density≠0** | ✅ `0.562092` |

**S1/S2 是重点**：`readiness_probe` 与 `core_profile_query` 在
[`README.md`](README.md) 里被刻意设计成两个工具（探活便宜、要高频；剖面贵、带签名）。
两个模型都**没有把它们混起来**——说明这两段 description 的分工是**可被读出来的**，
不只是设计者的意图。

---

## 二、发现（六条，按影响面排）

### F1 `skill` 是调用方自由文本，而它是核剖面三维之一 `span` 的输入

**这是本轮最重要的一条：一个真实 LLM 暴露了 CIRCE 自己词汇表的缺口。**

| 位置 | 代码 |
|---|---|
| 定义 | [`protocol/stats_provider.py:170`](../protocol/stats_provider.py#L170) `skills = len({r.skill for r in rs})` |
| 用法 | [`protocol/stats_provider.py:171`](../protocol/stats_provider.py#L171) `span = (saturate(callers, 5) + saturate(skills, 5)) / 2` |
| schema | [`mcp/server.py:193`](server.py#L193) `"skill": {"type": "string", "description": "本次调用属于哪个技能维度"}` |

**description 里没有取值范围、没有示例、没有词表。** 而 `skill` 直接决定 `span`。

**实测：同一个 agent、同一个 caller、各记录一次，三次运行给出三个不同的 skill 值：**

| 模型 / 配置 | 它填的 `skill` |
|---|---|
| qwen2.5:7b（给了时钟） | `"core_profile_query"` ← 把工具名当技能 |
| qwen2.5:7b（不给时钟） | `"circe"` ← 把服务器名当技能 |
| qwen3-vl:8b（给了时钟） | `"general"` |

**量化（`saturate(x, k) = x/(x+k)`，k_callers = k_types = 5）：**

| 情形 | span |
|---|---|
| 1 个 caller，1 个一致的 skill | `0.166667` |
| 1 个 caller，3 个噪声 skill | `0.270833`（**×1.625**） |
| 1 个 caller，5 个噪声 skill | `0.333333`（**×2.0**） |

**而噪声只会单调抬高 span**——skill 种类数不会因为"填错了"而减少。

> **这直接命中 CIRCE 的核心命题**：核剖面号称是「调用日志的确定性统计量，不含 LLM 判断」。
> 而 `skill` 这个字段**没有任何约束**，一个 LLM 调用方**必然**填出噪声，
> 于是 `span` 里混进了一个**非确定性的、由调用方模型决定的**成分。
> **统计量本身是确定性的，输入不是。**

**三条候选修法（含义不同，需要你定，我没有动代码）：**

1. **受控词表**：`skill` 改成 enum，或由注册表下发可选值——最强，但要求生态里先有注册表。
2. **描述里给示例**：最便宜，但只是"更可能填对"，不构成约束。
3. **服务端归一**：服务端把无法识别的 skill 归一到 `"unspecified"`——保住 `span` 的语义，
   代价是丢掉真实但未登记的技能名。

### F2 `timestamp` 是必填项，而服务端不校验它

| 位置 | 代码 |
|---|---|
| schema | [`mcp/server.py:196`](server.py#L196) `"timestamp": {"type": "string", "description": "ISO-8601 时间戳"}` |
| 校验 | [`mcp/server.py:405`](server.py#L405) 只检查**在不在**，不检查**能不能解析、合不合理** |

**实测（`--no-time`，不给模型时钟）：**

```
模型自报 timestamp = "2023-04-30T14:48:00Z"
真实时间           = 2026-10-10T23:24:45+08:00
相差               ≈ 2.5 年
服务端返回         = accepted: true
```

**模型不知道现在几点，于是它编一个。而服务端接受了。**

这不是模型的错——**是 schema 要了一个调用方原理上不知道的东西**。
一个 LLM 调用方的时钟只在**宿主**手里。所以这个字段该由宿主注入，
而 schema 现在的写法（"必填"）会**稳定地**产生一个假值。

### F3 `decay_weight` 的守卫被它唯一的调用点消掉了

**这一条是"一个层次的正确性，依赖于另一个层次上没有被保证的东西"的又一例，而且很干净。**

[`trust/chain.py:200`](../trust/chain.py#L200) 写得很明确：

```python
if age_seconds < 0:
    raise ValueError("age_seconds 不能为负（未来时间戳意味着时钟有问题）")
```

**而唯一调用它的地方** [`protocol/server.py:134`](../protocol/server.py#L134) 是：

```python
pairs.append((e.profile, decay_weight(max(age, 0.0), half_life_seconds)))
#                                ^^^^^^^^^^^^^ 先夹紧，再调用
```

**`max(age, 0.0)` 把这个守卫消掉了。** 而且 `now` 取的是**本次请求自己带的 timestamp**
（[`protocol/server.py:129`](../protocol/server.py#L129)），再对**全部历史条目**算 age。

**后果（半衰期 30 天，[`protocol/server.py:114`](../protocol/server.py#L114)）：**

```
未来 10 年的那一条  : age = -315619200s → max(·,0) = 0 → 权重 = 0.5^0     = 1.0
2026 年的全部历史   : age =  315619200s → 权重 = 0.5^121.8 = 2.2e-37
```

> **一个调用方发一个未来的时间戳，就让整条历史的权重趋零、自己那条拿满权重。**
> **一个字段就能把加权聚合的参考系挪到自己身上。**

**暴露面**：HTTP `POST /update` **直接返回**这个聚合
（[`protocol/server.py:213`](../protocol/server.py#L213) `"new_profile": r.new_profile`）。
MCP 路径走同一个 `AgentState.update`，但把它丢了（见 F5）。

**修法在你手里**：`decay_weight` 已经写好了它想要的语义。要恢复它，去掉那个
`max(..., 0.0)` 即可——**但那会让"未来时间戳"从静默接受变成显式报错**，
是行为变更，会影响已有客户端。**我没有改。**

### F4 `last_updated` 用字符串 `max`，跨时区格式会报出较早的时刻

[`protocol/stats_provider.py:335`](../protocol/stats_provider.py#L335)：

```python
last = max((r.timestamp for r in rs), default="")
```

**实测：**

```
max("2026-10-11T01:00:00+08:00", "2026-10-10T20:00:00Z")
  → "2026-10-11T01:00:00+08:00"     ← 字符串比较大
而 "2026-10-11T01:00:00+08:00" = 2026-10-10T17:00Z
   "2026-10-10T20:00:00Z"      = 更晚 3 小时   ← 真实更晚的是这个
```

**而 schema 不约束格式**（F2 已证实际出现 `Z` 与 `+08:00` 混用，见 F2 的实测值）。
**影响面要说准**：`depth` / `density` / `span` **都不读 timestamp**
（[`stats_provider.py:115-172`](../protocol/stats_provider.py#L115-L172)），
所以**核剖面三维不受这条影响**，只有 `CoreProfile.last_updated` 这一个派生字段会报错时刻。

### F5 同一个动作，HTTP 与 MCP 返回的 `new_profile` 是**两种东西**

| 路径 | 返回什么 |
|---|---|
| HTTP `POST /update` | `r.new_profile` = 衰减加权聚合（`success_rate` / `weighted_calls` / `by_skill`）——[`protocol/server.py:213`](../protocol/server.py#L213) |
| MCP `circe_trust_update` | `p.to_dict()` = 从账本重算的 `CoreProfile`（`depth`/`density`/`span`）——[`mcp/server.py:269-273`](server.py#L269-L273) |

**同一个字段名 `new_profile`，两条路径两种含义。** MCP 那条把 `r.new_profile` 算完就丢了。
现在不会出错（两边都自洽），但**改名或合并时会出事**，所以留档。

### F6 【已推翻并改正】带点号的工具名**确实是一道缝**——只是我测的那一个客户端没有它

> ⚠️ **本条的第一版结论是错的，2026-10-11 00:13 被 VS Code 推翻。**
> 原文写的是「Ollama **接受**带点号的函数名——那道我以为存在的缝，实测不存在」，
> 并据此建议不要再"预防性地"改名。**那个建议的方向是反的。**
> **保留下面的原文，因为「我怎么错的」比结论更有用。**

**原文（错误结论，保留）：**

> 我预期这里有一道缝：**MCP 允许工具名带 `.`**（我们用 `circe.` 做命名空间），
> 而 **OpenAI 的 function name 约束是 `^[a-zA-Z0-9_-]{1,64}$`——不含点**。
> **实测：不存在这道缝。** Ollama 直接接受 `circe.readiness_probe`。
> 记这一条是为了避免下次再"预防性地"改名——**改名要引入一张双向映射表，
> 那是为不存在的问题付代价。**

**为什么它错了**：我只在**一个**客户端上测了那道缝。**缝在另一个客户端上。**

```
2026-10-11 00:13:56   VS Code 1.141.0 的 mcpServer.mcp.config.ws0.circe.log
   [info]    Discovered 4 tools
   [warning] Tool "circe.readiness_probe"    is invalid. Tools names may only contain [a-z0-9_-]
   [warning] Tool "circe.core_profile_query" is invalid. Tools names may only contain [a-z0-9_-]
   [warning] Tool "circe.trust_update"       is invalid. Tools names may only contain [a-z0-9_-]
   [warning] Tool "circe.ledger_verify"      is invalid. Tools names may only contain [a-z0-9_-]
```

**VS Code 发现了四个工具，然后把四个全部判为无效**——所以 circe 在 VS Code 里
**四个工具一个都用不了**。而 Claude Code 2.1.296 接受它们（抓到的序列里四个都正常列出）。

**所以真实状态是「客户端分歧」，不是「没有这道缝」**：

| 客户端 | 带点号的 `circe.readiness_probe` |
|---|---|
| Claude Code 2.1.296 | ✅ 接受（`真实客户端协议序列.log` 里四个都列出来了） |
| 本地 Ollama（`/api/chat` 的 `tools`） | ✅ 接受 |
| **VS Code 1.141.0** | ❌ **四个全部判为 invalid**（`[a-z0-9_-]`） |

**我错在哪**：把「我测的那个客户端接受」读成了「不存在这道缝」。
**这正是本文 §6.1 那条规则**：

> 任何验证脚本都必须声明「它测的东西与真实运行的东西在哪一点上是同一个」。

我测的是「Ollama 接不接受」，而结论声称的是「这道缝存不存在」——
**两个命题不一样，而我把它们当成了一个。** 与 §6.2.1 里那三次
（UTF-16 探针、Chrome 截图、手挑探针集）**同形**。

**而这一条只在把 circe 挂到第二个真实客户端时才显形**——自检、管道往返、
甚至第一个真实客户端（Claude Code）**全都不会报**。
**这就是"挂到真实客户端"这件事本身的价值，也是它唯一无法被自检替代的地方。**

**修法（已执行，作者裁决 2026-10-11）**：四个工具名改成 `[a-z0-9_-]` 内的形态：

```
circe.readiness_probe     →  circe_readiness_probe
circe.core_profile_query  →  circe_core_profile_query
circe.trust_update        →  circe_trust_update
circe.ledger_verify       →  circe_ledger_verify
```

改动落在 **9 个文件、共 77 处**（`server.py` 33、`test_stdio_roundtrip.py` 8、
`demo_mcp_client.py` 10、`protocol/server.py` 1、`README.md` 4、`mcp/README.md` 5、
`挂载指南.md` 5、论文 5、`launch_vscode_and_read_mcp_log.py` 1。

**`真实客户端协议序列.log` 与本文上面的引文一律不改**——它们是**历史记录**：
那份日志记的就是「当时发的是带点号的名字」。

**并且加了一道护栏**（写进 `server.py --selftest`，`--selftest` 65 → **66**）：

```python
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
bad = [n for n in names if not _NAME_RE.match(n)]
check("**工具名全部满足客户端通用的 `[a-z0-9_-]{1,64}`**", not bad, …)
```

**为什么是断言而不是文档**：只写一句"名字别带点号"，改的人不会读到那段文档；
**门禁会拦住他。**（与 math 层那条「一个不会失败的门禁只是装饰」同一个理由。）

**而没有加别名兼容层**：Claude Code 与 Ollama 的配置里只写「启动 `server.py`」，
工具名是**运行时读到**的——所以改名对它们是透明的，**不需要一张双向映射表**。
（F6 第一版警告过"为不存在的问题付改名代价"；
区别在于——**这次那道缝是真的**。）

**验证（真实客户端，端到端）**：改完 → 在 VS Code 里 Stop → Start 一次，
同一条日志的**最近一次连接**给出：

```
00:22:08.277 [info]    Connection state: Running
00:22:08.402 [warning] [server stderr] [mcp] CIRCE server 启动: agent=agent-B data=…
00:22:08.404 [info]    Discovered 4 tools
（到此为止 —— 一条 `is invalid` 都没有）
```

**所以「点号是唯一的问题」这个假设成立**，而它是**可证伪的**：
若重启后仍有 `is invalid`，假设就被推翻。判据脚本随即给出**退出码 0**。

> ⚠️ 而判据脚本自己在这里又踩了一次同一个错法：它把**累积日志**里
> 00:13 与 00:15 那两次旧失败也数了进来，于是**改好之后仍报退出码 6**。
> **一份累积日志描述的不是「当前状态」，是「全部历史」。**
> 已改成只看**最近一次连接**那一段。见 `tools/check_vscode_mcp_live.py` 顶部第三课。


---

## 二之补、F1 与 F3 已修（作者裁决 G23：**两条都修，并补拒绝路径测试**）

**记录在案，包括两个由我做的判断**（`project-manifest.json` · `decisions` · G23）。

### 改了什么

| # | 位置 | 改动 |
|---|---|---|
| ① | [`protocol/stats_provider.py`](../protocol/stats_provider.py) | 新增 `skill_key()` 与 `StatsProvider.skill_vocabulary`（**可选**参数）。**归一只发生在派生统计层——账本仍写调用方原话。** |
| ② | [`protocol/server.py`](../protocol/server.py) `AgentState.update` | 衰减的参考时钟从 `req.timestamp` 改为**服务端时钟**；本次请求自身时间戳超容差 → `TimestampRejected` **拒绝写入**，且**校验在写账本之前**；历史里已有的未来时间戳**不抛，夹住并列进 `clock_skew_problems`** |
| ③ | 同上 `parse_timestamp()` | 接住**不带时区偏移量**的输入（按 UTC 解释）——schema 从未要求带偏移量，而 LLM 会省略 |
| ④ | [`mcp/server.py`](server.py) `tool_defs` | `skill` 与 `timestamp` 的 description 补上**用法**（这是根因修法：模型原来无从知道） |

### 为什么这样修（两个判断，标注为我的）

**(a) 保留了 `max(age, 0.0)`，但只用于容差内。**
彻底去掉夹紧会把「客户端时钟快几秒」也一起拒掉——**那是用一个可用性问题换一个完整性问题**。
所以加了显式参数 `max_future_skew_seconds=300`：容差内夹到 0（无害），容差外拒绝/报告。

**(b) 归一用可选词表，不给默认词表。**
`skill_vocabulary=None` 时行为与改动前**逐位一致**（已有测试不受影响）。
**归一到什么词表是部署方的判断，不是代码的判断**——与 `k_calls` 那几个半饱和点同样处理。

**而「历史里有未来记录」为什么不抛**：抛会让那个 agent **从此写不进任何东西**。
一个已存在的坏记录不该让整个 agent 变成只读。所以**夹住 + 报出来**，不猜。

### 测试

```
server.py --selftest            52 → 66    （+13 洞五拒绝路径、+1 工具名可移植性）
test_stdio_roundtrip.py            23      无回归
tests/selftest.py                  28      无回归
tests/selftest_stats.py            30      无回归
tests/selftest_span_correction.py  15      无回归
tests/check_conventions.py         17      无回归
```

新增的拒绝路径断言（`--selftest` §⑧之二/⑧之三）：

```
✓ 未来 10 年的时间戳被拒（-32602，不是 -32603）
✓ **被拒时账本没有增长**（校验在写入之前）      ← 最关键的一条
✓ 快 60 秒（容差 300 秒内）被接受
✓ 不带时区的时间戳被接受（按 UTC 解释，不报 TypeError）
✓ 账本已有记录时，响应里不出现 clock_skew_problems（无未来记录）
✓ 不传词表 = 旧行为（每个噪声都算一种技能）
✓ 传词表 → 词表外的都归到一个桶
✓ **归一把 skill 噪声对 span 的抬高掐掉了**（spanA > spanB）
✓ **再编一个新名字，span 一点都不动**（spanB == spanC）   ← F1 的修法被钉住
✓ **账本保真**：账本里仍是调用方原话，不是归一后的桶名
✓ skill/timestamp 的 description 交代了该交代的
```

### 实测：改描述**当场改变了模型行为**（同一模型 qwen2.5:7b，同一场景）

| 配置 | 改之前 | 改之后 |
|---|---|---|
| 给时钟 → `skill` | `"core_profile_query"`（把工具名当技能） | **`"问答"`** ← 新描述里的示例 |
| 不给时钟 → `skill` | `"circe"` | **`"unspecified"`** ← 按新描述的要求 |
| 不给时钟 → `timestamp` | `"2023-04-30T14:48:00Z"` | `"2023-04-14T12:00:00+08:00"` |

> **最后一行是本轮最干净的一条结论**：描述能修好「**格式**」与「**该填什么**」，
> **但修不好「现在是几点」。** 那个值**只能由宿主注入**——
> `timestamp` 从来就不该是一个指望调用方模型提供的必填项。

**强制 out-of-vocab 端到端实测**（`--skill-vocabulary "检索"`，而模型说的是 `问答`）：

```
账本里的原始 skill = ['问答']        ← 真相来源保真
剖面里的 skills    = ['unspecified'] ← 派生统计已归一
```

**一条如实记账的边界**：默认那次带词表的端到端跑（`--skill-vocabulary "问答,检索"`）里，
**归一分支没被触发**——因为改好的描述把模型引导进了词表内（它答了 `问答`）。
所以那条路的真实验证是靠上面的**强制场景**与 `--selftest`，不是靠默认那次。

---

## 三、判据修正：VS Code 的检查 3 原来是错的

[`挂载指南.md`](挂载指南.md) §三 检查 3 写的判据是：

> **唯一判据是 `mcpGateway.log`**。只有 `[McpGatewayService] Initialized` + `Client disconnected`
> → 网关起来了，**但一个服务器都没配**。

**2026-10-10 23:00 那次实测推翻了它。** `mcpGateway.log` 仍然只有那两行，
**而 VS Code 已经解析了我们的 `mcp.json`**——证据在同一日志目录的另外两个文件里：

```
renderer.log（4 条警告）
  [warning] Overwriting mcp server 'circe' from /D:/myproject/.vscode/mcp.json
            with /C:/Users/Administrator/AppData/Roaming/Code/User/mcp.json.

window1\mcpServer.mcp.config.ws0.circe.log        ← 为 circe 建了服务器槽位
```

**所以 `mcpGateway.log` 只记网关的生命周期，不记每个服务器的加载。**
判据要用日志，而**日志要读对那一层**。

**而日志仍然不够**——它只证明「建了槽位」。那个日志文件是 **0 字节**，
而 `server.py` 启动时会往 stderr 写一行 `[mcp] CIRCE server 启动: ...`。
**0 字节既不等于"起来了但没说话"，也不等于"没起来"——它什么都不能证明。**

**决定性证据是进程树**：VS Code 起服务器就是起一个子进程。
本轮的判据已经从"读日志"换成"看进程"：

```
python tools/check_vscode_mcp_live.py
  → 找命令行含 server.py 的 python 进程，沿 ParentProcessId 上溯
  → 若祖先是 Code.exe：**观察到的事实**，不是推断
  → 若 VS Code 没开：明说"无法判断"（退出码 4），不给一个看起来像结论的东西
```

**另一个附带发现（F7）：两处都注册了 `circe`。**

| 位置 | 内容 |
|---|---|
| 用户级 `%APPDATA%\Code\User\mcp.json` | `code --add-mcp` 写的 |
| 工作区 `D:\myproject\.vscode\mcp.json` | 手工加的，多一个 `cwd` |

VS Code 的裁决是**用户级覆盖工作区级**，并打 `Overwriting` 警告（4 次）。
**两处都留着会让"到底加载了哪一份"变成一个需要读日志才能回答的问题**——
而这正是判据变脆的原因之一。

### 三之二、进程树判据本身也需要对照组（2026-10-11 实测补上）

第一版的判据是「没有 `server.py` 子进程 → **✗ VS Code 读了配置却没起服务器**」。
**那句话技术上为真，但它把读者引向「circe 挂了」，而实测的成因不是那个。**

00:05 那次窗口（VS Code 真的打开了、11 个进程）的完整证据：

```
① 命令行含 server.py 的 python 进程              : 0
③ Code.exe 的后代                                : conhost.exe, copilot-runtime.exe
   其中 python / node（对照组）                   : 0
④ 已登记的 MCP 服务器                            : 2
     · mcp.config.ws0.circe                      → 日志 0 字节
     · ms-python.vscode-pylancepylance mcp server → 日志 0 字节   ← 内置的、必然有效
   views.log                                     : 空（MCP 视图没打开过）
```

**Pylance 自带的那个 MCP 服务器也没起**——所以真实成因是
「VS Code 在空闲窗口里**一个 MCP 服务器都还没起**，因为还没被需要」。
而第一版那句推论依赖一个**没有被核对过**的假设：
「VS Code 会在窗口打开时就把配好的服务器拉起来」。

**修法：判据改成相对的**——跟对照组比，不跟零比。

| 我方进程 | 对照组（python / node 后代） | 判定 | 退出码 |
|---|---|---|---|
| 在跑 | —— | ✓ 真的起来了 | 0 |
| 没有 | **也是零** | **？ 不能说明 circe 有问题** | 5 |
| 没有 | 有别的 | ✗ 这时才是它没被起起来 | 3 |

**还踩了一次同类的坑**：第一版把对照组定义成「任何非 Chromium 的后代」，
于是 `copilot-runtime.exe` 被当成「别的 MCP 服务器在跑」，判定从「都没起」错推成
「别的起了、circe 没起」。**对照组要选得能代表被测对象**——Copilot 运行时不是 MCP 服务器。
现在只算 `python.exe` / `node.exe`。

> **这与 §二 的 F1~F6 是同一条线**：一个层次的结论（"没有进程"），
> 依赖于另一个层次上一个没有被核对的假设（"空闲窗口会拉起 MCP 服务器"）。

### 三之三、结果：VS Code 确实启动了我们的服务器（2026-10-11 00:13:53）

在 MCP 视图里手动 Start `circe` 之后，判据给出**退出码 0**：

```
② 命令行里含 `server.py` 的 python 进程: 1
   pid=1800  父链: python.exe(1800) ← Code.exe(2432) ← Code.exe(34744)
     ✓ 是 Code.exe 的后代
     cmd: …\python.exe -X utf8 D:\myproject\CIRCE\mcp\server.py --agent agent-B --data D:\myproject…
③ 对照组：Code.exe 拉起的 python / node 进程（1 个）  ← 就是它自己
   ✓ VS Code 确实启动了我们的服务器（观察到 python.exe 子进程在跑 server.py）
```

**这是「观察到的事实」，不是推断**：进程存在、父链上溯到 `Code.exe`、
命令行与配置文件里的那一行**逐字相同**。

**同一时刻仍未证的，以及为什么**：

| 事项 | 状态 | 为什么 |
|---|---|---|
| VS Code 完成了 MCP 握手 | ❓ | VS Code **不把协议层写进日志**：`mcpGateway.log` 只有 `Initialized`，`mcpServer.mcp.config.ws0.circe.log` 仍是 **0 字节**（且它在 00:05:26 就写完了，**早于 00:13:53 的启动**——所以那个文件记录的是「配置被读到」，不是「服务器启动了」） |
| VS Code 调过任何工具 | ❌ **没调过** | `data/agent-B/ledger.json` 仍是 5 条、时间戳还是 23:22:36（那是我自己测试留下的）。**而这是预期的**——它还没有模型（`no-model-at-toolbar-build`），Chat 里问不了 |

**所以要拿 VS Code 的真实协议序列，只有一条路：把配置临时指向录制代理**
（`tools/mcp_recording_proxy.py`，抓 Claude Code 那次用的就是它）。
**不过 00:13 那次之后这条已经不那么急了**——服务器的连接日志已经把协议层的结果说出来了
（`Running` → `Discovered 4 tools` → 四个全 invalid），
代理能多给的只是**逐条 JSON-RPC 的原文**（它发 `initialize` 还是 `server/discover`、
`clientInfo` 是什么）。**那些是"它是怎么说的"，而"结果是什么"已经知道了。**

#### 而那条日志还翻出了一个真缺陷（同一份证据，两件事）

```
00:13:56.211 [info]    Discovered 4 tools
00:13:56.211 [warning] Tool "circe.readiness_probe"    is invalid. Tools names may only contain [a-z0-9_-]
00:13:56.212 [warning] Tool "circe.core_profile_query" is invalid. …
00:13:56.212 [warning] Tool "circe.trust_update"       is invalid. …
00:13:56.212 [warning] Tool "circe.ledger_verify"      is invalid. …
```

**VS Code 发现了四个工具，然后把四个全部判为无效——可用工具数 = 0。**
00:15:38 用户重启过一次，四个被同样拒绝：**可复现，不是偶发。**

**这推翻了 F6 的第一版结论**（我原本写「带点号不是问题」），
理由与完整对照表见 §二 F6。**而它落在论文 §7.2 那句设计理由上**：

> 「挂在 MCP 上意味着对方已经会了」——而这句话只在能连上时才成立。

**现在要把后半句也补上：能连上 ≠ 能用。**
VS Code 连上了、握了手、还**准确地发现了四个工具**——
然后因为**工具名里有 `.`** 而把四个全拒了。

**"对方已经会了"的精读是**：对方会的是 **MCP 协议**；
但它对**你起的工具名**有它自己的约束，而那个约束**不在 MCP 协议里**
（MCP 不禁止 `.`；约束来自 LLM 侧 function name 的模式 `[a-z0-9_-]`）。
**「会协议」与「接受你的命名」是两层，而我把它们当成了一层。**

---

### 三之四、挂了五层，断在第五层：客户端拿到了工具，但没交给模型（2026-10-12）

**这一条是服务器侧的协议追踪抓到的**（`CIRCE_MCP_TRACE=<路径>`，见 `server.py` 的 `serve_stdio`）。
**为什么必须从服务器侧记**：同一个 agent 的模型给出的自述是**不可信的**——
它一边编造 `runTests`、`create_session` 这类不存在的工具名，一边说「没有 circe 相关的函数」。
**要判「工具到底有没有送到模型手里」，只能看我们收到了什么请求。**

追踪原文（客户端标识已在里面）：

```
22:49:20 C→S server/discover  clientInfo={"name":"copilot-cli","version":"1.0.92-4.unstable…"}
22:49:20 C→S initialize       protocolVersion=2025-11-25
22:49:20 C→S notifications/initialized
         —— 这次连接**从未调用 tools/list**

22:54:03 （第二次连接，同样的握手）
22:54:06 C→S tools/list       {"_meta":{"progressToken":0}}
22:54:06 S→C ← **四个工具全部发出**（`circe_readiness_probe` / `circe_core_profile_query` /
             `circe_trust_update` / `circe_ledger_verify`，下划线名字、描述完整、inputSchema 完整）
         —— 此后**一次 tools/call 都没有**
```

**所以这一层我们是完全正确的，而模型手里一个 circe 工具都没有。**

而那个 agent 的模型自己把工具**逐个列了出来**——18 个，全是浏览器自动化与会话管理：

```
hoverInPage typeInPage runPlaywrightCode handleDialog runTests testFailure
addComment listComments replyToComment deleteComments resolveComments
viewUnreviewedComments list_sessions get_current_session create_session
send_message get_session_context delete_session
```

**不只没有 circe，连 `github-mcp-server` 的工具也没有**——即该 agent 交给模型的工具集**不含任何 MCP 工具**。

**而它接下来做的事很说明问题**：没有查询接口，它就**用浏览器去"打开"核剖面**——
`runPlaywrightCode` + `pageId="agent-b-session"` → `Page not found` → 换成 session URI →
再失败 → **同一段推理循环了十几遍**（8B 模型在长上下文里的典型退化）。

> **`Page 'agent-b-session' not found` 这个错误，就是「调用方没有工具可用」的样子。**
> 它把一个 API 查询当成了网页操作。

**这一层为什么值得单列**：挂载一共要过五层，前四层都验过了——

| 层 | 状态 |
|---|---|
| ① 协议版本兼容 | ✅ Claude Code 探测两代、VS Code 都过 |
| ② 服务器被启动 | ✅ 进程树证过（§三之二） |
| ③ 握手完成 | ✅ `Connection state: Running`（§三之三） |
| ④ **服务器把工具发出去** | ✅ **本次追踪证过**（四个工具 + 完整 schema） |
| ⑤ **客户端把工具交给模型** | ✅ **已证（23:09:33）——但有条件**：换成**原生模型**之后才通，见下 |

#### 而它当晚就通了——条件是「换成原生模型」

**同一个客户端**（`copilot-cli` v1.0.92）、**同一份配置**，只有一处不同：

| 会话用的模型 | 模型手里的工具 | 结果 |
|---|---|---|
| BYOK 本地 `qwen3-vl:8b` | 18 个，**全是内置的浏览器/会话工具** | ❌ 编造浏览器路径，循环十几遍 |
| **原生 `Auto → gpt-6-luna`** | **含 circe 的四个** | ✅ **真的调了** |

追踪原文（**这是第一条来自第三方客户端的 `tools/call`**）：

```
23:09:33 C→S tools/call   → circe_ledger_verify
23:09:33 S→C ← {"entries": 5, "head": "4366966748c55661137ace6c0b526fc392f9dd671582f2eb690…"}
```

**所以第 ⑤ 层不在我们手里，而在「客户端 × 模型」这个组合里**——
同一个客户端，对 BYOK 模型只给内置工具，对原生模型才给 MCP 工具。
**（这是两次观察支持的推断，不是证明；变量没有完全控制。）**

#### 而那个模型做的第一件事，正是描述里要它做的

权限询问弹出来之前，它先把四个工具**逐个复述**了一遍，并自己下了一个判断：

> 如果要依据核剖面判断是否信任它，**应先校验账本**；如果要记录调用，
> 则应在调用完成后如实上报。

**「先校验账本」这句不在它的训练数据里——它写在 `circe_ledger_verify` 的 description 里。**
而它接下来调用的**正是** `circe_ledger_verify`。

> **这就是 README §「四个工具」那段设计理由的实证**：
> **描述里没有它会的词，它就找不到这个工具；而写对了词，它自己会把顺序推出来。**
> 而且这一次的调用方是**第三方客户端 + 前沿模型**，不是我们自己写的客户端。

**而它报出的四个数，与我独立手算的逐位相同**（2026-10-12 23:10）：

| 维度 | 模型报告 | 独立复算 |
|---|---|---|
| 深度 | `0.310345` | `1.8/(1.8+4) = 0.3103448275…` → round 6 = **0.310345** ✅ |
| 密度 | `0.601732` | `((5/55) + 1 + (1 − 400/1400))/3 = 0.6017316017…` → **0.601732** ✅ |
| 跨度 | `0.166667` | `(1/6 + 1/6)/2 = 0.1666666666…` → **0.166667** ✅ |

原始量同样一致：**5 次调用 · 成功率 100% · 平均耗时 400 ms · 平均栈深 1.8 · 1 位 caller · 1 种 skill**。

**调用序列（追踪原文）**：

```
23:09:33  circe_ledger_verify       args={}                    ← 它先验账本
23:10:44  circe_core_profile_query  args={"explain": true}      ← 再查剖面
```

> **顺序不是我们教它的，是它从两句 description 里推出来的；而数字对得上，
> 是本模块的核心主张（「确定性统计量、不含 LLM 判断」）在
> 第三方客户端 + 前沿模型这条链路上第一次被完整走通并复核。**

#### 而第 6 条记录把**洞五现场复现了**（2026-10-12 23:15:03）

四个工具在 6 分钟内全部被真实调用，顺序完全正确（追踪原文）：

```
23:09:33  circe_ledger_verify        {}
23:10:44  circe_core_profile_query   {"explain": true}
23:14:32  circe_readiness_probe      {}
23:15:03  circe_trust_update         {caller:"agent-A", latency_ms:500,
                                      skill:"检索", stack_depth:1, success:true,
                                      timestamp:"2026-10-12T23:14:56.483+08:00"}
23:15:23  circe_ledger_verify        {}                   ← **写完它自己又验一次**
23:15:25  circe_core_profile_query   {"explain": true}     ← **再查，看变化**
```

**「写完再验、再查」是它自己做的——我们没有要求。洞四的回归由调用方自发完成。**

**三件要记的：**

**① `skill` 是模型自己填的自由文本，而它当场把 `span` 抬高了 35.7%。**

```
调用前:  skills = {'qa'}          → span = (sat(1,5)+sat(1,5))/2 = 0.166667
调用后:  skills = {'qa','检索'}   → span = (sat(1,5)+sat(2,5))/2 = 0.226190
```

**一次普通调用、一个自由文本字段，核剖面一个维度涨 35.7%——纯粹因为换了个名字。**
而账本里存的仍是原话 `"skill": "检索"`——**归一只发生在派生统计层**（§二之四 F1 的取舍）。

> **这是洞五第一次在第三方客户端 + 前沿模型上被复现，而不是我们脚本制造的。**

**② `timestamp` 是宿主注入的，不是模型编的。** 它的形状说明了一切：

```
"2026-10-12T23:14:56.483+08:00"     带毫秒、带时区偏移
调用发生在 23:15:03                  ← 早了约 7 秒
```

**宿主在模型开始生成之前就把时间取好了；模型不会产出 `.483` 这种精度。**
所以 F2/F3 那条描述（「**必须由宿主/调用方把当前时间填进来**」）在真实链路上成立——
**而服务端的 ±300 秒校验放行了它**（那条修法第一次作用于真实客户端）。

**③ 前 5 条账本是测试留下的（`skill='qa'`），第 6 条是外部客户端写的第一条。**

**所以那一格「断在第五层」的结论也修正了**：断的不是客户端，是**「客户端 × 模型」这个组合**——
同一个 `copilot-cli`，对 BYOK 本地模型只给 18 个内置工具，对原生模型才给 MCP 工具。
**换模型就好了，不必换客户端。**

#### 词表修法也在真实客户端上验成了（2026-10-12 23:32）

F1 的修法是**受控词表**（`--skill-vocabulary`）。它此前只在 `--selftest` 里验过。
这次在同一个第三方客户端、同一个模型上验：

配置 `--skill-vocabulary "qa,检索"`，然后让模型再写**两个新技能名**（「翻译」「摘要」）。

**预测（写在前面的）**：`span` 应为 `0.270833`；若词表不生效则为 `0.305556`。
**实测：`0.270833`。** 密度 `0.616347`、深度 `0.272727` 也都逐位命中。
（只写**一个**新名字是区分不出来的——`unspecified` 自己也是一个新桶，两者都得 0.270833。
**两个才能把词表开与关分开。**）

而**账本与剖面的分工同时被验**：

```
账本里的原话（4 种）: qa, 摘要, 检索, 翻译        ← 一字未改
剖面的桶名（3 种）  : qa, unspecified, 检索       ← 2 个噪声名 → 1 个桶
```

**N 个噪声名 → 1 个桶，而不是 N 个桶。** 这就是受控词表买到的东西。

**而那个模型自己发现了这件事**——它报告的最后一句是：

> 注意：返回的技能列表是 qa、unspecified、检索，**没有包含刚记录的「翻译」和「摘要」**。

**没有人提示它。** 它自己比对了两处、发现不一致、如实报出来——
**而这是「账本存原话、剖面存桶名」这条边界第一次由外部调用方指出。**

#### 两个顺带记下的新观察

**① 宿主给的时间戳是「轮次级」的，不是「调用级」的。** 两次 `trust_update`
分别在 `23:32:14` 与 `23:32:17` 发出，但**两条记录的 `timestamp` 一模一样**：

```
23:32:14  trust_update  {"skill":"翻译", ... "timestamp":"2026-10-12T23:30:19.368+08:00"}
23:32:17  trust_update  {"skill":"摘要", ... "timestamp":"2026-10-12T23:30:19.368+08:00"}
```

宿主在**用户发出那条消息时**取了一次时间（23:30:19），两条记录复用它。两件事随之成立：

- **这再次证明时间戳不是模型写的**——模型两次独立生成，不可能产生**逐毫秒相同**的值。
- **同一轮内的多次调用在账本上无法在时间上区分**——而 `decay_weight(age)` 正是按
  `timestamp` 衰减的。（本次偏差约 **115 秒与 118 秒**，仍在 ±300 秒容差内，所以被接受。
  **那条容差确实在承重。**）

**② 缺必填字段时，强模型问、弱模型编。** 第二次让它记录两笔调用，它**回头来要 `latency_ms`**：

> 两次账本记录都需要 latency_ms。请分别提供……若没有精确值，给出你认可的估算值也可以。

而先前那个 8B 模型面对必填的 `timestamp` **直接编了一个**。

**两者对我们都不理想**：编的那个被 ±300 秒校验挡下（**那是正确行为**），
问的那个**把一次自动调用变成了人机往返**——**而调用方是 agent 时，没有人可以问。**

> **这是比洞五更靠前的一个形态**：洞五是**值填得不对**（自由文本没受控），
> 这一条是**根本填不出来**（必填项落在模型的知识边界之外）。
> 两次实测合起来指向同一件事：**schema 的必填项不能包含只有宿主才知道的东西，
> 除非宿主负责注入它——而「宿主会不会注入」不在我们手里。**

#### 描述改完之后：新会话里它先查了清单，再复用旧名（2026-10-12 23:42）

把 `skill` 描述从「**不确定时用 `unspecified`**」改成
「**不确定该用哪个词时，先调 `circe_core_profile_query`——它会返回该 agent
已有的技能名清单，能对上就复用**」（`unspecified` 退成最后兜底），
然后在**一个全新会话**里给一个**只描述、不命名**的任务：

> 报告一次对 agent-B 的调用，调用方 agent-A，成功，耗时 400 毫秒，
> **这次的任务是把一份中文合同译成英文**

客户端日志（`agenthost.log`，逐字）：

```
23:42:45.515  sendMessage: "……把一份中文合同译成英文"
23:42:45.657  MCP server 'circe' connected (ready)
23:42:53.892  Tool started: circe-circe_core_profile_query     ← ★ 先查清单
23:43:23.723  Tool completed: call_1EL8d24Sxk28wWcRGwnDotBj
23:43:27.250  Tool started: circe-circe_trust_update          ← 再写（34 秒后）
23:43:29.000  Tool completed: call_8dQDUHcUpq34YNSaxKXWrBEc
```

**过程和结果都对**：查剖面在前、写账本在后，填的是**已有的** `翻译`，
`span` 停在 `0.305556` 而不是涨到 `0.333333`（密度 `0.621335`、深度 `0.265306` 也逐位命中）。

**为什么必须开新会话**：旧会话的**对话里已经出现过技能清单**（它自己报告过
「返回的技能列表是 qa、unspecified、检索」）。在那里接着问，复用旧名可能只是**记得**——
**测的是记忆，不是描述。** 这一条是这次实验最容易搞错的地方。

**而必须如实说清的一点：这不是受控 A/B。** 上一次失败那轮，提示词**直接指定了**
技能名（「第一次技能用『翻译』，第二次技能用『摘要』」）——**模型没有理由去查清单**。
所以本轮证明的是「**新指令在一个未命名的任务上可被遵循**」，
**它没有把「描述」孤立成原因**。要归因，得用同一句未命名的提示词跑一遍旧描述做对照。

#### 对照补上了：把描述改回去，行为就变了（2026-10-12 23:47）

上一节说那**不是受控 A/B**，所以补了一个。同一句**逐字相同**的未命名提示词、
同一个客户端、同一个模型、**两次都是全新会话**，**只改描述这一处**：

| | 旧描述（对照） | 新描述（处理组） |
|---|---|---|
| 写入前调 `core_profile_query` | ❌ **没有**——本次会话**只有一次**工具调用 | ✅ **有**——查完隔 34 秒才写 |
| 写进账本的 `skill` | `翻译` | `翻译` |
| `span` | `0.305556` | `0.305556` |

客户端日志（`agenthost.log`）逐字：

```
旧描述   23:47:54.025  sendMessage
         23:48:03.509  Tool started: circe-circe_trust_update        ← 直接写，没查

新描述   23:42:53.892  Tool started: circe-circe_core_profile_query  ← 先查
         23:43:27.250  Tool started: circe-circe_trust_update         ← 再写
```

**所以：描述改动是原因。** 唯一变量是描述，行为差异恰好是描述所要求的那个动作。

**而这张表里最该看的是「`span` 那一列两边一样」——结果层根本区分不出来。**
对照那轮它**没查，却仍然填对了** `翻译`：因为「中译英合同」的自然名字**刚好**
就在账本里。

> **这解释了洞五为什么难被发现。** 旧描述下，调用方**不查也可能碰对**——
> 只要它的自然命名与已有名字重合。**错误只在两者不重合时才显形**
> （先前那轮填「摘要」就是这种情形：自然、合理，但账本里没有）。
> **所以这条洞在结果层是间歇的，在过程层是必然的。**
> **判它只能看过程——而过程要求仪器装在客户端一侧**（见上一节）。

#### 仪器本身的一课：客户端的日志比我们自己的追踪看得更多

**这一轮我犯了一个顺序错误**：先在配置清理里删掉了 `CIRCE_MCP_TRACE`，
**然后才去跑一个必须靠它才能判「过程」的实验**。追踪里因此没有这一段。

退路是客户端自己的日志。而它给出的东西**比我们的追踪更多**：

```
23:30:30.700  Tool started: ask_user          ← 那次「问我要 latency_ms」
23:56:13.530  Tool started: runPlaywrightCode ← 浏览器工具
23:56:21.519  Tool started: list_sessions     ← 会话工具
```

**`ask_user`、`runPlaywrightCode`、`list_sessions` 都不经过 MCP——我们的追踪
永远看不到它们。** 而它们恰恰是判断「客户端把模型放进了什么工具环境」的关键。

> **所以对「挂载第 ⑤ 层」这类问题，仪器应该选在客户端一侧：**
> 它不受我们的配置影响，而且能看见全部工具调用（含内置的）。
> **服务器侧的追踪仍然不可替代**——它记的是**协议原文**（`tools/list` 回了什么、
> `tools/call` 的参数逐字），那是客户端日志给不了的。

---

## 四、两个卡点，以及各自不需要账号的替代路径

### 卡点一：Claude Code 的 `-p`（模型侧验证）——被**账号暂停**挡住

```
2026-10-10 22:22   Anthropic 账号被暂停（详见 账号暂停-应对.md）
```

`/login` + `claude -p` 这条路**现在不可用**，而且**不宜反复重试**：
`账号暂停-应对.md` §一 记的是 8 分钟内 12 次账号级调用是**起因的候选之一**。
**所以本轮没有碰 Anthropic 账号，一次都没有。**

**替代路径**：本地 Ollama（三个带 tools 能力的模型）。它证明的是
「一个 LLM 能够」，标注见 §零。**这条路径可复现、不依赖任何云账号、没有账号风险。**

### 卡点二：VS Code Copilot Chat——被**未登录**挡住，但有绕法

`renderer.log` 里的原话：

```
[AgentHost] No signed-in session resolved for resource: https://api.github.com   （反复出现）
[ChatModelSelection] event=no-model-at-toolbar-build                              （反复出现）
```

**没有 GitHub 登录 → 没有模型 → Chat 里问不了。**

**而这里有一个不需要任何账号的绕法**，本轮查证到：

| 事实 | 证据 |
|---|---|
| VS Code 1.141.0 **自带** `github.copilot-chat` | `2a59476c9b\resources\app\extensions\copilot`；`renderer.log` 提到 `'github.copilot-chat' appears in product.json` |
| 它**支持 Ollama BYOK** | 扩展 `package.json` 里 `ollama` 命中 5 处、`Byok` 8 处、`customendpoint` 3 处 |
| 本地已有三个带 tools 能力的模型 | `qwen3-vl:8b` / `qwen2.5:7b` / `deepseek-r1:14b` |

**即：在 VS Code 的模型选择器里走 "Manage Models…" → Ollama，选本地模型，
就能在 Copilot Chat 里问 circe —— 不需要 GitHub 账号，也不需要 Copilot 订阅。**
（本轮未实测这条 UI 路径：它需要桌面，见下。）

### 附带：VS Code 从本会话起不来

```
Start-Process Code.exe  →  pid=26568  HasExited=True  ExitCode=1   （无任何输出）
会话 3，UserInteractive=True
```

所以「打开 VS Code」「在 Chat 里问一句」这两步**确实只能你来做**——
与 `挂载指南.md` §四 的判断一致，这次拿到了确切的退出码。

---

## 五、这一轮的形状

`账号暂停-应对.md` §五 记的第 11 次是：

> **验证 MCP 挂载**这个动作本身是有效的，**而它依赖于「验证动作不触发账号风控」**。

本轮是它的下一步：**当那条依赖被打破时，去找一条不依赖它的路。**
Ollama 那条路给出的东西**比原来那条更多**——因为它不只是"能不能连上"，
它把模型放到了调用方的位置上，于是 CIRCE **自己的**字段设计被照出来了：

```
F1  skill 是自由文本     →  span 里混进调用方模型的噪声
F2  timestamp 由调用方给 →  模型必然编一个
F3  那个守卫被 max(·,0) 消掉 →  编出来的时间戳还能拿满权重
```

**这三条都不是 MCP 的问题，是 CIRCE 的问题。**
它们只在**另一端坐着一个真的 LLM** 时才显形——
这恰好就是 §零 那张表第一行那句话要证明的东西。
