"""Does co-occurrence GRANULARITY decide whether the delete test can work at all?

THE QUESTION
------------
Three core detectors were run on one graph (31 nodes / 339 edges / mean degree 21.9). The
metric backbone sparsified 38% of edges and STILL produced zero articulation points, and the
top k-core held 61% of all nodes. Both facts point at the same cause: the graph is dense
because co-occurrence was counted per MESSAGE, and one message mentioning ten concepts creates
a 45-edge clique.

So sparsifying after the fact (backbone) may be treating the symptom. This builds the same
system at SENTENCE granularity and asks the two questions that matter:
  1. do articulation points appear — i.e. can delete-and-collapse work again?
  2. do the rich-core members stay roughly the same — i.e. is the answer robust to granularity?

A method that changes its answer when you change the co-occurrence unit is not measuring the
system; it is measuring the unit. That is the whole lesson of D1, and it applies here too.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).parent))
import concepts as C          # noqa: E402
import system as S            # noqa: E402

CORPUS = Path(r"D:\myproject\PACSP-collection\03-PACSP-情绪树\conversation.md")
SPLIT = re.compile(r"[。；;！!？?\n]+")


def build_graph(unit: str):
    """unit: 'message' | 'sentence'"""
    cat = C.build_catalog()
    msgs = S.load_messages(CORPUS)
    G = nx.Graph()
    for _mid, _role, body in msgs:
        chunks = [body] if unit == "message" else SPLIT.split(body)
        for ch in chunks:
            cs = C.occurrences(ch, cat)
            if len(cs) < 2:
                # a chunk with 0 or 1 concept contributes no edge; still a node if len==1
                for c in cs:
                    G.add_node(c)
                continue
            cl = sorted(cs)
            for i in range(len(cl)):
                for j in range(i + 1, len(cl)):
                    a, b = cl[i], cl[j]
                    G.add_edge(a, b, weight=G.get_edge_data(a, b, {}).get("weight", 0) + 1)
    return G


def report(tag: str, G: nx.Graph):
    n, m = G.number_of_nodes(), G.number_of_edges()
    deg = [d for _, d in G.degree()]
    arts = sorted(nx.articulation_points(G))
    comps = list(nx.connected_components(G))
    cn = nx.core_number(G)
    mx = max(cn.values()) if cn else 0
    top = [x for x, k in cn.items() if k == mx]

    # rich core
    degd = dict(G.degree())
    order = sorted(degd, key=lambda x: -degd[x])
    best_r, best_cnt = 0, -1
    for r in range(len(order)):
        top_set = set(order[:r + 1])
        cnt = sum(1 for u, v in G.edges() if (u in top_set) != (v in top_set))
        if cnt > best_cnt:
            best_cnt, best_r = cnt, r
    rc = set(order[:best_r + 1])

    print(f"═══ {tag} ═══")
    print(f"  节点 {n}  边 {m}  平均度 {sum(deg)/len(deg):.1f}  最大度 {max(deg)}  最小度 {min(deg)}")
    print(f"  连通分量 {len(comps)}   最大分量 {max(len(c) for c in comps)}")
    print(f"  割点（删后断裂）: {len(arts)} 个" + (f": {', '.join(arts)}" if arts else "  ← 删除-塌陷判据仍不可用"))
    print(f"  最高 k-core: k={mx}, 含 {len(top)} 个节点 ({len(top)/n:.0%})")
    print(f"  富核: {len(rc)} 个 ({len(rc)/n:.0%})")
    print(f"    {', '.join(sorted(rc))}")
    print()
    return {"nodes": n, "edges": m, "arts": set(arts), "kmax": mx, "ktop": set(top), "rich": rc}


def main() -> int:
    print("═" * 64)
    print("共现粒度对照：消息级 vs 句子级")
    print("═" * 64)
    a = report("① 消息级（当前）", build_graph("message"))
    b = report("② 句子级", build_graph("sentence"))

    print("═" * 64)
    print("对照结论")
    print("═" * 64)
    print(f"  边数      : {a['edges']} → {b['edges']}   （{(b['edges']-a['edges'])/a['edges']:+.0%}）")
    print(f"  平均度    : {sum(dict(build_graph('message').degree()).values())/a['nodes']:.1f} → "
          f"{sum(dict(build_graph('sentence').degree()).values())/b['nodes']:.1f}")
    print(f"  割点      : {len(a['arts'])} → {len(b['arts'])}")
    print(f"  最高k-core: {len(a['ktop'])} ({len(a['ktop'])/a['nodes']:.0%}) → {len(b['ktop'])} ({len(b['ktop'])/b['nodes']:.0%})")
    print()
    inter = a["rich"] & b["rich"]
    print(f"  富核交集  : {len(inter)} / ({len(a['rich'])} , {len(b['rich'])})")
    print(f"    两级都判为核心的: {', '.join(sorted(inter))}")
    only_a = a["rich"] - b["rich"]
    only_b = b["rich"] - a["rich"]
    if only_a:
        print(f"    只在消息级为核心: {', '.join(sorted(only_a))}")
    if only_b:
        print(f"    只在句子级为核心: {', '.join(sorted(only_b))}")
    print()
    stable = len(inter) / max(len(a["rich"] | b["rich"]), 1)
    print(f"  富核的 Jaccard 稳定性: {stable:.2f}")
    print(f"  → {'换粒度后结论基本不变，富核稳健' if stable >= 0.6 else '换粒度后结论显著改变——富核测的是粒度，不是系统'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
