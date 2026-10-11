"""核剖面引擎自检 —— 证明它是统计量，因而没有确定性关口。

跑法:  python CIRCE/tests/selftest_stats.py

它要证的五件事，每件都能翻车：

  ① 无 LLM 依赖   源码里不含任何模型调用；同一日志跨进程得到同一个 core_hash
  ② 有界与单调    三个维度恒在 [0,1)，且计数增加时单调不减
  ③ 可解释        每个维度值都能还原成原始计数（解释串里的算术算出来等于它）
  ④ 语义正确      「被依赖」(次数多) 与「被需要」(caller 多) 得分不同
  ⑤ 与链耦合      改一条历史 → 剖面变 → core_hash 变（这是应有的，不是缺陷）
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE.parent / "protocol"
TRUST = HERE.parent / "trust"
for p in (str(PROTO), str(TRUST)):
    if p not in sys.path:
        sys.path.insert(0, p)

from stats_provider import CallRecord, StatsProvider, saturate  # noqa: E402
from chain import core_hash                                    # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


def mk(n_calls: int, n_callers: int, skills=("coding",),
       depth: int = 2, lat: int = 400, ok_ratio: float = 0.9) -> list[CallRecord]:
    out = []
    for i in range(n_calls):
        out.append(CallRecord(
            caller=f"caller-{i % n_callers}", callee="agent-B",
            skill=skills[i % len(skills)],
            timestamp=f"2026-10-10T12:{i % 60:02d}:00Z",
            success=(i / max(n_calls, 1)) < ok_ratio,
            latency_ms=lat, stack_depth=depth))
    return out


# --------------------------------------------------------------------------
def t1_no_llm() -> None:
    print("\n  ① 无 LLM 依赖 —— 这是「门是假的」的直接证据")

    src = (PROTO / "stats_provider.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 收集所有 import 的模块名
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])

    banned = {"openai", "anthropic", "transformers", "torch", "requests",
              "httpx", "urllib", "socket", "ollama", "llama_cpp"}
    hit = mods & banned
    check("不导入任何模型/网络库", not hit, f"imports={sorted(mods)}")

    # 也不该有网络或子进程调用的字面量
    for word in ("http://", "https://", "api_key", "model=", "temperature"):
        check(f"源码里没有 {word!r}", word not in src)

    # 跨进程：同一份日志 → 同一个 core_hash
    recs = mk(40, 6, ("coding", "writing"))
    sp = StatsProvider(records=recs)
    prof = sp.profile_for("agent-B")
    h1 = core_hash(prof.to_dict())

    code = (
        "import sys;"
        f"sys.path.insert(0,r'{PROTO}');sys.path.insert(0,r'{TRUST}');"
        "from stats_provider import CallRecord, StatsProvider;"
        "from chain import core_hash;"
        "recs=[CallRecord(caller=f'caller-{i%6}',callee='agent-B',"
        "skill=('coding','writing')[i%2],timestamp=f'2026-10-10T12:{i%60:02d}:00Z',"
        "success=(i/40)<0.9,latency_ms=400,stack_depth=2) for i in range(40)];"
        "sp=StatsProvider(records=recs);"
        "print(core_hash(sp.profile_for('agent-B').to_dict()))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    check("另一个进程得到同一个 core_hash", r.stdout.strip() == h1,
          f"{h1[:16]}… vs {r.stdout.strip()[:16]}…")
    check("跨进程一致不是靠固定温度/模型版本",
          "temperature" not in src and "model=" not in src,
          "→ 它靠的是「根本没引入不确定性」，不是「把不确定性压住」")


def t2_bounds() -> None:
    print("\n  ② 有界与单调")
    for n in (0, 1, 10, 100, 10000):
        sp = StatsProvider(records=mk(n, max(1, n // 5)))
        p = sp.profile_for("agent-B")
        for d in ("depth", "density", "span"):
            v = getattr(p, d)
            if not (0.0 <= v <= 1.0):
                check(f"n={n} 的 {d} 越界", False, f"{d}={v}")
                return
    check("任意规模下三维都在 [0,1]", True, "试了 n=0/1/10/100/10000")

    vals = [StatsProvider(records=mk(n, 10)).density("agent-B")
            for n in (1, 5, 20, 100, 1000)]
    check("density 随调用次数单调不减",
          all(a <= b + 1e-12 for a, b in zip(vals, vals[1:])),
          " < ".join(f"{v:.3f}" for v in vals))

    sp0 = StatsProvider(records=[])
    p0 = sp0.profile_for("agent-B")
    check("零记录时三维全为 0",
          (p0.depth, p0.density, p0.span) == (0.0, 0.0, 0.0))

    check("saturate 在半饱和点恰为 0.5", saturate(50, 50) == 0.5)
    for bad in [(1, 0), (-1, 10)]:
        try:
            saturate(*bad)
            check(f"saturate{bad} → 应被拒", False, "（竟然通过了！）")
        except ValueError:
            check(f"saturate{bad} → 被拒", True)


def t3_explainable() -> None:
    print("\n  ③ 可解释 —— 每个值都要能手算核对")
    sp = StatsProvider(records=mk(40, 6, ("coding", "writing")))
    ex = sp.explain("agent-B")
    check("explain 给出原始计数",
          ex["calls"] == 40 and ex["n_callers"] == 6 and ex["n_skills"] == 2,
          f"calls={ex['calls']} callers={ex['n_callers']} skills={ex['n_skills']}")

    # 解释串里必须出现 round(..., 6)，否则它与维度值差最后几位（初版就差了 4e-7）
    check("解释串写出了 round(..., 6)", "round(" in ex["depth"] and ", 6)" in ex["depth"],
          "→ 维度值经过 round 以保证 core_hash 稳定，解释串必须一并写出")

    # 程序化核对：check 里的未 round 值经 round 后必须逐位等于维度值
    for d in ("depth", "density", "span"):
        got = getattr(sp, d)("agent-B")
        want = round(ex["check"][d], 6)
        check(f"{d}: round(解释里的算术) 逐位等于维度值", got == want,
              f"{got} vs {want}")

    # 手算 depth：平均栈深 2，k=4 → 2/6
    check("depth 能手算（round(2/(2+4),6)=1/3）",
          sp.depth("agent-B") == round(1 / 3, 6), f"{sp.depth('agent-B')}")

    # 手算 span：round((6/11 + 2/7)/2, 6)
    manual = round((saturate(6, 5) + saturate(2, 5)) / 2, 6)
    check("span 能手算（含 round）", sp.span("agent-B") == manual,
          f"{sp.span('agent-B')} vs 手算 {manual}")

    # 手算 density：round((40/90 + 0.9 + (1-400/1400))/3, 6)
    manual_d = round((saturate(40, 50) + 0.9 + (1 - saturate(400, 1000))) / 3, 6)
    check("density 能手算（含 round）", sp.density("agent-B") == manual_d,
          f"{sp.density('agent-B')} vs 手算 {manual_d}")


def t4_semantics() -> None:
    print("\n  ④ 语义 —— 「被依赖」与「被需要」不是同一件事")
    # 一个 caller 调用 100 次
    dep = StatsProvider(records=mk(100, 1))
    # 20 个 caller 各调用 5 次
    need = StatsProvider(records=mk(100, 20))
    d_dep, d_need = dep.span("agent-B"), need.span("agent-B")
    print(f"     1 个 caller 调用 100 次: span={d_dep:.4f}  density={dep.density('agent-B'):.4f}")
    print(f"     20 个 caller 各 5 次:   span={d_need:.4f}  density={need.density('agent-B'):.4f}")
    check("同样 100 次调用，caller 多的 span 更高", d_need > d_dep,
          f"{d_need:.4f} > {d_dep:.4f}")
    check("density 只看次数，两者接近（延迟与成功率相同）",
          abs(dep.density("agent-B") - need.density("agent-B")) < 1e-9)

    # 技能条件化
    sp = StatsProvider(records=mk(60, 8, ("coding", "writing")))
    all_p = sp.profile_for("agent-B")
    cod_p = sp.profile_for("agent-B", skill="coding")
    check("技能条件化后的 call_count 更小", cod_p.call_count < all_p.call_count,
          f"{cod_p.call_count} < {all_p.call_count}")
    check("depth 是位置属性，不随技能变", cod_p.depth == all_p.depth,
          f"{cod_p.depth} == {all_p.depth}")
    check("depth 与 skills 的差在 density/span 上",
          cod_p.density != all_p.density or cod_p.span != all_p.span)


def t5_chain_coupling() -> None:
    print("\n  ⑤ 与链耦合 —— 改历史必须改变剖面（这是应有的）")
    recs = mk(30, 5)
    h_before = core_hash(StatsProvider(records=recs).profile_for("agent-B").to_dict())

    tampered = list(recs)
    tampered[0] = CallRecord(**{**recs[0].to_dict(), "latency_ms": 99999})
    h_after = core_hash(StatsProvider(records=tampered).profile_for("agent-B").to_dict())

    check("改一条调用的耗时 → core_hash 变", h_before != h_after,
          f"{h_before[:12]}… → {h_after[:12]}…")
    check("所以「链上历史」与「剖面」是耦合的",
          True, "→ 这正是核剖面可以只锚事实的原因")

    # 顺序无关：同一批记录换个顺序，剖面不变
    import random
    shuffled = list(recs)
    random.Random(7).shuffle(shuffled)
    h_shuf = core_hash(StatsProvider(records=shuffled).profile_for("agent-B").to_dict())
    check("记录顺序不影响剖面（统计量该有的性质）", h_shuf == h_before,
          "→ 求和与计数都可交换")


def main() -> int:
    print("=" * 72)
    print("  核剖面引擎自检 —— 它是统计量，因此没有确定性关口")
    print("=" * 72)
    t1_no_llm()
    t2_bounds()
    t3_explainable()
    t4_semantics()
    t5_chain_coupling()
    print()
    print("=" * 72)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 72)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
