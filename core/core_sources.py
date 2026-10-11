"""core_sources.json：把「核」拆成两层四源，分别计算，不取交集。

分层（作者 2026-10-09 精确化）
------------------------------
节点层（概念）：
    结构角色核   —— 跨粒度（节/段/句）的割点或富核稳定
    依赖前提核   —— 被 depends_on 跨口径稳定依赖
边层（关系）：
    直接证据     —— 在 ≥1 个句子单元内共现，论文明确表达
    推理证据     —— 0 个句子单元内共现，跨句推断的产物

为什么不能混
------------
1. 两类节点核的来源独立，取交集会把「多来源的核错误排除」。实测：在论文系统上这两个集合
   的交集为空——`metric backbone` 是结构角色核但不是依赖前提核，`asymptotic scaling` 反之。
   若取交集，两者都会被排除，而它们各自都是真的。
2. 边层的两层可信度不是「两种关系」，是同一批关系的两种证据强度。混报 76 条关系等于宣称
   论文明确说了它没说的话——实测 44/76 是推理产物。
3. 节点核与边可信度在图的**不同层**：一个问「哪些概念是核」，一个问「哪条边有直接证据」。

全部在【同一系统】（论文 arXiv:2406.03852 的概念图）上计算，避免跨系统拼凑。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).parent))

SECTIONS = Path(r"D:\knowledge-graph\sources\papers\2406.03852.sections.json")
GRAPH = Path(r"D:\knowledge-graph\ingested\paper-concept-graph.json")
TYPE_SWEEP = Path(r"D:\knowledge-graph\ingested\type-sweep.json")
OUT = Path(r"D:\knowledge-graph\ingested\core_sources.json")

LEVELS = [
    ("节", None),
    ("段落", re.compile(r"\n\s*\n")),
    ("句子", re.compile(r"(?<=[.!?])\s+")),
]

DEP_TYPES = {"depends_on"}
BROAD_TYPES = {"depends_on", "uses", "generalizes", "applies_to"}


# ---------------------------------------------------------------- roles
def role_cut(G):
    if G.number_of_nodes() == 0:
        return set()
    return set(nx.articulation_points(G))


def role_rich(G):
    """Ma & Mondragon 原文：最大化逐节点 k+_r（向更高排名节点的连边数）。"""
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


def main() -> int:
    secs = json.loads(SECTIONS.read_text(encoding="utf-8"))
    full = "\n\n".join(v["text"] for v in secs.values())
    g = json.loads(GRAPH.read_text(encoding="utf-8"))
    concepts = [c["name"] for c in g["concepts"]]
    rels = g["relations"]

    print("═" * 70)
    print("核的来源分层（同一系统：arXiv:2406.03852 概念图）")
    print("═" * 70)
    print(f"  {len(concepts)} 概念 / {len(rels)} 关系")
    print()

    # ---------------- 一、节点核：结构角色 ----------------
    print("── 一、节点核 · 来源 A：结构角色 ──")
    # 节级必须是 6 个节，不是「把全文当一个单元」。第一版把 secs 拼成 full 后再对 full 做
    # 「节」级切分（rx=None -> [full]），得到 1 个单元 —— 那等于完全图，于是割点 0、富核 47，
    # 整个结构角色核被这个假数据污染。节级单元直接取 secs 的各节。
    units_by_level = {
        "节": [v["text"] for v in secs.values()],
        "段落": None,
        "句子": None,
    }
    occ_by_level = {}
    for lname, rx in LEVELS:
        us = units_by_level[lname] if lname == "节" else (
            [full] if rx is None else rx.split(full))
        occ_by_level[lname] = [{c for c in concepts if c.lower() in u.lower()} for u in us]
        print(f"    {lname:<4} 单元 {len(us):>4}   含≥2概念 {sum(1 for s in occ_by_level[lname] if len(s)>=2):>4}")

    struct = {}
    for lname, _ in LEVELS:
        occ = occ_by_level[lname]
        G = nx.Graph()
        G.add_nodes_from(concepts)
        for s in occ:
            sl = sorted(s)
            for i in range(len(sl)):
                for j in range(i + 1, len(sl)):
                    G.add_edge(sl[i], sl[j])
        for rname, fn in ROLES.items():
            struct.setdefault(rname, {})[lname] = fn(G)

    struct_core = {}
    for rname in ROLES:
        sets = [struct[rname][ln] for ln, _ in LEVELS]
        inter = set.intersection(*sets) if sets else set()
        union = set.union(*sets) if sets else set()
        struct_core[rname] = {"per_level": {ln: len(struct[rname][ln]) for ln, _ in LEVELS},
                              "intersection": sorted(inter),
                              "jaccard": round(len(inter) / len(union), 4) if union else 0.0}
        print(f"    {rname:<4} 各级 {[len(struct[rname][ln]) for ln, _ in LEVELS]}"
              f"   跨粒度交集 {len(inter)}   Jaccard {struct_core[rname]['jaccard']:.3f}")
        if inter:
            print(f"           {', '.join(sorted(inter))}")
    struct_union = sorted(set().union(*(set(v["intersection"]) for v in struct_core.values())))
    print()
    print(f"    结构角色核（两 Role 的并集，不取交）: {len(struct_union)} 个")
    for c in struct_union:
        src = [r for r in ROLES if c in struct_core[r]["intersection"]]
        print(f"      {c:<32} 来自 {', '.join(src)}")
    print()

    # ---------------- 二、节点核：依赖前提 ----------------
    print("── 二、节点核 · 来源 B：依赖前提 ──")
    dep_strict = {c: set() for c in concepts}
    for r in rels:
        if r["type"] in DEP_TYPES:
            dep_strict.setdefault(r["from"], set()).add(r["to"])
    # 被依赖数
    cited = {}
    for u, refs in dep_strict.items():
        for v in refs:
            cited[v] = cited.get(v, 0) + 1
    # 与 type-sweep 的 8/8 结果对照
    ts = json.loads(TYPE_SWEEP.read_text(encoding="utf-8")) if TYPE_SWEEP.exists() else {}
    robust = ts.get("robust_cores", [])
    print(f"    依赖边（仅 depends_on）: {sum(len(v) for v in dep_strict.values())}")
    print(f"    被 depends_on 引用过的概念: {len(cited)}")
    print(f"    跨 8 个关系类型阶段全稳定（type_sweep 的 8/8）: {len(robust)}")
    for c in robust:
        print(f"      {c:<32} 被 {cited.get(c, 0)} 个概念依赖")
    dep_union = sorted(set(robust) | {c for c, n in cited.items() if n >= 2})
    print()
    print(f"    依赖前提核（8/8 ∪ 被≥2个依赖）: {len(dep_union)} 个")
    print()

    # ---------------- 交集检验 ----------------
    print("── 两类节点核的交集 ──")
    inter = set(struct_union) & set(dep_union)
    print(f"    结构角色核 {len(struct_union)} ∩ 依赖前提核 {len(dep_union)} = {len(inter)}")
    if inter:
        print(f"      {', '.join(sorted(inter))}")
    else:
        print("      （空）—— 两类来源在系统上完全不重叠")
        print(f"      只在结构角色: {', '.join(sorted(set(struct_union) - set(dep_union)))}")
        print(f"      只在依赖前提: {', '.join(sorted(set(dep_union) - set(struct_union)))}")
    print()
    print("    → 若取交集会得到空集，而那与「没有核」是两回事。这正是不能取交集的理由。")
    print()

    # ---------------- 三、边层：关系可信度 ----------------
    print("── 三、关系可信度（边层，两层证据强度）──")
    occ_sent = occ_by_level["句子"]
    direct, inferred = [], []
    for r in rels:
        a, b = r["from"], r["to"]
        n = sum(1 for s in occ_sent if a in s and b in s)
        rec = {"from": a, "to": b, "type": r["type"], "sentence_cooccurrence": n}
        (direct if n > 0 else inferred).append(rec)
    print(f"    直接证据（句子级共现 ≥1）: {len(direct)}/{len(rels)}  ({len(direct)/len(rels):.0%})")
    print(f"    推理证据（句子级共现 = 0）: {len(inferred)}/{len(rels)}  ({len(inferred)/len(rels):.0%})")
    print()
    print("    直接证据里共现最多的:")
    for r in sorted(direct, key=lambda x: -x["sentence_cooccurrence"])[:8]:
        print(f"      {r['sentence_cooccurrence']:>3} 句  {r['from'][:26]:<28} --{r['type']}--> {r['to'][:24]}")
    print()
    print("    推理证据（按类型分布）:")
    from collections import Counter
    ct = Counter(r["type"] for r in inferred)
    for t, k in ct.most_common():
        print(f"      {t:<14} {k}")
    print()

    # ---------------- 输出 ----------------
    OUT.write_text(json.dumps({
        "system": "arXiv:2406.03852 Why the Metric Backbone Preserves Community Structure",
        "structure": "两层四源；节点层与边层不混合，同类内不取跨 Role 交集",
        "node_cores": {
            "structural_role": {
                "definition": "跨粒度（节/段/句）的割点或富核稳定的概念",
                "applicability": "图连通、存在桥",
                "by_role": struct_core,
                "union": struct_union,
            },
            "dependency_premise": {
                "definition": "被 depends_on 关系跨口径稳定依赖的概念",
                "applicability": "依赖图存在、关系类型明确",
                "robust_8of8": robust,
                "cited_by_ge2": sorted(c for c, n in cited.items() if n >= 2),
                "union": dep_union,
            },
            "intersection": sorted(inter),
            "note": "两类来源独立，交集为空不表示「没有核」；取交集会排除多来源的核。",
        },
        "edge_credibility": {
            "direct_evidence": {"definition": "≥1 个句子单元内共现", "count": len(direct), "edges": direct},
            "inferred": {"definition": "0 个句子单元内共现，跨句推断", "count": len(inferred), "edges": inferred},
            "note": "两层是同一批关系的证据强度，不是两种关系；混报等于宣称论文说了它没说的话。",
        },
        "dimension_scan_status": {
            "granularity": "已扫（4 级，情绪树；3 级，论文）",
            "relation_type": "已扫（8 个累积阶段，核在 14-15 间振荡，非单调）",
            "role": "不是维度 —— 是三种判据，适用条件不同，不取交集",
            "evidence_granularity": "已扫（支持度法，58% 关系为推理产物）",
            "convergence": "未定 —— 已扫维度上非单调",
        },
    }, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(f"  ✓ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
