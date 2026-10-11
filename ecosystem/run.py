"""CIRCE 多元生态 —— N 个 agent 互相调用，观察 span 是否分化。

跑法:
    python CIRCE/ecosystem/run.py --agents 5 --rounds 3
    python CIRCE/ecosystem/run.py --agents 5 --rounds 3 --dry      # 不真跑模型（快）

它要回答的问题（用户 2026-10-10 提出，而这是 CIRCE 从未面对过的检验）
--------------------------------------------------------------------
**`span` 从未被真正测试过。** 二元演示里 callee 只有一个 caller，
span 恒为 0 或趋近 0。三个维度里两个被测试了（depth、density），一个没有。

本脚本建一个 N 元生态，让 agent **自主**互相调用，然后观察：

| 观察项 | 能回答什么 |
|---|---|
| span 是否分化 | 不同 agent 的 span 有稳定差异，还是全部趋同 |
| 时间衰减的真实尺度 | 半衰期在当前场景里是否合适 |
| 白纸问题是否自然解决 | 新 agent 加入时剖面如何从 0 生长 |
| 自主调用是否发生 | 没有脚本时，agent 会不会主动调用别的 agent |
| 核剖面是否被读取 | caller 的 LLM 是否真在决策中用了它 |

**关于"没有 ground truth"**（用户指出的障碍 3）
---------------------------------------------
你无法验证核剖面是否**正确**——因为"真实的不可替代性"在生态里没有定义。
所以本脚本**不试图验证正确性**，只观察三件事：**是否稳定、是否分化、是否随时间演化。**

**稳定性本身就是信号。**

关于"LLM 不会自主调用"（障碍 1）
--------------------------------
解法就是用户说的：**给每个 agent 一个目标 + 工具调用循环**，让它自己决定。
本脚本给每个 agent 一个 JSON 形式的可用工具表与一句目标，
然后**解析它自己选的工具**——而不是我替它选。

这是本脚本与 demo_real_models.py 的关键区别：
后者是脚本驱动的（我显式要求 caller 调用 callee），
**前者要求 agent 自己决定要不要调用、调用谁。**
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CIRCE = HERE.parent
for _p in (str(CIRCE), str(CIRCE / "protocol"), str(CIRCE / "trust")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import CoreProfile, StaticProvider        # noqa: E402
from server import AgentState                            # noqa: E402

OLLAMA = "http://127.0.0.1:11434"


# --------------------------------------------------------------------------
def ask(model: str, prompt: str, timeout: float = 240.0) -> tuple[str, float]:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.7, "num_predict": 220}}).encode()
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
# 一个 agent：能力 + 技能 + 账本 + 可选的真实模型
# --------------------------------------------------------------------------
@dataclass
class EcoAgent:
    name: str
    skills: list[str]
    model: str | None = None          # None = 不真跑模型（用规则替代）
    data_dir: Path | None = None
    personality: str = ""             # 影响它自主决策的倾向
    state: AgentState | None = None
    served: int = 0                   # 被调用次数（本生态内部计数，用于对照）
    given: int = 0                    # 它发出的调用次数

    def init(self) -> None:
        prof = CoreProfile(depth=0.0, density=0.0, span=0.0,
                           agent_id=self.name, skills=tuple(self.skills))
        assert self.data_dir is not None
        self.state = AgentState(self.name, prof,
                                StaticProvider({self.name: prof}), self.data_dir)

    def answer(self, question: str, dry: bool) -> tuple[str, bool, int]:
        """作为被调用方，回答一个问题。返回 (回答, 成功, 毫秒)。"""
        self.served += 1
        if dry or self.model is None:
            # 规则替代：技能越匹配越"成功"
            time.sleep(0.002)
            return (f"[{self.name}] 关于「{question[:24]}」的答复（模拟）",
                    True, 5)
        try:
            txt, dt = ask(self.model, question)
            return txt, bool(txt), int(dt * 1000)
        except Exception as e:
            return f"(失败: {e})", False, 0


# --------------------------------------------------------------------------
# 自主决策：给 agent 一个目标 + 工具表，让它自己选
# --------------------------------------------------------------------------
def tools_table(peers: list[EcoAgent], self_name: str) -> str:
    """给 agent 的工具表。

    ⚠️ **不可以用 `<占位符>`**（2026-10-10，真实模型实验抓出）：
    初版的表里写的是
        {"tool": "call", "target": "<对方名字>", "skill": "<技能>", ...}
    而真实模型**照抄了 `<对方名字>`**——它输出了一个字面量占位符，
    于是 4 次调用全部指向不存在的对象，账本 0 条，span 全 0。

    这不是「LLM 不愿自主调用」（那是用户预判的障碍 1），
    而是**prompt 让它可以偷懒**：既然示例里给了尖括号，抄下来就是合法答案。

    所以改成**用真实的对象名做示例**——模型看到的是一个完整可用的例子，
    而不是一个需要它自己填空的模板。
    """
    others = [p for p in peers if p.name != self_name]
    example_target = others[0].name if others else "（没有别人）"
    example_skill = (others[0].skills[0] if others and others[0].skills else "general")

    lines = ["可用动作。你只能选下面三种之一，输出**一个 JSON 对象**：", ""]
    lines.append(f'  {{"tool": "call", "target": "{example_target}", '
                 f'"skill": "{example_skill}", "question": "你的问题"}}')
    lines.append('  {"tool": "query", "target": "先看对方的核剖面再决定"}')
    lines.append('  {"tool": "no_action", "reason": "为什么什么都不做"}')
    lines.append("")
    lines.append("**target 必须是下面列出的真实名字之一，不能写占位符。**")
    lines.append("对方：")
    for p in others:
        lines.append(f'  - "{p.name}"  技能: {", ".join(p.skills)}')
    return "\n".join(lines)


# 允许 markdown 围栏与前后闲聊，取第一个平衡的 JSON 对象
DECISION_RE = re.compile(r'\{[^{}]*"tool"\s*:\s*"[^"]+"[^{}]*\}', re.S)


def extract_json(text: str) -> dict | None:
    """从模型输出里取出那个 JSON 对象。

    比初版宽松的地方（初版只用一个正则，agent-C 的 428 字符输出没被解析）：
      · 剥掉 ```json 围栏
      · 若有多个候选，取**第一个含 "tool" 的**
      · 允许对象内有嵌套（逐字符配平，而不是靠正则）
    """
    t = text.strip()
    # 剥 markdown 围栏
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)

    # 逐字符找第一个配平的 {...}
    depth = 0
    start = -1
    for i, c in enumerate(t):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                cand = t[start:i + 1]
                try:
                    d = json.loads(cand)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(d, dict) and "tool" in d:
                    return d
                start = -1
    # 退回正则（应对对象里含嵌套导致配平失败的情况）
    m = DECISION_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def decide(agent: EcoAgent, peers: list[EcoAgent], goal: str,
           history: list[str], dry: bool, two_stage: bool = False) -> dict:
    """让 agent 自己决定做什么。解析它输出的 JSON。

    `two_stage=True` 时走**两阶段**：先 `query` 取到核剖面，**再把剖面交给它决定**。

    ⚠️ 为什么要两阶段（2026-10-10，第一次生态实验的发现）：
    单阶段时「查询剖面」与「决定调用」是**并列的两个动作**，于是观测到
        agent-E 查询了 3 次剖面，一次都没调用
    ——**它读了，但读与决策没有因果连接。**

    而 CIRCE 声称的恰恰是「**读到剖面之后**据此决定是否调用」。
    单阶段实验**答不了这个问题**，因为它从不让剖面进入决策的输入。

    两阶段把问题变成可回答的：**同一个 agent，给它看剖面 vs 不给它看，
    它的调用选择会不同吗？** 那才是「剖面是否影响决策」的检验。
    """
    if dry or agent.model is None:
        # 规则替代：随机挑一个 peer 或什么都不做
        others = [p for p in peers if p.name != agent.name]
        r = random.Random(hash((agent.name, len(history))) & 0xFFFF)
        if not others or r.random() < 0.15:
            return {"tool": "no_action", "reason": "没有合适的对象"}
        t = r.choice(others)
        sk = t.skills[0] if t.skills else "general"
        return {"tool": "call", "target": t.name, "skill": sk,
                "question": f"{agent.name} 问 {t.name}：请就 {sk} 给一句判断"}

    recent = "\n".join(f"  - {h}" for h in history[-6:]) or "  （还没有行动）"

    # ---- 第一阶段（仅 two_stage）：先把所有 peer 的剖面摆出来 ----
    profiles_block = ""
    if two_stage:
        lines = ["对方的核剖面（来自调用历史，三个维度都在 [0,1]）："]
        for p in peers:
            if p.name == agent.name or p.state is None:
                continue
            prof = p.state.current_profile()
            # 先算好再拼——**f-string 里嵌同类引号在 Python 3.10 会解析失败**
            # （初版写成一个 f-string 套 " 与 '，报 unterminated string literal）
            sk = ", ".join(p.skills)
            line = ('  - "' + p.name + '"  技能: ' + sk
                    + '  深度=' + format(prof.depth, '.3f')
                    + ' 密度=' + format(prof.density, '.3f')
                    + ' 跨度=' + format(prof.span, '.3f')
                    + ' 被调用=' + str(prof.call_count) + '次')
            lines.append(line)
        lines.append("")
        lines.append("含义：深度=被调用时位于调用栈的哪一层；"
                     "密度=频次/成功率/速度的合成；"
                     "跨度=被多少个**不同**的 caller 调用过。"
                     "三个都为 0 表示**还没有被任何人调用过**（新加入的）。")
        profiles_block = "\n".join(lines) + "\n\n"
        n_prof = sum(1 for p in peers if p.name != agent.name and p.state)
        print("      （两阶段：已把 " + str(n_prof) + " 个剖面喂进决策）")

    prompt = (
        f"你是多智能体生态里的一个 agent，名叫 {agent.name}。\n"
        f"你的技能：{', '.join(agent.skills)}\n"
        f"你的人格倾向：{agent.personality}\n\n"
        f"你的目标：{goal}\n\n"
        f"{profiles_block}"
        f"{tools_table(peers, agent.name)}\n\n"
        f"你最近做过的事：\n{recent}\n\n"
        + ("请**依据上面那些核剖面**决定调用谁——"
           "优先依赖不可替代性高的（跨度/密度高的），"
           "还是要支持还没被调用过的（三个维度为 0 的）？由你判断。\n"
           if two_stage else "")
        + f"现在请输出**一个 JSON 对象**表示你的决定。"
          f"例如 {{\"tool\": \"no_action\", \"reason\": \"我自己的技能足够\"}}。"
          "**只输出这个 JSON，前后不要任何解释文字。**")
    try:
        txt, _ = ask(agent.model, prompt, timeout=300)
    except Exception as e:
        return {"tool": "no_action", "reason": f"决策失败: {e}"}

    d = extract_json(txt)
    if d is None:
        return {"tool": "no_action",
                "reason": f"没能解析出 JSON（输出 {len(txt)} 字符）"}
    return d


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=3,
                    help="每个 agent 每轮做一次决策")
    ap.add_argument("--dry", action="store_true", help="不真跑模型（快）")
    ap.add_argument("--join-late", action="store_true",
                    help="让最后一个 agent 在中途加入（观察白纸问题）")
    ap.add_argument("--two-stage", action="store_true",
                    help="两阶段决策：先把核剖面喂进 prompt，再让它决定（检验剖面是否影响决策）")
    ap.add_argument("--check-models", action="store_true",
                    help="先筛模型：只保留能稳定输出合法 JSON 的（避免把模型能力混进生态观测）")
    args = ap.parse_args()

    print("=" * 78)
    print("  CIRCE 多元生态 —— 观察 span 是否分化")
    print("=" * 78)

    models: list[str] = []
    if not args.dry:
        try:
            models = list_models()
        except Exception as e:
            print(f"  ✗ Ollama 不可达，退回 --dry: {e}")
            args.dry = True

    # ---- 筛模型：把「模型能力」与「生态结构」分开 ----
    # 第一次生态实验里，qwen3-vl:8b 对同一个 prompt **从未**产出合法 JSON
    # （422–477 字符，三次全失败），而另两个模型都能解析。
    # **那意味着观测到的 span 分布部分反映了模型能力，而不是生态结构**——
    # 一个不能决策的 agent 在生态里等于一个不会被调用的 agent。
    # 所以要跑长期实验，先把这一层噪声筛掉。
    if args.check_models and not args.dry:
        print("\n  先筛模型（同一 prompt，看能否稳定输出合法 JSON）")
        probe = ('只输出一个 JSON 对象，不要任何其他文字：\n'
                 '{"tool": "no_action", "reason": "测试"}')
        good: list[str] = []
        for m in models:
            ok_n = 0
            for _ in range(2):
                try:
                    txt, _ = ask(m, probe, timeout=120)
                    if extract_json(txt) is not None:
                        ok_n += 1
                except Exception:
                    pass
            verdict = "可用" if ok_n == 2 else f"不稳定（{ok_n}/2）"
            print(f"    {m:<18} {verdict}")
            if ok_n == 2:
                good.append(m)
        if good:
            models = good
            print(f"    → 保留 {len(models)} 个: {', '.join(models)}")
        else:
            print("    → 没有模型能稳定输出 JSON，退回 --dry")
            args.dry = True
    print(f"  模式: {'DRY（规则替代，不跑模型）' if args.dry else '真实模型'}"
          f"{' · 两阶段决策' if args.two_stage else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="circe-eco-"))
    rng = random.Random(20261010)

    # ---- 建 agent ----
    SKILL_POOL = [["编码"], ["写作"], ["分析"], ["检索"], ["规划"], ["问答"]]
    PERSONAS = ["倾向于独自完成，不轻易求助", "倾向于频繁协作",
                "只在自己做不了时才求助", "喜欢验证别人的说法",
                "倾向于先把任务拆开再分配"]
    names = [f"agent-{chr(65+i)}" for i in range(args.agents)]
    agents: list[EcoAgent] = []
    for i, nm in enumerate(names):
        mdl = None if args.dry else models[i % len(models)]
        a = EcoAgent(name=nm, skills=SKILL_POOL[i % len(SKILL_POOL)],
                     model=mdl, data_dir=tmp / nm, personality=PERSONAS[i % len(PERSONAS)])
        a.init()
        agents.append(a)

    # 中途加入的那个：起始账本为空，且**在别人已有历史之后才出现**
    late = None
    if args.join_late:
        late = EcoAgent(name="agent-NEW", skills=["通用"],
                        model=None if args.dry else models[0],
                        data_dir=tmp / "agent-NEW", personality="刚加入，还不了解别人")
        late.init()

    for a in agents:
        print(f"  {a.name:<10} 技能={','.join(a.skills):<8} "
              f"模型={a.model or '（规则）'}")
    if late:
        print(f"  {late.name:<10} 技能=通用      （中途加入）")

    goal = "尽可能完成自己的任务，并让整个生态的产出更好。你可以调用别人，也可以不调用。"

    # ---- 交互循环 ----
    print(f"\n  开始 {args.rounds} 轮自主决策（每个 agent 每轮一次）")
    print("-" * 78)
    log: list[str] = []
    decisions = {"call": 0, "query": 0, "no_action": 0, "other": 0}
    span_events: list[tuple[str, str]] = []      # (caller, callee)

    for rd in range(args.rounds):
        print(f"\n  ── 第 {rd+1} 轮 ──")
        for a in agents:
            peers = agents + ([late] if late else [])
            d = decide(a, peers, goal, log, args.dry, two_stage=args.two_stage)
            tool = d.get("tool", "other")
            decisions[tool] = decisions.get(tool, 0) + 1
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            if tool == "call":
                tname = d.get("target")
                target = next((p for p in peers if p.name == tname), None)
                if target is None:
                    print(f"    {a.name}: 想调 {tname}（不存在）→ 跳过")
                    log.append(f"{a.name} 试图调用不存在的 {tname}")
                    continue
                q = d.get("question", "请给一句判断")
                ans, ok, ms = target.answer(q, args.dry)
                a.given += 1
                # 真：caller 把这次调用写进 callee 的账本
                target.state.update(__import__("endpoints").UpdateRequest(  # type: ignore
                    caller=a.name, target=target.name,
                    skill=d.get("skill") or (target.skills[0] if target.skills else "general"),
                    success=ok, latency_ms=ms, timestamp=ts,
                    note="生态内自主调用",
                    stack_depth=1 + rd % 3))
                span_events.append((a.name, target.name))
                short = ans.replace("\n", " ")[:44]
                print(f"    {a.name} → {target.name}  ({ms:>5} ms, "
                      f"{'✓' if ok else '✗'})  {short}")
                log.append(f"{a.name} 调用了 {target.name}（{d.get('skill')}）")
            elif tool == "query":
                tname = d.get("target")
                target = next((p for p in peers if p.name == tname), None)
                if target:
                    p = target.state.current_profile()
                    print(f"    {a.name} 查询 {target.name} 的剖面: "
                          f"d={p.depth:.3f} den={p.density:.3f} sp={p.span:.3f}")
                    log.append(f"{a.name} 查了 {target.name} 的核剖面")
            elif tool == "no_action":
                print(f"    {a.name}: 不行动（{str(d.get('reason'))[:40]}）")
                log.append(f"{a.name} 选择不行动")
            else:
                print(f"    {a.name}: 给了个不认识的工具 {tool!r}")

        # 中途加入
        if late and rd == args.rounds // 2:
            print(f"\n    ★ {late.name} 加入生态（它的账本是空的）")
            agents.append(late)

    # ---- 观测 ----
    print("\n" + "=" * 78)
    print("  观测")
    print("=" * 78)
    all_agents = agents + ([late] if late and late not in agents else [])

    print("\n  ① 核剖面（这是 span 第一次有真实值）")
    print(f"     {'agent':<12}{'depth':>8}{'density':>9}{'span':>8}"
          f"{'被调用':>8}{'发出':>7}  技能")
    rows = []
    for a in all_agents:
        p = a.state.current_profile()
        rows.append((a, p))
        print(f"     {a.name:<12}{p.depth:>8.3f}{p.density:>9.3f}{p.span:>8.3f}"
              f"{a.served:>8}{a.given:>7}  {','.join(a.skills)}")

    spans = [p.span for _, p in rows]
    densities = [p.density for _, p in rows]
    print(f"\n  ② span 是否分化")
    if spans:
        uniq = len({round(s, 6) for s in spans})
        print(f"     span 值: {[round(s,4) for s in spans]}")
        print(f"     不同取值的个数: {uniq} / {len(spans)}")
        print(f"     极差: {max(spans) - min(spans):.4f}")
        if uniq == 1:
            print("     → **全部趋同**：在当前交互规模下 span 不区分 agent")
        else:
            print(f"     → **分化了**（{uniq} 个不同取值）")
            # 分化是否与真实被调用次数一致？那是最基本的自洽检查
            callers = {}
            for _, p in rows:
                callers[p.agent_id] = len({c for c, t in span_events if t == p.agent_id})
            print(f"     真实不同 caller 数: "
                  f"{ {k: v for k, v in callers.items()} }")
            agree = all(
                (spans[i] > spans[j]) == (callers[rows[i][0].name] > callers[rows[j][0].name])
                for i in range(len(rows)) for j in range(len(rows))
                if callers[rows[i][0].name] != callers[rows[j][0].name])
            print(f"     span 序与真实 caller 数序一致: {agree}")

    print(f"\n  ③ 自主调用是否发生")
    total = sum(decisions.values())
    print(f"     决策总数 {total}: " +
          ", ".join(f"{k}={v}" for k, v in sorted(decisions.items())))
    if decisions.get("call", 0) == 0:
        print("     → **没有发生任何调用**（障碍 1 成真：agent 不主动求助）")
    else:
        print(f"     → 发生了 {decisions['call']} 次调用，"
              f"{decisions.get('no_action',0)} 次选择不行动")

    print(f"\n  ④ 账本与链（真实写入）")
    for a in all_agents:
        n = len(a.state.ledger.entries)
        ok = a.state.ledger.verify() == []
        print(f"     {a.name:<12} {n:>3} 条  链完整={ok}  "
              f"head={a.state.ledger.head[:12]}…")

    print(f"\n  ⑤ 白纸问题（新 agent 加入时）")
    if late:
        p = late.state.current_profile()
        print(f"     {late.name} 被调用 {late.served} 次，"
              f"剖面 d={p.depth:.3f} den={p.density:.3f} sp={p.span:.3f}")
        if late.served == 0:
            print("     → **它没有被任何人调用过**：剖面全 0，"
                  "而别的 agent 都没去调它")
            print("       （这正是「第一次调用问题」：谁愿意先调一个剖面为空的对象）")
        else:
            print(f"     → 它被调用了 {late.served} 次，剖面已开始生长")
    else:
        print("     （--join-late 未启用）")

    print(f"\n  ⑥ 时间衰减的真实尺度")
    print("     本次运行时长在秒级，半衰期（默认 30 天）内的衰减 ≈ 1.0。")
    print("     **要测真实尺度需要跑几小时到几天**——那是本脚本给不出的，")
    print("     但它已经把要观测的量（每次调用的时间戳）写进了账本。")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
