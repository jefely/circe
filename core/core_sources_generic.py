"""通用版「两层四源」分析：对任意论文的概念图跑，并报告各类是否适用。

为什么必须通用
--------------
若为对照论文单独改脚本，对照就变成「改到出现预期结果为止」——那引入的新维度比它测量的还多。
所以这里只接受两个参数（概念图 JSON、章节 JSON），对 2406.03852 与 2609.31013 用同一份代码。

「适用性」与「结果为空」的区分（这是本脚本的重点）
--------------------------------------------------
实证论文里 `depends_on` 可能极少甚至为零。此时「依赖前提核」应当被报告为
    status = NOT_APPLICABLE
而不是
    core   = []          （那会被读成「有这一类，但它是空的」）

区分办法：看依赖类边占全部关系的比例，以及是否存在任何被 ≥2 个概念依赖的概念。
两个阈值都写进输出，可被质疑。

用法:
    python NSP/core_sources_generic.py <concept-graph.json> <sections.json> <out.json>
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import networkx as nx

LEVELS = [("节", None),
          ("段落", re.compile(r"\n\s*\n")),
          # 句子切分必须同时支持中英文句末标点。
          # 第一版只有 (?<=[.!?])\s+（为英文论文写的），遇到中文语料（情绪树）时
          # 中文句号「。」不在字符类里，句子级切不出来 —— 粒度退化：
          #     句子=3796 不 > 上一级 段落=14971
          # 由 check_granularity_monotonicity 抓住。这是它第二次抓到我没预料到的同类问题。
          ("句子", re.compile(r"(?<=[.!?。；！？])[\s]+|(?<=[。；！？])"))]
DEP_TYPES = {"depends_on"}
BROAD_TYPES = {"depends_on", "uses", "generalizes", "applies_to"}

# 适用性阈值（可质疑，故写进输出）
MIN_DEP_EDGE_RATIO = 0.05      # 依赖类边占全部关系的最低比例
MIN_CITED_BY_2 = 2             # 至少要有几个被 ≥2 个概念依赖的概念


def role_cut(G):
    if G.number_of_nodes() == 0:
        return set()
    return set(nx.articulation_points(G))


def role_rich(G):
    if G.number_of_nodes() == 0:
        return set()
    deg = dict(G.degree())
    order = sorted(deg, key=lambda n: -deg[n])
    rank = {n: i for i, n in enumerate(order)}
    kp = {n: sum(1 for nb in G.neighbors(n) if rank[nb] < rank[n]) for n in order}
    br, bv = 0, -1
    for r, n in enumerate(order):
        if kp[n] > bv:
            bv, br = kp[n], r
    while br + 1 < len(order) and kp[order[br + 1]] == bv:
        br += 1
    return set(order[:br + 1])


ROLES = {"割点": role_cut, "富核": role_rich}


class GranularityDegeneration(AssertionError):
    """更细的粒度必须产生更多单元。违反即为退化。"""


def check_granularity_monotonicity(counts: dict, labels: list) -> None:
    """粒度必须单调：节 < 段落 < 句子（< 子句）。

    为什么需要这条检查
    ------------------
    本项目实际发生过一次粒度退化：`strip_tags` 用 `re.sub(r"\\s+", " ")` 把换行压掉，
    于是段落正则 `\\n\\s*\\n` 永远匹配不到，「段落」级静默退化成与「节」级相同的单元数。
    它连续给出了三批看起来完全合理的数字（结构角色核报 12，真值 8），**没有任何异常信号**。

    唯一能抓住它的是**与结果无关的结构约束**：更细的粒度必须切出更多单元。
    这条检查不需要知道正确值，也不需要看结果是否合理——退化的结果恰恰是合理的。

    counts 的键是任意标签，labels 给出从粗到细的顺序。缺项跳过（允许只扫部分粒度）。
    """
    prev_label, prev = None, 0
    for g in labels:
        if g not in counts:
            continue
        n = counts[g]
        if n <= prev:
            raise GranularityDegeneration(
                f"粒度退化: {g}={n} 不 > 上一级 {prev_label}={prev}"
                f"  （更细的粒度必须产生更多单元；相等即说明切分没有真正生效）")
        prev_label, prev = g, n


def selftest_granularity_check() -> bool:
    """证明这条检查会失败——一个不会失败的门禁只是装饰。"""
    print("  ── 粒度单调性自检 ──")
    ok = True
    try:
        check_granularity_monotonicity({"节": 6, "段落": 6, "句子": 267}, ["节", "段落", "句子"])
        print("    ✗ 退化样本（段落==节）未被抓住")
        ok = False
    except GranularityDegeneration as e:
        print(f"    ✓ 退化样本被抓住: {e}")
    try:
        check_granularity_monotonicity({"节": 6, "段落": 153, "句子": 267}, ["节", "段落", "句子"])
        print("    ✓ 健康样本（6/153/267）通过")
    except GranularityDegeneration as e:
        print(f"    ✗ 健康样本被误杀: {e}")
        ok = False
    try:
        check_granularity_monotonicity({"节": 6, "段落": 153, "句子": 100}, ["节", "段落", "句子"])
        print("    ✗ 倒挂样本（句子<段落）未被抓住")
        ok = False
    except GranularityDegeneration as e:
        print(f"    ✓ 倒挂样本被抓住: {e}")
    print(f"  {'自检通过——这条检查会失败' if ok else '自检失败——检查无效，粒度结果不予采信'}")
    return ok


def check_system_consistency(concepts: list, secs: dict) -> None:
    """概念图与章节文件必须来自【同一个系统】。

    为什么需要这条检查
    ------------------
    实际发生过一次：把情绪树语料的概念图配上了论文的 sections 文件去跑。
    情绪树的概念（"情绪树"、"意义技术"）在英文论文里根本不出现，于是每个粒度单元的概念集
    为空、图无边。**脚本没有报错**，而是给出：

        富核各级 [31, 31, 31]，Jaccard 1.000    （无边图的"核"就是全部节点）
        直接证据 0/248 · 推理证据 248/248        （共现恒为 0）

    两个数字都"整齐得可疑"，但没有任何机制拦住它们 —— 与粒度退化同一类失败。
    判据：概念在源文本里的总出现次数必须足够高。若绝大多数概念一次都不出现，
    就是配错了文件。
    """
    full = "\n\n".join(v["text"] for v in secs.values()).lower()
    hit = sum(1 for c in concepts if c.lower() in full)
    ratio = hit / max(1, len(concepts))
    print(f"  ── 系统一致性检查 ──")
    print(f"    概念在源文本中出现: {hit}/{len(concepts)} ({ratio:.0%})")
    if ratio < 0.5:
        raise AssertionError(
            f"概念图与章节文件不属于同一系统: 只有 {hit}/{len(concepts)} ({ratio:.0%}) "
            f"的概念在源文本里出现过。配错文件会给出看似合理但无意义的数字。")
    print(f"    ✓ 一致（阈值 50%）")


def cross_reading_robust(rels: list[dict]) -> tuple[list[str], list[dict]]:
    """跨口径扫描：逐步加入关系类型，找出每个累积阶段都是核的概念。

    为什么这步不能省：通用版第一版只用了「被 ≥2 个概念依赖」，结果在 2406.03852 上给出
    6 个依赖前提核，而专用版给出 7 个——差的 `inter-community edge` 只被 1 个概念依赖，
    但它在跨口径扫描里是 8/8。少这一步，对照实验就不是「同一套方法」，而通用版会系统性地
    低估依赖前提核。

    与 type_sweep.py 同一逻辑：累积加入类型，每个阶段做一次剥离，取全阶段都存活的概念。
    """
    types = Counter(r["type"] for r in rels)
    order = [t for t, _ in sorted(types.items(), key=lambda x: -x[1])]

    def peel(active: set[str]) -> set[str]:
        deps: dict[str, set[str]] = {}
        for r in rels:
            if r["type"] in active:
                deps.setdefault(r["from"], set()).add(r["to"])
        for r in rels:
            deps.setdefault(r["to"], set())
            deps.setdefault(r["from"], set())
        alive = set(deps)
        while True:
            deletable = [x for x in sorted(alive)
                         if not any(deps.get(u) and not (deps[u] & (alive - {x}))
                                    for u in deps if u != x)]
            if not deletable:
                break
            alive -= set(deletable)
            if not alive:
                break
        return alive

    stages, cum = [], set()
    for t in order:
        cum = cum | {t}
        c = peel(cum)
        stages.append({"types": sorted(cum), "core": sorted(c)})
    robust = sorted(set.intersection(*[set(s["core"]) for s in stages])) if stages else []
    return robust, stages


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    gpath, spath, opath = (Path(a) for a in sys.argv[1:4])
    g = json.loads(gpath.read_text(encoding="utf-8"))
    secs = json.loads(spath.read_text(encoding="utf-8"))
    concepts = [c["name"] for c in g["concepts"]]
    rels = g["relations"]
    kinds = {c["name"]: c.get("kinds", []) for c in g["concepts"]}

    print("═" * 70)
    print(f"两层四源（通用）：{gpath.name}")
    print("═" * 70)
    print(f"  {len(concepts)} 概念 / {len(rels)} 关系")
    print()
    if not selftest_granularity_check():
        return 1
    print()
    check_system_consistency(concepts, secs)
    print()

    # ---- 适用性检查：先做，因为它决定后续哪一节有意义 ----
    type_cnt = Counter(r["type"] for r in rels)
    dep_edges = sum(v for t, v in type_cnt.items() if t in DEP_TYPES)
    broad_edges = sum(v for t, v in type_cnt.items() if t in BROAD_TYPES)
    cited = Counter()
    for r in rels:
        if r["type"] in DEP_TYPES:
            cited[r["to"]] += 1
    n_cited_ge2 = sum(1 for c, n in cited.items() if n >= 2)

    print("── 适用性检查 ──")
    print(f"  关系类型分布: " + " · ".join(f"{t} {k}" for t, k in type_cnt.most_common()))
    print(f"  依赖类边（depends_on）: {dep_edges} / {len(rels)} = {dep_edges/max(1,len(rels)):.1%}"
          f"   （阈值 {MIN_DEP_EDGE_RATIO:.0%}）")
    print(f"  被 ≥2 个概念依赖的: {n_cited_ge2}   （阈值 {MIN_CITED_BY_2}）")
    dep_applicable = (dep_edges / max(1, len(rels)) >= MIN_DEP_EDGE_RATIO) and (n_cited_ge2 >= MIN_CITED_BY_2)
    print(f"  → 依赖前提核: {'APPLICABLE' if dep_applicable else 'NOT_APPLICABLE'}"
          + ("" if dep_applicable else "   （依赖结构不足，不是「算出来为空」）"))
    print()

    # ---- 节点核 A：结构角色 ----
    full = "\n\n".join(v["text"] for v in secs.values())
    units_by_level = {"节": [v["text"] for v in secs.values()]}
    occ_by = {}
    level_labels = [ln for ln, _ in LEVELS]
    # 先切分、先查单调性，再算任何 Role —— 退化必须在产生数字【之前】被拦住。
    # 放在后面查也可以发现问题，但那时已经打印了一批看起来合理的数字。
    raw_units = {}
    for lname, rx in LEVELS:
        raw_units[lname] = units_by_level.get(lname) or ([full] if rx is None else rx.split(full))
    check_granularity_monotonicity({k: len(v) for k, v in raw_units.items()}, level_labels)
    for lname, _ in LEVELS:
        occ_by[lname] = [{c for c in concepts if c.lower() in u.lower()}
                         for u in raw_units[lname]]

    print("── 节点核 A：结构角色 ──")
    struct = {}
    for lname, _ in LEVELS:
        G = nx.Graph()
        G.add_nodes_from(concepts)
        for s in occ_by[lname]:
            sl = sorted(s)
            for i in range(len(sl)):
                for j in range(i + 1, len(sl)):
                    G.add_edge(sl[i], sl[j])
        for rn, fn in ROLES.items():
            struct.setdefault(rn, {})[lname] = fn(G)
    struct_core = {}
    for rn in ROLES:
        sets = [struct[rn][ln] for ln, _ in LEVELS]
        it = set.intersection(*sets) if sets else set()
        un = set.union(*sets) if sets else set()
        struct_core[rn] = {"per_level": [len(struct[rn][ln]) for ln, _ in LEVELS],
                           "intersection": sorted(it),
                           "jaccard": round(len(it) / len(un), 4) if un else 0.0}
        st = "APPLICABLE" if (max(struct_core[rn]["per_level"]) > 0) else "NOT_APPLICABLE"
        print(f"  {rn:<4} 各级 {struct_core[rn]['per_level']}  交集 {len(it)}  "
              f"Jaccard {struct_core[rn]['jaccard']:.3f}   [{st}]")
        if it:
            print(f"       {', '.join(sorted(it))}")
    struct_union = sorted(set().union(*(set(v["intersection"]) for v in struct_core.values())))
    print(f"  结构角色核（并集）: {len(struct_union)} 个")
    print()

    # ---- 节点核 B：依赖前提 ----
    print("── 节点核 B1：严格依赖核（仅 depends_on 入边）──")
    strict_core = sorted(c for c, n in cited.items() if n >= 2)
    if dep_applicable or strict_core:
        print(f"  被 ≥2 个概念以 depends_on 依赖: {len(strict_core)} 个")
        for c in strict_core:
            print(f"    {c:<36} 被 {cited[c]} 个依赖")
        if not strict_core:
            print("    （无）—— 没有概念被 ≥2 个概念以 depends_on 依赖")
    else:
        print("  NOT_APPLICABLE —— 该系统的依赖结构不足，此类不产生结论。")
    print()

    print("── 节点核 B2：跨口径稳健核（累积类型集下稳定）──")
    robust, stages = cross_reading_robust(rels)
    n_types = len(set(r["type"] for r in rels))
    # 单一类型时"跨口径"没有意义：累积扫描只有 1 个阶段，它不是"跨"任何东西。
    # 情绪树（只有 co_occurs）就是这样 —— 第一版照算并给出 2 个核，报了个假数字。
    if n_types < 2:
        print(f"  NOT_APPLICABLE —— 只有 {n_types} 种关系类型，「跨口径」无从谈起（至少需要 2 种）")
        robust, stages = [], []
    else:
        print(f"  {len(stages)} 个累积阶段，全阶段存活 {len(robust)} 个")
        print(f"    阶段核大小: {[len(s['core']) for s in stages]}")
        order_txt = " → ".join(t for t, _ in Counter(r["type"] for r in rels).most_common())
        print(f"    累积类型顺序: {order_txt}")
        for c in robust:
            # 跨口径稳健核【不要求】有 depends_on 入边，这正是它与严格依赖核的区别
            d = cited.get(c, 0)
            tag = "" if d else "   （无 depends_on 入边——靠 reports/correlates_with 等类型存活）"
            print(f"    {c:<36} depends_on 入边 {d}{tag}")
    print()
    print("  ⚠ 两者是两个独立问题，不取交集、不合并命名：")
    print("      严格依赖核   回答「什么是论文的地基」——前提性的、不可删的")
    print("      跨口径稳健核 回答「什么是论文的重心」——无论用哪种关系读，它都在")
    print(f"    只在严格依赖核: {', '.join(sorted(set(strict_core) - set(robust))) or '（无）'}")
    print(f"    只在跨口径稳健: {', '.join(sorted(set(robust) - set(strict_core))) or '（无）'}")
    print(f"    两者皆在: {', '.join(sorted(set(strict_core) & set(robust))) or '（无）'}")
    print()

    # 交集（度量单位）用严格依赖核，因为它问的是"地基与骨架共用的量"
    dep_union = strict_core
    inter = sorted(set(struct_union) & set(dep_union))
    print(f"── 交集（度量单位）── {len(inter)} 个"
          + (f": {', '.join(inter)}" if inter else "（空或不可算）"))
    if inter:
        kk = Counter()
        for c in inter:
            for k in kinds.get(c, ["?"]):
                kk[k] += 1
        print(f"    类型分布: " + " · ".join(f"{k} {v}" for k, v in kk.most_common()))
    print()

    # ---- 边层：证据可信度 ----
    print("── 边层：证据可信度 ──")
    sent = occ_by["句子"]
    direct, inferred = [], []
    for r in rels:
        a, b = r["from"], r["to"]
        n = sum(1 for s in sent if a in s and b in s)
        (direct if n > 0 else inferred).append({**r, "sentence_cooccurrence": n})
    print(f"  直接证据 {len(direct)}/{len(rels)} ({len(direct)/max(1,len(rels)):.0%})"
          f"   推理证据 {len(inferred)}/{len(rels)} ({len(inferred)/max(1,len(rels)):.0%})")

    # NULL finding 层：与边层并列，但不进图
    # 边层两层在【共现系统】上会误用：那里的边本来就是共现，"句子级共现=0" 只说明
    # 两个概念同处一条消息但不同句，**不是跨句推断**。所以共现系统必须单独说明。
    only_cooccurrence = set(r["type"] for r in rels) == {"co_occurs"}
    if only_cooccurrence:
        print("  ⚠ 本系统是【纯共现】：边层两层【不适用】——")
        print(f"     {len(inferred)} 条「句子级共现=0」的边不是跨句推断，而是"
              f"「同处一条消息但不同句」。")
        print("     共现系统只有一层证据：共现本身。上面的 直接/推理 切分不应被引用。")
        print()
    # 第一版靠文件名猜（f"null-findings-{gpath.name.split('-')[0]}.json"），
    # 对 2609.31013-concept-graph.json 会去找 null-findings-2609.31013.json，
    # 而实际文件是 null-findings-2609.json —— 于是静默地报 0 条。
    # 猜文件名是个会静默失败的接口，改成显式传参（第 4 个参数，可选）。
    nulls, null_src = [], None
    if len(sys.argv) >= 5:
        cand = Path(sys.argv[4])
        if cand.exists():
            nulls = json.loads(cand.read_text(encoding="utf-8")).get("null_findings", [])
            null_src = cand.name
        else:
            print(f"  [!] 指定的 null-findings 文件不存在: {cand}")
    if nulls:
        print(f"  NULL finding（显式声明「无关联」，{len(nulls)} 条）← {null_src}")
        for n in nulls:
            print(f"    {n['subject'][:30]:<32} ⊥ {n['object'][:30]:<32} 「{n['assertion']}」")
    else:
        print("  NULL finding: 0 条（未指定 null-findings 文件，或该文件为空）")
    print("    为什么独立成层：NULL finding 不是边，它断言两者之间【没有】关系。")
    print("    图编码的是「有关系」，所以它不能进图——它是图结构之外的断言层。")
    print("    对实证论文，这一层可能是核心发现。")
    print()

    # ---- 输出 ----
    Path(opath).write_text(json.dumps({
        "source": str(gpath),
        "counts": {"concepts": len(concepts), "relations": len(rels)},
        "applicability": {
            "relation_type_distribution": dict(type_cnt),
            "dep_edges": dep_edges, "dep_edge_ratio": round(dep_edges / max(1, len(rels)), 4),
            "cited_by_ge2": n_cited_ge2,
            "thresholds": {"min_dep_edge_ratio": MIN_DEP_EDGE_RATIO, "min_cited_by_2": MIN_CITED_BY_2},
            "dependency_premise_core": "APPLICABLE" if dep_applicable else "NOT_APPLICABLE",
        },
        "node_cores": {
            "structural_role": {"by_role": struct_core, "union": struct_union},
            "strict_dependency": {
                "definition": "仅 depends_on 入边，被 ≥2 个概念依赖",
                "union": strict_core,
                "cited_counts": {c: cited[c] for c in strict_core},
            },
            "cross_reading_robust": {
                "definition": "累积类型集（含 reports / correlates_with 等）下全阶段稳定",
                "union": robust,
                "stages": stages,
            },
            "intersection_measurement_unit": inter,
            "note": "严格依赖核与跨口径稳健核是两个独立问题，不取交集、不合并命名：前者问「什么是地基」，后者问「什么是重心」。理论论文上两者高度重合，实证论文上分离——这个分离本身是论文类型的信息。",
        },
        "edge_credibility": {
            "direct_evidence": {"count": len(direct), "edges": direct},
            "inferred": {"count": len(inferred), "edges": inferred},
        },
        "null_findings": {
            "count": len(nulls),
            "definition": "显式声明「无关联」的断言。不是边，不进图——图编码「有关系」。",
            "findings": nulls,
        },
    }, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(f"  ✓ {opath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
