"""验证 span 修正与 choice_entropy 的读法。

跑法:  python CIRCE/tests/selftest_span_correction.py

它验证四件事——**每一件都是 2026-10-10 那轮实验逼出来的**：

  ① 旧 span 分不开两种情形：
       A：同一个 caller 在 1 种任务上调 5 次
       B：同一个 caller 在 5 种任务上各调 1 次
     旧定义给同一个值；**而两者的不可替代性含义完全不同**
     （A 可能只是结构性锁定）。新定义 `span_by_task` 能把它们分开。

  ② `H=0` 有两种解释，光看 H 分不开：
       结构性锁定（候选池只有 1 个）
       能力性锁定（候选池 >1，但一个明显更好）
     `choice_entropy` 必须同时报出候选池大小与跨任务是否换伙伴。

  ③ 跨任务换伙伴要能被检出。

  ④ 新方法不改变旧方法的行为（旧 span 的值必须先冻结，避免悄悄改了它）。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CIRCE = HERE.parent
for _p in (str(CIRCE / "protocol"), str(CIRCE / "trust")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from stats_provider import CallRecord, StatsProvider  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


def mk(caller, callee, skill, n=1, start=0):
    return [CallRecord(caller=caller, callee=callee, skill=skill,
                       timestamp=f"2026-10-10T10:{(start+i) % 60:02d}:00Z",
                       success=True, latency_ms=100, stack_depth=1)
            for i in range(n)]


def main() -> int:
    print("=" * 72)
    print("  span 修正与 choice_entropy 的读法")
    print("=" * 72)

    print("\n  ① 旧 span 的 task 维度**本来就在**（我起初的推断是错的）")
    A = mk("solo", "X", "coding", n=5)                       # 1 种任务 × 5 次
    B = [CallRecord(caller="solo", callee="X", skill=f"task{i}",
                    timestamp=f"2026-10-10T10:0{i}:00Z", success=True,
                    latency_ms=100, stack_depth=1) for i in range(5)]   # 5 种任务 × 1 次

    spA, spB = StatsProvider(records=A), StatsProvider(records=B)
    oldA, oldB = spA.span("X"), spB.span("X")

    print(f"     A（1 种任务 × 5 次）:  旧 span={oldA}")
    print(f"     B（5 种任务 × 1 次）:  旧 span={oldB}")
    # ⚠️ 初版这里断言 `oldA == oldB`（"旧 span 分不开"），**那是错的**：
    # 旧 span 里的 `skills` 就是任务数，所以它本来就能分开。
    # 断言建立在错误的推断上，报出来的失败指向测试而不是代码。
    check("旧 span 已经区分了这两种情形（因为 skills 就是任务数）",
          oldA != oldB, f"{oldA} vs {oldB} —— task 维度本来就在")

    print("\n  ①之二 真正的差别：配对计数会把两种情形混成一个值")
    # 旧 span 用 callers+tasks 分别计数；配对计数用 (caller,task)
    #   情形 P: caller1 做A + caller2 做A   → callers=2, tasks=1, pairs=2
    #   情形 Q: caller1 做A + caller1 做B   → callers=1, tasks=2, pairs=2
    P = mk("c1", "X", "A", n=1) + mk("c2", "X", "A", n=1, start=5)
    Q = mk("c1", "X", "A", n=1) + mk("c1", "X", "B", n=1, start=10)
    spP, spQ = StatsProvider(records=P), StatsProvider(records=Q)
    dP, dQ = spP.span_decomposed("X"), spQ.span_decomposed("X")
    print(f"     P（2 人做同一件事）: callers={dP['callers']} tasks={dP['tasks']} "
          f"pairs={dP['pairs']}  旧span={dP['span']}  新span={dP['span_by_task']}")
    print(f"     Q（1 人做两件事）  : callers={dQ['callers']} tasks={dQ['tasks']} "
          f"pairs={dQ['pairs']}  旧span={dQ['span']}  新span={dQ['span_by_task']}")
    check("配对计数把两者混成同一个值（所以它不该单独用于诊断）",
          dP["pairs"] == dQ["pairs"] == 2)
    check("分解后的 callers/tasks 能区分它们",
          dP["callers"] != dQ["callers"] and dP["tasks"] != dQ["tasks"])
    check("P 被判为结构性锁定嫌疑", "结构性" in dP["reading"], dP["reading"])
    check("Q 未被判为结构性锁定", "结构性" not in dQ["reading"], dQ["reading"])

    print("\n  ② H=0 的两种解释必须能被区分")
    # C：候选池只有 1 个，跨任务都调它 → 结构性锁定
    C = mk("c", "only", "coding", n=2) + mk("c", "only", "writing", n=2, start=10)
    # D：候选池 2 个，但 writing 上固定调一个 → 那个 H=0 可能是能力性锁定
    D = (mk("d", "expert", "writing", n=3)
         + mk("d", "other", "coding", n=1, start=20))

    ec = StatsProvider(records=C).choice_entropy("c")
    ed = StatsProvider(records=D).choice_entropy("d")
    print(f"     C（候选池 {ec['callee_pool_size']}）: {ec['reading']}")
    print(f"     D（候选池 {ed['callee_pool_size']}）: {ed['reading']}")

    check("C 被判为结构性锁定", "结构性" in ec["reading"], ec["reading"])
    check("D 的 H=0 未被误判为结构性锁定", "需要 alignment" in ed["reading"],
          ed["reading"])
    check("C 的两个任务 H 都是 0", all(v["H"] == 0 for v in ec["per_task"].values()))
    check("D 在 writing 上 H=0（固定调一个）",
          ed["per_task"]["writing"]["H"] == 0,
          str(ed["per_task"]["writing"]["dist"]))

    print("\n  ③ 跨任务换伙伴要能被检出")
    check("C 不换伙伴（两个任务 top 相同）",
          ec["switches_across_tasks"] is False, str(ec["distinct_tops"]))
    check("D 换伙伴（coding 与 writing 的 top 不同）",
          ed["switches_across_tasks"] is True, str(ed["distinct_tops"]))

    # 反向自检：一个明确乱挑的 caller，entropy 该高
    E = []
    for i, callee in enumerate(["p", "q", "r", "s"]):
        E += mk("e", callee, "mixed", n=1, start=30 + i)
    ee = StatsProvider(records=E).choice_entropy("e")
    check("乱挑的 caller entropy 高（与 H=0 形成对照）",
          ee["per_task"]["mixed"]["H"] > 1.5,
          f"H={ee['per_task']['mixed']['H']}")

    print("\n  ④ 旧 span 的行为未被改动（值必须与修正前一致）")
    # 旧 span 只依赖 caller 数与 task 数，公式未变
    ref = StatsProvider(records=mk("a", "X", "coding", n=1)
                        + mk("b", "X", "writing", n=1, start=5)
                        + mk("c", "X", "coding", n=1, start=10))
    v = ref.span("X")
    # (saturate(3,5) + saturate(2,5)) / 2 = (0.375 + 0.2857142857142857)/2
    manual = round((3 / 8 + 2 / 7) / 2, 6)
    check("旧 span 公式未变（能手算核对）", v == manual, f"{v} vs 手算 {manual}")

    # 新方法的边界
    empty = StatsProvider(records=[])
    check("空记录时 span_by_task = 0", empty.span_by_task("X") == 0.0)
    check("空记录时 choice_entropy 给出说明而非崩溃",
          "note" in empty.choice_entropy("nobody"))

    print()
    print("=" * 72)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 72)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
