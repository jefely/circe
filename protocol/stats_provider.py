"""CIRCE 核剖面引擎 —— 从调用日志算统计量。**不含任何 LLM 调用。**

为什么这里没有"确定性关口"
--------------------------
`CIRCE/README.md` §五 曾把一个不存在的问题写成关口，理由是
「NSP 的核剖面需要 LLM 判断会不会塌陷」。那个论证把 **同名不同层** 的东西平移了：

    NSP   的核剖面：对一份【文本 / 概念图】做删除测试  → 需要 LLM 判断
    CIRCE 的核剖面：对一个【agent 的调用历史】做统计   → 不需要 LLM

**本模块是后者。** 三个维度全部由调用日志的计数与比率得出——
同一个日志跑两次，得到的 `core_hash` 逐位相同（`selftest` 里有跨进程断言）。

三维的定义（按作者裁决，2026-10-10）
------------------------------------
    深度 depth    被调用时处于调用栈第几层 / 被依赖的集成层级
    密度 density  单位时间内的调用频次 + 成功率 + 平均耗时
    跨度 span     被多少个不同的 caller 调用过 / 跨多少种任务类型

**为什么必须归一化到 [0,1]**
---------------------------
三个维度都是**原始计数**的自然函数：调用次数、caller 个数、栈深——
它们会随交互无界增长。而「不可替代性」要在不同 agent 之间**可比**，
所以需要一个**单调、有界、且确定性**的映射。

这里用饱和函数 `x / (x + k)`：
  · 单调递增，值域 [0,1)——计数越大越接近 1，永不越界
  · `k` 是「半饱和点」：计数等于 k 时值为 0.5，**是个人能读懂的刻度**
  · 纯算术，无随机、无迭代、无浮点顺序依赖 → 确定性

**不用 sigmoid / tanh 的理由**：那两者需要 exp，而 exp 在不同 libm 上
最后一位可能不同。**链上锚定的东西不该依赖 libm 的实现细节。**
`x/(x+k)` 只有一次除法。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

# 让本模块能被直接跑（`python stats_provider.py`）。
#
# ⚠️ 这已经是同一个错误的**第四次**：给一个模块加了 sys.path 注入，
# 却漏了与它成对的那个。前三次是 vc.py → client.py → 本文件。
# 每一次都是「改了一处、忘了另一处」，而每次都被自包含检查抓出。
# 集中在这里写清楚，是为了下一次加模块时能看见这条规律：
# **protocol/ 下每个模块都需要这四行，因为它同时依赖 endpoints（同目录）
# 与 trust（上一级）。**
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import CoreProfile  # noqa: E402


# --------------------------------------------------------------------------
# 饱和归一化
# --------------------------------------------------------------------------
def saturate(x: float, k: float) -> float:
    """把 [0,∞) 的计数映射到 [0,1)。x=k 时恰为 0.5。

    x/(x+k) 而不是 1-exp(-x/k)：后者要 exp，而 exp 的最后一位随 libm 变。
    链上锚定的量不该依赖 libm。
    """
    if k <= 0:
        raise ValueError("半饱和点 k 必须为正")
    if x < 0:
        raise ValueError(f"计数不能为负: {x}")
    return x / (x + k)


# --------------------------------------------------------------------------
# 一条调用记录（链上存的就是这个 —— 事实，不是判断）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CallRecord:
    caller: str
    callee: str
    skill: str
    timestamp: str          # ISO-8601
    success: bool
    latency_ms: int
    stack_depth: int = 1    # 本次调用发生在调用栈第几层

    def to_dict(self) -> dict:
        return {"caller": self.caller, "callee": self.callee, "skill": self.skill,
                "timestamp": self.timestamp, "success": self.success,
                "latency_ms": self.latency_ms, "stack_depth": self.stack_depth}


# --------------------------------------------------------------------------
# skill 归一（2026-10-10 新增 —— 一个真实 LLM 调用方把它逼出来的）
# --------------------------------------------------------------------------
UNSPECIFIED_SKILL = "unspecified"


def skill_key(skill: str | None, vocabulary: frozenset[str] | None = None) -> str:
    """把一个调用方自报的 skill 归一到**用于计数**的那个字符串。

    ⚠️ **为什么这个函数必须存在**（`CIRCE/mcp/FINDINGS.md` §二 F1）：

    `span` 是核剖面三维之一，而它的一个输入是
    [`span()`](#L147) 里的 `len({r.skill for r in rs})` ——
    **`skill` 是调用方自报的自由文本，schema 里没有词表、没有示例。**

    把三个本地模型放到调用方的位置上，同一个 agent、同一个 caller、各记录一次，
    它们填出了三个不同的 skill：

        qwen2.5:7b（给时钟） → "core_profile_query"   ← 把工具名当技能
        qwen2.5:7b（无时钟） → "circe"                ← 把服务器名当技能
        qwen3-vl:8b（给时钟）→ "general"

    而 `saturate(x,k)=x/(x+k)`、`k_types=5` 下：

        1 个 caller，1 个一致的 skill → span = 0.166667
        1 个 caller，3 个噪声 skill   → span = 0.270833   （×1.625）
        1 个 caller，5 个噪声 skill   → span = 0.333333   （×2.0）

    **而噪声只会单调抬高 span**——skill 种类数不会因为"填错了"而减少。

    于是：核剖面号称「调用日志的确定性统计量，不含 LLM 判断」，
    **而它有一个输入必然由调用方模型决定。统计量是确定性的，输入不是。**

    **取舍（按作者 2026-10-10 的裁决：两条都修）**：

    · **账本保真**——归一**只发生在派生统计这一层**，账本里写下的仍是调用方原话。
      这与本模块自己的立场一致：**账本是真相来源，剖面是它的派生缓存。**
      所以归一放在 `StatsProvider`（读的那一侧），不放在写入的那一侧。
    · `vocabulary=None`（默认）= **不做词表过滤**，只做格式归一（strip、空 → unspecified）。
      所以默认行为与改动前一致，已有测试不受影响。
    · 传了词表 = 词表外的 skill 计入 `UNSPECIFIED_SKILL` 一个桶。
      **这是"保住 span 语义"的那一步**：无论模型编多少花样，它对 `skills` 的贡献是 1。

    **归一到什么词表是部署方的判断，不是代码的判断**——
    所以它是参数（与 `k_calls` 那几个半饱和点同样的处理），不给默认词表。
    """
    s = (skill or "").strip()
    if not s:
        return UNSPECIFIED_SKILL
    if vocabulary is not None and s not in vocabulary:
        return UNSPECIFIED_SKILL
    return s


# --------------------------------------------------------------------------
# 统计提供者
# --------------------------------------------------------------------------
@dataclass
class StatsProvider:
    """从调用记录算核剖面。**纯函数，无状态副作用。**

    三个半饱和点是**可配置的参数，不是魔数**——
    它们的取值决定「多少次调用算多」，而那是部署方的判断，不该内嵌在代码里。
    默认值只是起点，且**必须显式写在调用处**（不允许省略）。
    """
    records: Sequence[CallRecord]
    k_calls: float = 50.0        # 调用次数半饱和
    k_callers: float = 5.0       # 不同 caller 数半饱和
    k_depth: float = 4.0         # 栈深半饱和
    k_types: float = 5.0         # 任务类型数半饱和
    # 受控词表（可选）。None = 只做格式归一；给了词表 = 词表外的 skill 计入
    # UNSPECIFIED_SKILL 一个桶。理由与取舍见 skill_key() 的 docstring。
    skill_vocabulary: frozenset[str] | None = None

    def __post_init__(self) -> None:
        """把 skill 归一到**计数用**的字符串。

        **只改这份派生视图，不改账本**——`records` 是从账本读出来的一份拷贝
        （`records_from_ledger`），归一它不影响真相来源。
        归一只在真的需要时做，于是默认路径不产生额外拷贝。
        """
        out: list[CallRecord] = []
        changed = False
        for r in self.records:
            k = skill_key(r.skill, self.skill_vocabulary)
            if k == r.skill:
                out.append(r)
            else:
                changed = True
                out.append(replace(r, skill=k))
        if changed:
            self.records = out

    def _for(self, agent_id: str) -> list[CallRecord]:
        return [r for r in self.records if r.callee == agent_id]

    # ---- 三个维度 ----
    def depth(self, agent_id: str) -> float:
        """被调用时处于调用栈第几层。

        取【平均栈深】而不是最大栈深：最大栈深由一次偶然的深调用决定，
        会被单个事件主导；平均栈深反映"通常被放在哪一层"。
        """
        rs = self._for(agent_id)
        if not rs:
            return 0.0
        avg = sum(r.stack_depth for r in rs) / len(rs)
        return round(saturate(avg, self.k_depth), 6)

    def density(self, agent_id: str) -> float:
        """调用频次 / 成功率 / 平均耗时的合成。

        三者各自的语义不同，先把每个归一化到 [0,1] 再等权平均——
        **不先把原始量纲混在一起**（次数与毫秒不可直接相加）。

        成功率已经是 [0,1]，不用 saturate。
        耗时要**取反**：越快越"不可替代"。
        """
        rs = self._for(agent_id)
        if not rs:
            return 0.0
        n = len(rs)
        freq = saturate(n, self.k_calls)
        succ = sum(1 for r in rs if r.success) / n
        avg_lat = sum(r.latency_ms for r in rs) / n
        # 延迟用 k=1000ms 的半饱和反相：0ms → 1.0，1000ms → 0.5，∞ → 0
        speed = 1.0 - saturate(avg_lat, 1000.0)
        return round((freq + succ + speed) / 3.0, 6)

    def span(self, agent_id: str) -> float:
        """被多少个不同 caller 调用过 / 跨多少种任务类型。

        两个量都要：一个 agent 可能被一个人调用一万次（caller 数=1），
        也可能被二十个人各调用一次（caller 数=20）。
        **前者是"被依赖"，后者是"被需要"——不可替代性更接近后者。**
        所以 caller 数与类型数等权，而**单纯的调用次数不进 span**（它算在 density 里）。

        ⚠️ **这个定义在 2026-10-10 被实验证明不足**，见 `span_by_task` 与
        `CIRCE/ecosystem/FINDINGS.md` §七：

        > 固定配对的生态里，每个 callee 只有一个 caller，**span 结构上无法分化**。
        > 而这揭示了一个 CIRCE 尚未区分的东西：
        >   **结构性不可替代性**（caller 没有别的选择）vs
        >   **能力性不可替代性**（callee 确实独特）。

        **本方法测的是「被调用分布」，不是「不可替代性」。**
        要区分两者，用 `span_by_task` + `choice_entropy`。
        """
        rs = self._for(agent_id)
        if not rs:
            return 0.0
        callers = len({r.caller for r in rs})
        skills = len({r.skill for r in rs})
        return round((saturate(callers, self.k_callers)
                      + saturate(skills, self.k_types)) / 2.0, 6)

    # ---- 任务多样性（2026-10-10 新增，用于区分两种不可替代性）----
    def span_decomposed(self, agent_id: str) -> dict:
        """把 span 拆成三个量并给出**结构性/能力性**的诊断。

        ⚠️ 一个被实测修正过的推断（2026-10-10，`selftest_span_correction.py`）：
        我起初以为「旧 span 缺 task 维度」。**那是错的**——旧 span 里的 `skills`
        就是任务数。旧定义**已经**能区分「1 种任务 × 5 次」(0.167) 与
        「5 种任务 × 1 次」(0.333)。

        **真正的差别在别处**：旧定义把 `callers` 与 `skills`
        **分别计数再平均**，而用户的修正是用 `(caller, task)` **配对**数。
        配对计数会把下面两种情形混成同一个值：

            caller1 做任务A + caller2 做任务A   → pairs=2
            caller1 做任务A + caller1 做任务B   → pairs=2

        **而这两者的不可替代性含义完全不同：**

            callers 高 + tasks 低  多人都只能找我做**同一件事**  → 结构性锁定嫌疑
            callers 低 + tasks 高  一个人在**多方面**依赖我      → 能力性

        **所以正确的做法不是合并成配对计数，而是把三个量分开报、
        再看它们的关系。** 本方法给出的 `reading` 就是这个关系。
        """
        rs = self._for(agent_id)
        if not rs:
            return {"agent_id": agent_id, "callers": 0, "tasks": 0, "pairs": 0,
                    "span": 0.0, "span_by_task": 0.0,
                    "reading": "没有被调用过（剖面是白纸）"}

        callers = {r.caller for r in rs}
        tasks = {r.skill for r in rs}
        pairs = {(r.caller, r.skill) for r in rs}
        n_c, n_t, n_p = len(callers), len(tasks), len(pairs)

        # 诊断：看「多个 caller」是否带来了「多个任务」
        #   tasks_per_caller ≈ 1 且 callers 多 → 结构性的嫌疑
        #   tasks_per_caller 高                → 能力性的证据
        tpc = n_p / n_c if n_c else 0.0
        if n_c <= 1 and n_t <= 1:
            reading = "只有一个 caller、只有一个任务 —— 无法判断（样本太小）"
        elif n_c > 1 and tpc <= 1.05:
            reading = ("结构性锁定嫌疑：多个 caller 但每个都只在同一件事上找我"
                       f"（{n_c} 个 caller，平均每人 {tpc:.2f} 个任务）")
        elif tpc >= 2.0:
            reading = (f"能力性证据：每个 caller 平均在 {tpc:.2f} 件事上依赖我"
                       "——不只是一件事上的替代品")
        else:
            reading = f"混合（{n_c} caller / {n_t} 任务 / {n_p} 配对，每人 {tpc:.2f} 个任务）"

        return {
            "agent_id": agent_id,
            "callers": n_c,
            "tasks": n_t,
            "pairs": n_p,
            "tasks_per_caller": round(tpc, 4),
            "span": self.span(agent_id),
            "span_by_task": self.span_by_task(agent_id),
            "reading": reading,
        }

    def span_by_task(self, agent_id: str) -> float:
        """`(caller, task_type)` 组合数 —— 用户提出的修正版 span。

        **注意它与 `span` 的差别不是「有没有 task 维度」**
        （旧 `span` 里的 `skills` 已经是任务数），
        而是**是否把 caller 与 task 配起来算**：

            旧：callers 与 tasks 分别计数，再平均
            新：配对数

        配对计数会把「3 人都做同一件事」与「1 人做 3 件事」混成同一个值。
        **所以本方法不该单独用于诊断**——用 `span_decomposed`，
        它把三个量分开报并给出关系。
        """
        rs = self._for(agent_id)
        if not rs:
            return 0.0
        pairs = {(r.caller, r.skill) for r in rs}
        return round(saturate(len(pairs), self.k_callers * self.k_types), 6)

    def choice_entropy(self, caller: str) -> dict:
        """`H(callee | caller, task_type)` —— 有真正的偏好，还是被结构性锁定。

        ⚠️ **读法要点（有实测反例，见 FINDINGS §7.2）**：

            H = 0    → 在所有任务类型下都调同一个 callee
            H 高     → 不同任务类型下调不同 callee

        **但 H=0 有两种完全不同的解释，光看 H 分不开：**

            结构性锁定    候选集里**没有**别的选择
            能力性锁定    候选集里**有**别的选择，但一个明显更好

        实测反例：某个 caller 在 writing 任务上 `H=0`（只调那个 writing 专家），
        但那**正是能力性选择**。

        **所以本方法同时给出 `alignment`**——被选中的 callee 在该 task 上
        是否真的更强（由调用方传入的评分函数决定；这里只算"被选中的 callee
        在它自己的强项上被选中的比例"，那是最保守的一个代理）。

        **「偏好 + 对齐」才是能力性不可替代性。**
        """
        import math
        rs = [r for r in self.records if r.caller == caller]
        if not rs:
            return {"caller": caller, "per_task": {}, "mean_entropy": 0.0,
                    "note": "这个 caller 没有调用记录"}

        by_task: dict[str, dict[str, int]] = {}
        for r in rs:
            by_task.setdefault(r.skill, {})
            by_task[r.skill][r.callee] = by_task[r.skill].get(r.callee, 0) + 1

        per_task = {}
        for task, dist in by_task.items():
            total = sum(dist.values())
            h = -sum((c / total) * math.log2(c / total) for c in dist.values())
            top = max(dist, key=dist.get)
            # 跨任务是否换伙伴：与其它任务的首选是否不同
            per_task[task] = {"H": round(h, 6), "dist": dist, "top": top,
                              "n_callees": len(dist)}

        mean_h = (sum(v["H"] for v in per_task.values()) / len(per_task)) if per_task else 0.0
        tops = {v["top"] for v in per_task.values()}
        # 候选集大小的下限：这个 caller 调用过的不同 callee 总数
        pool = len({r.callee for r in rs})
        return {
            "caller": caller,
            "per_task": per_task,
            "mean_entropy": round(mean_h, 6),
            "distinct_tops": sorted(tops),
            "switches_across_tasks": len(tops) > 1,
            "callee_pool_size": pool,
            # 判别提示：H=0 且候选池只有 1 个 → 结构性锁定；
            #          H=0 但候选池 >1        → 可能是能力性锁定
            "reading": ("结构性锁定（候选池只有 1 个）" if pool <= 1
                        else "H=0 但候选池 >1 —— 需要 alignment 才能判断"
                        if mean_h == 0 else "有跨任务偏好"),
        }

    # ---- 组装 ----
    def profile_for(self, agent_id: str, skill: str | None = None) -> CoreProfile:
        """`CoreProfileProvider` 协议的实现。

        给定 skill 时：只统计该技能维度上的记录（技能条件化的 R(i|k)），
        **但 depth 例外**——栈深是位置属性，不随技能变化。
        """
        rs = self._for(agent_id)
        if skill is not None:
            orig = self.records
            self.records = [r for r in rs if r.skill == skill]   # type: ignore[misc]
            try:
                d, den, sp = self.depth(agent_id), self.density(agent_id), self.span(agent_id)
            finally:
                self.records = orig                             # type: ignore[misc]
            rs_s = [r for r in rs if r.skill == skill]
        else:
            rs_s = rs
            d, den, sp = self.depth(agent_id), self.density(agent_id), self.span(agent_id)

        last = max((r.timestamp for r in rs), default="")
        return CoreProfile(
            depth=d, density=den, span=sp,
            agent_id=agent_id,
            skills=tuple(sorted({r.skill for r in rs})),
            call_count=len(rs_s),
            last_updated=last,
        )

    # ---- 可复核性 ----
    def explain(self, agent_id: str) -> dict:
        """把三个维度还原成原始计数 —— **让每个值都能被人手算核对**。

        一个不可解释的分数在信任基础设施里没有价值：
        调用方看到 depth=0.67 时必须能问「凭什么」，并得到算术答案。

        ⚠️ 解释串必须把 `round(..., 6)` 一并写出（2026-10-10 自检抓出）：
        维度值经过 round 以保证 core_hash 稳定，而初版的解释串显示未 round 的算术，
        于是「手算得 0.415584415…，系统值 0.415584」——**差 4e-7**。
        这个差不会让任何人误判，但它让「可复核」这句话不成立：
        **解释串的意义就在于逐位闭合，差最后一位就已经不是解释了。**
        """
        rs = self._for(agent_id)
        if not rs:
            return {"agent_id": agent_id, "calls": 0,
                    "note": "没有调用记录，三维全为 0（这不是错误，是「尚未被调用」）"}
        n = len(rs)
        callers = sorted({r.caller for r in rs})
        skills = sorted({r.skill for r in rs})
        avg_depth = sum(r.stack_depth for r in rs) / n
        avg_lat = sum(r.latency_ms for r in rs) / n
        succ = sum(1 for r in rs if r.success) / n

        raw_depth = saturate(avg_depth, self.k_depth)
        raw_density = (saturate(n, self.k_calls) + succ
                       + (1.0 - saturate(avg_lat, 1000.0))) / 3.0
        raw_span = (saturate(len(callers), self.k_callers)
                    + saturate(len(skills), self.k_types)) / 2.0

        return {
            "agent_id": agent_id,
            "calls": n,
            "callers": callers, "n_callers": len(callers),
            "skills": skills, "n_skills": len(skills),
            "avg_stack_depth": round(avg_depth, 4),
            "avg_latency_ms": round(avg_lat, 2),
            "success_rate": round(succ, 4),
            "half_saturation": {"k_calls": self.k_calls, "k_callers": self.k_callers,
                                "k_depth": self.k_depth, "k_types": self.k_types,
                                "k_latency_ms": 1000.0},
            # 解释串 = 「未 round 的算术」+ 「round 到 6 位」= 系统值，逐位闭合
            "depth": (f"round(saturate({avg_depth!r}, {self.k_depth}) , 6)"
                      f" = round({raw_depth!r}, 6) = {self.depth(agent_id)}"),
            "density": (f"round((saturate({n}, {self.k_calls}) + {succ!r} "
                        f"+ (1 - saturate({avg_lat!r}, 1000.0))) / 3, 6)"
                        f" = round({raw_density!r}, 6) = {self.density(agent_id)}"),
            "span": (f"round((saturate({len(callers)}, {self.k_callers}) "
                     f"+ saturate({len(skills)}, {self.k_types})) / 2, 6)"
                     f" = round({raw_span!r}, 6) = {self.span(agent_id)}"),
            # 供程序化核对：不解析字符串也能验证「解释 == 值」
            "check": {"depth": raw_depth, "density": raw_density, "span": raw_span},
        }


# --------------------------------------------------------------------------
# 从账本条目重建记录（链上的东西 → 统计的输入）
# --------------------------------------------------------------------------
def records_from_ledger(entries: Iterable) -> list[CallRecord]:
    """把 `trust/chain.py` 的账本条目还原成 CallRecord。

    账本里存的是**事实**（谁调用了谁、成功与否、何时），
    核剖面是从这些事实上算出来的**派生量**——
    所以「改一条历史就会改变核剖面」，而那正是链与剖面之间应有的耦合。
    """
    out: list[CallRecord] = []
    for e in entries:
        p = e.profile if hasattr(e, "profile") else e
        if not isinstance(p, dict):
            continue
        try:
            out.append(CallRecord(
                caller=p.get("caller", "?"), callee=p.get("callee", "?"),
                skill=p.get("skill", "?"), timestamp=str(
                    e.timestamp if hasattr(e, "timestamp") else p.get("timestamp", "")),
                success=bool(p.get("success", False)),
                latency_ms=int(p.get("latency_ms", 0)),
                stack_depth=int(p.get("stack_depth", 1))))
        except (TypeError, ValueError):
            continue
    return out
