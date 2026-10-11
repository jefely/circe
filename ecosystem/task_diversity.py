"""任务多样性实验 —— 区分结构性不可替代性与能力性不可替代性。

跑法:
    python CIRCE/ecosystem/task_diversity.py --dry          # 规则替代，快
    python CIRCE/ecosystem/task_diversity.py                # 真实模型

用户指出的那个更深的区分（2026-10-10）
--------------------------------------
前一个实验测出：**固定配对的生态里每个 callee 只有一个 caller，span 结构上无法分化。**
而那揭示了一个 CIRCE 尚未区分的东西：

    结构性不可替代性   caller 没有别的选择        → 固定配对里高，多元任务里低
    能力性不可替代性   callee 确实独特            → 两种情况下都高

**agent-B 在固定配对里的 span 是 0.167，但它的不可替代性不是真的**——
它是被 agent-A 结构性地锁定了。换个生态，agent-A 可能找到更好的替代品。

**所以 CIRCE 目前测的是「被调用分布」，不是「不可替代性」。**

本实验要把它分开
----------------
```
agent 分两类：
  专家类（2 个）：各有一个明确强项（coding / writing）
  通用类（2 个）：没有强项

任务分两类：coding / writing
```

三个观测量：

| 观测量 | 能区分什么 |
|---|---|
| **专家类 agent 的 span 是否分化** | 多个 caller 是否指向同一个 callee（任务多样性是否打破固定配对） |
| **同一 callee 在不同 task_type 下的 density** | 能力是否有方向性——专家该在它的强项上更高 |
| **`choice_entropy = H(callee \| caller, task_type)`** | **有真正的偏好**（高熵）还是**结构性锁定**（低熵） |

关于 entropy 的读法（**这一点容易读反**）
----------------------------------------
    H = 0        → 在所有任务类型下都调同一个 callee → **可能是结构性锁定**
    H 高         → 不同任务类型下调不同 callee       → **有真正的偏好**

**但要注意**：H 高也可能是"随便挑"（与能力无关的随机）。
所以本脚本同时算一个**能力对齐度**：被选中的 callee 在该 task_type 上
是不是真的更强。**偏好 + 对齐，才是能力性不可替代性。**
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sys
import tempfile
import time
import urllib.request          # ← 初版漏了这行。
# 而它只在【非 dry】路径上被用到（list_models / ask），
# 所以 DRY 模式跑了一路都没暴露——**同一个脚本的两条路径，
# 只有联网那条会碰到这个缺失。** 这与 CIRCE 里那几条教训同形：
# 一条路径验证过了，不等于另一条也能走。
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CIRCE = HERE.parent
for _p in (str(CIRCE), str(CIRCE / "protocol"), str(CIRCE / "trust")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import CoreProfile, StaticProvider, UpdateRequest  # noqa: E402
from server import AgentState                                     # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
TASKS = ["coding", "writing"]


def ask(model: str, prompt: str, timeout: float = 240.0) -> tuple[str, float]:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.7, "num_predict": 200}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    txt = (d.get("response") or "").strip() or (d.get("thinking") or "").strip()
    return txt, time.time() - t0


def list_models() -> list[str]:
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=10) as r:
        return [m["name"] for m in json.loads(r.read().decode())["models"]]


# --------------------------------------------------------------------------
@dataclass
class Agent:
    name: str
    kind: str                       # "expert" | "general"
    strong: str | None              # 专家的强项 task_type
    model: str | None = None
    data_dir: Path | None = None
    state: AgentState | None = None
    served_by: list[tuple[str, str]] = field(default_factory=list)  # (caller, task)

    def init(self) -> None:
        prof = CoreProfile(depth=0.0, density=0.0, span=0.0, agent_id=self.name,
                           skills=(self.strong,) if self.strong else ("general",))
        assert self.data_dir is not None
        self.state = AgentState(self.name, prof,
                                StaticProvider({self.name: prof}), self.data_dir)

    def apparent_skill(self, task: str) -> float:
        """它在这个 task_type 上**看起来**有多强（供 caller 判断）。

        专家在强项上 1.0、非强项上 0.25；通用在两者上都 0.55。
        **这是 caller 能观察到的能力**——本实验要检验的就是
        caller 能不能据此选出对的 callee。
        """
        if self.kind == "expert":
            return 1.0 if task == self.strong else 0.25
        return 0.55

    def answer(self, task: str, question: str, dry: bool) -> tuple[str, bool, int]:
        if dry or self.model is None:
            time.sleep(0.002)
            return (f"[{self.name}/{task}] 模拟答复", True, 5)
        try:
            txt, dt = ask(self.model, f"（任务类型：{task}）{question}")
            return txt, bool(txt), int(dt * 1000)
        except Exception as e:
            return f"(失败: {e})", False, 0


# --------------------------------------------------------------------------
def ask_tools(model: str, prompt: str, tools: list[dict],
              timeout: float = 300.0) -> tuple[str, dict | None, str, float]:
    """用 **tool calling** 让模型做决策，而不是让它生成 JSON 文本。

    为什么这条最可能成功（用户 2026-10-10 的判断，本函数就是那个尝试）
    ------------------------------------------------------------------
    上一轮实测：同一个 `qwen2.5:7b` 在 `run.py` 的简单 prompt 下能稳定产出 JSON，
    在含剖面块的复杂 prompt 下 12/16 失败。**问题可能不是模型能力，是任务形式。**

        **结构化输出是 tool calling 的原生任务，不是自由文本生成的任务。**

    让模型「生成一个合法的 JSON 字符串」要求它同时做两件事：
    ① 决定调用谁 ② 把决定编码成语法正确的 JSON。**第二件事会失败。**
    而 tool calling 把第二件事交给运行时——模型只需要产出参数。

    返回 (函数名, 参数字典 或 None, 原始文本, 秒)。

    ⚠️ 函数名在 `function.name` 里，**不在 arguments 里**——
    初版只看 arguments，于是丢了"它到底调了哪个工具"这个信息。
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 200},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    dt = time.time() - t0

    msg = d.get("message") or {}
    calls = msg.get("tool_calls") or []
    if calls:
        fn = ((calls[0].get("function") or {}).get("name")) or ""
        args = (calls[0].get("function") or {}).get("arguments")
        # 有些运行时给 dict，有些给 JSON 字符串
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = None
        if isinstance(args, dict):
            return fn, args, msg.get("content") or "", dt
        return fn, None, msg.get("content") or "", dt
    return "", None, (msg.get("content") or ""), dt


def decision_tools(peers: list[Agent], self_name: str, task: str) -> list[dict]:
    """给 tool calling 的工具定义。

    与自由文本方案的关键差别：**target 的取值被 enum 限死**。
    模型不可能输出一个不存在的对象名——那正是上一轮
    「想调 <对方名字>（不存在）」那个 bug 的成因（prompt 里给了占位符）。
    """
    names = [p.name for p in peers if p.name != self_name]
    return [
        {
            "type": "function",
            "function": {
                "name": "call_agent",
                "description": f"调用另一个 agent 来处理这个 {task} 任务。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": names,
                                   "description": "要调用的对方"},
                        "question": {"type": "string",
                                     "description": "你要问它什么"},
                    },
                    "required": ["target", "question"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "do_nothing",
                "description": "自己处理，不调用任何人。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string",
                                   "description": "为什么自己处理"},
                    },
                    "required": ["reason"],
                },
            },
        },
    ]


def decide_via_tools(agent: Agent, peers: list[Agent], task: str,
                     history: list[str]) -> dict:
    """tool calling 版的决策。**剖面只给三个数字**（方案 C 的另一半）。"""
    others = [p for p in peers if p.name != agent.name]

    # C 的要点之一：**只给三个数字，不给解释串与 VC**
    lines = [f"对方的核剖面（三个 [0,1] 的数）："]
    for p in others:
        if p.state is None:
            continue
        pr = p.state.current_profile()
        lines.append("  " + p.name + "  深度=" + format(pr.depth, ".3f")
                     + "  密度=" + format(pr.density, ".3f")
                     + "  跨度=" + format(pr.span, ".3f"))
    lines.append("（三个都是 0 = 还没被任何人调用过）")

    names = "、".join(p.name for p in others)
    prompt = (
        "你是 " + agent.name + "，技能是 " + (agent.strong or "通用") + "。\n"
        "现在有一个【" + task + "】任务。\n\n"
        + "\n".join(lines) + "\n\n"
        "可选：" + names + "\n\n"
        "请决定：调用其中某一个，还是自己做。"
        "（看到什么就据此判断，没有标准答案。）")

    tools = decision_tools(peers, agent.name, task)
    try:
        fn, args, content, _ = ask_tools(agent.model, prompt, tools)
    except Exception as e:
        return {"tool": "no_action", "reason": "决策失败: " + str(e)}

    if args is None:
        # 退回：模型没用 tool call，试试解析它的文本
        d = decision_regex_json(content) or first_line_json(content)
        if d is not None:
            d["_via"] = "文本退回"
            return d
        return {"tool": "no_action",
                "reason": "没有 tool_call，文本也没解析出（" + str(len(content)) + " 字符）"}

    # 工具名 → 内部动作（name 在 function.name 里，见 ask_tools 的说明）
    if fn == "do_nothing":
        return {"tool": "no_action", "reason": args.get("reason", ""), "_via": "tool"}
    if fn == "call_agent" and "target" in args:
        return {"tool": "call", "target": args["target"],
                "question": args.get("question") or ("请处理一个 " + task + " 任务"),
                "_via": "tool"}
    # 名字缺失但参数长得像调用
    if "target" in args:
        return {"tool": "call", "target": args["target"],
                "question": args.get("question") or ("请处理一个 " + task + " 任务"),
                "_via": "tool(无名)"}
    return {"tool": "no_action",
            "reason": "tool_call 认不出: fn=" + repr(fn) + " args=" + str(args)}


def decision_regex_json(text: str) -> dict | None:
    """从模型输出里取第一个含 tool 的平衡 JSON 对象（剥 markdown 围栏）。"""
    import re
    t = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    t = re.sub(r"\s*```\s*$", "", t)
    depth, start = 0, -1
    for i, c in enumerate(t):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    d = json.loads(t[start:i + 1])
                    if isinstance(d, dict) and "tool" in d:
                        return d
                except json.JSONDecodeError:
                    pass
                start = -1
    return None


def first_line_json(text: str) -> dict | None:
    """宽容版：找出文本里**任意**一个含 tool 的 JSON 对象（跨行、可嵌套一层）。"""
    import re
    for m in re.finditer(r"\{", text):
        for end in range(len(text), m.start(), -1):
            if text[end - 1] != "}":
                continue
            try:
                d = json.loads(text[m.start():end])
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and "tool" in d:
                return d
    return None


def decide(agent: Agent, peers: list[Agent], task: str,
           history: list[str], dry: bool, guided: bool) -> dict:
    """让 agent 为**指定的 task_type** 选一个 callee。

    `guided=False` 时**只给剖面、不给任何倾向性提示**——
    这是用户要求的「无引导对照」：去掉「支持还没被调用过的」那句话，
    看它是否仍然会那样做。
    """
    others = [p for p in peers if p.name != agent.name]
    if dry or agent.model is None:
        r = random.Random(hash((agent.name, task, len(history))) & 0xFFFF)
        if not others or r.random() < 0.12:
            return {"tool": "no_action", "reason": "自己处理"}
        # 规则版：按 apparent_skill 加权挑（模拟一个"看懂能力"的 caller）
        ws = [max(p.apparent_skill(task), 0.05) for p in others]
        t = r.choices(others, weights=ws, k=1)[0]
        return {"tool": "call", "target": t.name, "task": task,
                "question": f"请帮我处理一个 {task} 任务"}

    # 剖面块（两阶段的核心）——只给数据，不给倾向
    lines = ["对方的核剖面（来自调用历史，三维都在 [0,1]）："]
    for p in others:
        if p.state is None:
            continue
        pr = p.state.current_profile()
        # 注意：本文件里的 Agent 用 `strong`（强项 task_type），不是 `skills`——
        # 初版从 run.py 复制时留下了 `p.skills`，于是这条路径一跑就 AttributeError。
        sk = p.strong or "通用"
        lines.append('  - "' + p.name + '"  技能: ' + sk
                     + "  深度=" + format(pr.depth, ".3f")
                     + " 密度=" + format(pr.density, ".3f")
                     + " 跨度=" + format(pr.span, ".3f")
                     + " 被调用=" + str(pr.call_count) + "次")
    lines.append("（三维全为 0 = 还没被任何人调用过）")
    profile_block = "\n".join(lines)

    names = " / ".join('"' + p.name + '"' for p in others)
    recent = "\n".join("  - " + h for h in history[-4:]) or "  （还没有行动）"

    prompt = (
        "你是多智能体生态里的一个 agent，名叫 " + agent.name + "。\n"
        "你的技能：" + (agent.strong or "通用") + "\n\n"
        "现在有一个 **" + task + "** 类型的任务需要处理。\n\n"
        + profile_block + "\n\n"
        "可选的对方：" + names + "\n"
        "你最近做过的事：\n" + recent + "\n\n"
        + ("请依据上面的核剖面决定调用谁（看到数据自己判断，"
           "没有标准答案）。\n" if guided is False else
           "请依据上面的核剖面决定调用谁。如果某个对象还没被调用过（三维全 0），"
           "你也可以选择支持它。\n")
        + '只输出一个 JSON 对象，例如 {"tool": "call", "target": "'
        + (others[0].name if others else "?") + '", "question": "你的问题"}，'
          '或者 {"tool": "no_action", "reason": "为什么"}。'
          '前后不要任何解释文字。')

    try:
        txt, _ = ask(agent.model, prompt, timeout=300)
    except Exception as e:
        return {"tool": "no_action", "reason": "决策失败: " + str(e)}
    d = decision_regex_json(txt) or first_line_json(txt)
    if d is None:
        return {"tool": "no_action",
                "reason": "没能解析出 JSON（输出 " + str(len(txt)) + " 字符）"}
    d.setdefault("task", task)
    return d


# --------------------------------------------------------------------------
def entropy(counts: Counter) -> float:
    """香农熵（bit）。H=0 表示所有质量集中在一个取值上。"""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / total
        h -= p * math.log2(p)
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--rounds", type=int, default=3, help="每种任务类型跑几轮")
    ap.add_argument("--guided", action="store_true",
                    help="带引导（提示它可支持没被调用过的）；默认**无引导**")
    ap.add_argument("--check-models", action="store_true",
                    help="用与真实决策【同构】的 prompt 筛模型")
    ap.add_argument("--via-tools", action="store_true",
                    help="方案 C：用 **tool calling** 决策，而不是让模型生成 JSON 文本")
    args = ap.parse_args()

    print("=" * 78)
    print("  任务多样性实验 —— 结构性 vs 能力性不可替代性")
    print("=" * 78)

    models: list[str] = []
    if not args.dry:
        try:
            models = list_models()
        except Exception as e:
            print(f"  ✗ Ollama 不可达: {e}")
            args.dry = True

    tmp = Path(tempfile.mkdtemp(prefix="circe-div-"))

    def build(model_pool: list[str]) -> list[Agent]:
        spec = [("expert-A", "expert", "coding"),
                ("expert-B", "expert", "writing"),
                ("general-C", "general", None),
                ("general-D", "general", None)]
        out = []
        for i, (nm, kind, strong) in enumerate(spec):
            m = None if args.dry else model_pool[i % len(model_pool)]
            a = Agent(name=nm, kind=kind, strong=strong, model=m,
                      data_dir=tmp / (nm + "-" + str(i)))
            a.init()
            out.append(a)
        return out

    # ---- ④（提前做）用同构 prompt 筛模型 ----
    if args.check_models and not args.dry:
        print("\n  筛模型 —— 用与真实决策【同构】的 prompt")
        probe_agents = build(models)
        good: list[str] = []
        for m in models:
            # 同构：真的人格/目标/工具表/剖面块/历史，只是规模是 1 轮
            probe_agents[0].model = m
            ok_n = 0
            for t in TASKS:
                d = decide(probe_agents[0], probe_agents, t,
                           ["（测试）"], False, guided=False)
                if d.get("tool") != "no_action" or "没能解析" not in str(d.get("reason", "")):
                    ok_n += 1
            verdict = "可用" if ok_n == len(TASKS) else f"不稳定（{ok_n}/{len(TASKS)}）"
            print(f"    {m:<18} {verdict}")
            if ok_n == len(TASKS):
                good.append(m)
        if good:
            models = good
            print(f"    → 保留 {len(models)} 个: {', '.join(models)}")
        else:
            print("    → 没有模型在同构 prompt 下稳定，退回 --dry")
            args.dry = True

    agents = build(models if models else ["x"])
    print(f"\n  模式: {'DRY' if args.dry else '真实模型'}"
          f" · {'带引导' if args.guided else '**无引导**'}"
          f" · {'**tool calling**（方案 C）' if args.via_tools else 'JSON 文本'}")
    via_counts = Counter()
    for a in agents:
        print(f"    {a.name:<12} {a.kind:<8} 强项={a.strong or '—':<8} "
              f"模型={a.model or '（规则）'}")

    # ---- 跑：每种 task_type 各 rounds 轮，每轮每个 agent 决策一次 ----
    print(f"\n  开始：{len(TASKS)} 种任务 × {args.rounds} 轮 × {len(agents)} 个 agent")
    print("-" * 78)
    log: list[str] = []
    # (caller, task) -> Counter(callee)
    choices: dict[tuple[str, str], Counter] = defaultdict(Counter)
    # (caller, task) -> 被选中 callee 的真实能力
    alignment: dict[tuple[str, str], list[float]] = defaultdict(list)
    decisions = Counter()

    for task in TASKS:
        print(f"\n  ══ 任务类型: {task} ══")
        for rd in range(args.rounds):
            for a in agents:
                if args.via_tools and not args.dry and a.model is not None:
                    d = decide_via_tools(a, agents, task, log)
                    via_counts[d.get("_via", "?")] += 1
                else:
                    d = decide(a, agents, task, log, args.dry, guided=args.guided)
                    via_counts["JSON 文本"] += 1
                tool = d.get("tool", "other")
                decisions[tool] += 1
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                if tool != "call":
                    print(f"    [{task} r{rd+1}] {a.name}: 不行动"
                          f"（{str(d.get('reason'))[:34]}）")
                    log.append(f"{a.name} 在 {task} 上选择不行动")
                    continue

                tname = d.get("target")
                target = next((p for p in agents if p.name == tname), None)
                if target is None:
                    print(f"    [{task} r{rd+1}] {a.name}: 目标 {tname!r} 不存在")
                    continue

                q = d.get("question") or f"请处理一个 {task} 任务"
                ans, ok, ms = target.answer(task, q, args.dry)
                choices[(a.name, task)][target.name] += 1
                alignment[(a.name, task)].append(target.apparent_skill(task))
                target.served_by.append((a.name, task))
                # 真：写进 callee 账本，skill 用 task_type
                target.state.update(UpdateRequest(
                    caller=a.name, target=target.name, skill=task,
                    success=ok, latency_ms=ms, timestamp=ts,
                    note=f"任务多样性 {task}", stack_depth=1 + rd % 3))
                print(f"    [{task} r{rd+1}] {a.name} → {target.name}  "
                      f"({ms:>5} ms, {'✓' if ok else '✗'})  "
                      f"能力={target.apparent_skill(task):.2f}")
                log.append(f"{a.name} 在 {task} 上调用 {target.name}")

    # ---- 观测 ----
    print("\n" + "=" * 78)
    print("  观测")
    print("=" * 78)

    print("\n  ① 核剖面（注意 span 的定义现在有歧义——见 §三）")
    print(f"     {'agent':<12}{'kind':<9}{'depth':>8}{'density':>9}{'span':>8}"
          f"{'被调用':>7}  被谁调过")
    for a in agents:
        p = a.state.current_profile()
        callers = sorted({c for c, _ in a.served_by})
        print(f"     {a.name:<12}{a.kind:<9}{p.depth:>8.3f}{p.density:>9.3f}"
              f"{p.span:>8.3f}{len(a.served_by):>7}  {','.join(callers) or '—'}")

    print("\n  ② 专家是否被多个 caller 指向（任务多样性打破了固定配对吗）")
    for a in agents:
        callers = {c for c, _ in a.served_by}
        tasks = {t for _, t in a.served_by}
        print(f"     {a.name:<12} 不同 caller {len(callers)} 个 "
              f"({','.join(sorted(callers)) or '—'})  "
              f"覆盖任务 {','.join(sorted(tasks)) or '—'}")
    experts = [a for a in agents if a.kind == "expert"]
    multi = [a for a in experts if len({c for c, _ in a.served_by}) >= 2]
    if experts:
        print(f"     → {len(multi)}/{len(experts)} 个专家被 ≥2 个 caller 调用")

    print("\n  ③ 同一 callee 在不同 task_type 下的 density（能力是否有方向性）")
    print("     （用账本按 skill=task 过滤后重算，而不是看账面那一个数）")
    for a in agents:
        line = f"     {a.name:<12}"
        for t in TASKS:
            n = sum(1 for c, tt in a.served_by if tt == t)
            line += f"  {t}: {n} 次"
        line += f"   强项={a.strong or '—'}"
        print(line)

    print("\n  ④ choice_entropy = H(callee | caller, task_type)")
    print("     H=0 → 在所有任务下都调同一个 → **可能是结构性锁定**")
    print("     H 高 → 不同任务下调不同 callee → **有真正的偏好**")
    per_caller: dict[str, float] = {}
    for a in agents:
        hs, detail = [], []
        for t in TASKS:
            c = choices.get((a.name, t), Counter())
            h = entropy(c)
            hs.append(h)
            detail.append(f"{t}: H={h:.3f} {dict(c)}")
        avg = sum(hs) / len(hs) if hs else 0.0
        per_caller[a.name] = avg
        # 跨任务类型的偏好：把两个 task 的分布合起来看有没有区分
        allc = Counter()
        for t in TASKS:
            allc.update(choices.get((a.name, t), Counter()))
        print(f"     {a.name:<12} 平均 H={avg:.3f}   " + " | ".join(detail))

    # 跨任务区分度：同一 caller 在两个 task 上选的 callee 分布是否不同
    print("\n  ⑤ 跨任务区分度（换任务会不会换伙伴）")
    for a in agents:
        dists = [choices.get((a.name, t), Counter()) for t in TASKS]
        tops = [max(d, key=d.get) if d else None for d in dists]
        same = tops[0] == tops[1] and tops[0] is not None
        print(f"     {a.name:<12} {TASKS[0]}→{tops[0]}  {TASKS[1]}→{tops[1]}   "
              + ("**换伙伴** ✓" if not same else "不换伙伴（固定）"))

    print("\n  ⑥ 能力对齐度（选中的 callee 在该任务上真的更强吗）")
    print("     ——「偏好 + 对齐」才是能力性不可替代性；只有偏好可能是乱挑")
    for a in agents:
        vals = []
        for t in TASKS:
            vals += alignment.get((a.name, t), [])
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        best = 1.0        # 该任务上最好的 callee 的能力
        print(f"     {a.name:<12} 平均选中能力 {avg:.3f}  "
              f"（最优 {best:.2f}，比值 {avg/best:.2f}）")

    print("\n  ⑦ 决策分布")
    print("     " + ", ".join(f"{k}={v}" for k, v in sorted(decisions.items())))
    if via_counts:
        print("     决策走哪条路: "
              + ", ".join(f"{k}={v}" for k, v in sorted(via_counts.items())))
        # ⚠️ 这一行是方案 C 的成败判据：
        # tool / tool(无名) 的比例 = 有多少决策是**真正靠 tool calling 拿到的**
        # 文本退回 的比例 = 有多少退回了自由文本解析（也就是旧路径）
        tool_n = via_counts.get("tool", 0) + via_counts.get("tool(无名)", 0)
        total = sum(via_counts.values())
        if total:
            print(f"     → tool calling 成功率 {tool_n}/{total} "
                  f"= {tool_n/total:.0%}")

    print("\n  ⑧ 账本")
    for a in agents:
        n = len(a.state.ledger.entries)
        ok = a.state.ledger.verify() == []
        if n:
            print(f"     {a.name:<12} {n} 条  链完整={ok}  head={a.state.ledger.head[:12]}…")
        else:
            print(f"     {a.name:<12} 0 条  （没被调用过）")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
