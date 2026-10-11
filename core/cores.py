"""Dense-graph core detection: k-core, metric backbone, rich core — compared on the same graph.

WHY THREE AT ONCE
-----------------
The delete-and-collapse test failed on this graph for a structural reason (339 edges over 31
nodes ≈ degree 22 leaves no cut vertex), not a coding bug. Three alternative definitions of
"core" are implemented here so they can be compared on identical input rather than argued
about in the abstract.

TWO CORRECTIONS TO THE RECEIVED CODE
------------------------------------
1. METRIC BACKBONE NEEDS WEIGHTS. The sketch passed a bare graph to `all_shortest_paths`, which
   treats it as unweighted — and in an unweighted graph EVERY edge lies on some shortest path,
   so the "backbone" is the whole graph and nothing is sparsified. The graph here is weighted
   (co-occurrence counts, 1..15), so distances must be derived from those weights.
2. THE DISTANCE MAPPING IS A CHOICE, NOT A GIVEN. A similarity w has to become a distance d
   before "is this edge metric?" means anything. `d = 1/w` is used — it is the standard choice
   and introduces no free parameter — but it IS a choice, and it is recorded here so nobody has
   to rediscover it. The claim that the metric backbone is "parameter-free" is true only after
   that mapping is fixed.

k-core and the rich core need no such mapping; that is a genuine advantage over the backbone.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).parent))
import concepts as C          # noqa: E402
import system as S            # noqa: E402

CORPUS = Path(r"D:\myproject\PACSP-collection\03-PACSP-情绪树\conversation.md")


def load_graph():
    cat = C.build_catalog()
    msgs = S.load_messages(CORPUS)
    sy = S.build(msgs, cat)
    G = nx.Graph()
    G.add_nodes_from(sy.nodes)
    for (a, b), w in sy.edges.items():
        G.add_edge(a, b, weight=w)
    return G, sy


# ---------------------------------------------------------------- k-core
def kcore(G: nx.Graph):
    cn = nx.core_number(G)
    layers = defaultdict(list)
    for n, k in cn.items():
        layers[k].append(n)
    return cn, layers


# ---------------------------------------------------- metric backbone
def metric_backbone(G: nx.Graph, dist_of=lambda w: 1.0 / w):
    """Keep only edges that are METRIC: no alternative path is shorter.

    An edge (u,v) is non-metric when some path u→…→v has total distance < d(u,v). Those edges
    are redundant for distance, so removing them preserves all shortest-path distances — which
    is exactly the property that makes the result a "backbone" rather than a filter.
    """
    H = nx.Graph()
    H.add_nodes_from(G.nodes())
    for u, v, d in G.edges(data=True):
        H.add_edge(u, v, dist=dist_of(d["weight"]))
    nonmetric = []
    for u, v, d in list(H.edges(data=True)):
        direct = d["dist"]
        H.remove_edge(u, v)
        try:
            alt = nx.shortest_path_length(H, u, v, weight="dist")
        except nx.NetworkXNoPath:
            alt = float("inf")
        H.add_edge(u, v, dist=direct)
        if alt < direct:
            nonmetric.append((u, v))
    B = H.copy()
    B.remove_edges_from(nonmetric)
    return B, nonmetric


# ------------------------------------------------------------ rich core
def rich_core(G: nx.Graph):
    """Ma & Mondragon rich-core, implemented to the paper rather than from memory.

    THE ALGORITHM (arXiv:1402.4325, Methods):
      - rank nodes by DESCENDING degree
      - for the node at rank r, let k+_r be the number of its links to nodes of a HIGHER rank
      - find r* where k+_r is maximal and stays below that maximum afterwards
      - the core is every node with rank <= r*

    WHAT THE FIRST VERSION GOT WRONG: it maximised the number of edges crossing the candidate
    boundary (a cut size). That is a different quantity and it produced a different core. The
    paper's k+_r is a PER-NODE upward link count, not a cut.

    NOTE ON AN INTERNAL INCONSISTENCY IN THE PAPER: the running text says the maximum is of
    (k_r + k+_r), while the Figure 1 caption says it is of k+_r. Since k_r decreases
    monotonically under degree-ordering, the maximum of (k_r + k+_r) would sit at rank 0 for
    any graph — so the text's formula cannot mean what it appears to say. The caption's version
    is used here.
    """
    deg = dict(G.degree())
    order = sorted(deg, key=lambda n: -deg[n])
    rank = {n: i for i, n in enumerate(order)}
    k_plus = {}
    for n in order:
        k_plus[n] = sum(1 for nb in G.neighbors(n) if rank[nb] < rank[n])

    best_r, best_v = 0, -1
    for r, n in enumerate(order):
        if k_plus[n] > best_v:
            best_v, best_r = k_plus[n], r
    # "and from that node onwards k+_r is always less than k+_{r*}" — scan forward to the last
    # rank that still attains the maximum, so ties do not truncate the core.
    while best_r + 1 < len(order) and k_plus[order[best_r + 1]] == best_v:
        best_r += 1
    return set(order[:best_r + 1]), k_plus


def main() -> int:
    G, sy = load_graph()
    n, m = G.number_of_nodes(), G.number_of_edges()
    deg = [d for _, d in G.degree()]
    print(f"  图: {n} 节点 / {m} 边   平均度 {sum(deg)/len(deg):.1f}   最大度 {max(deg)}   最小度 {min(deg)}")
    print()

    # 1) k-core
    cn, layers = kcore(G)
    mx = max(cn.values())
    print(f"═══ ① k-core 分解 ═══")
    print(f"  最高 coreness: {mx}")
    for k in sorted(layers, reverse=True)[:6]:
        print(f"    k={k:<3} {len(layers[k]):>2} 个节点: {', '.join(layers[k][:10])}{' …' if len(layers[k])>10 else ''}")
    med = sorted(cn.values())[len(cn)//2]
    hi = [x for x, k in cn.items() if k > med]
    print(f"  中位 coreness {med}；高于中位的节点 {len(hi)} 个")
    print(f"    最顶层(k={mx}) 的节点数: {len(layers[mx])}")
    print()

    # 2) metric backbone
    B, nonmetric = metric_backbone(G)
    print(f"═══ ② 度量骨架（d = 1/w）═══")
    print(f"  原始边 {m} → 骨架边 {B.number_of_edges()}   移除非度量边 {len(nonmetric)}")
    comps = list(nx.connected_components(B))
    print(f"  骨架连通分量: {len(comps)}   最大分量 {max(len(c) for c in comps)} 节点")
    bd = [d for _, d in B.degree()]
    print(f"  骨架平均度 {sum(bd)/len(bd):.1f}   最小度 {min(bd)}")
    # does the backbone now have cut vertices?
    arts = list(nx.articulation_points(B))
    print(f"  骨架中的割点（删后断裂）: {len(arts)} 个: {', '.join(arts[:12])}")
    print()

    # 3) rich core
    rc, curve = rich_core(G)
    print(f"═══ ③ 富核（Ma & Mondragon）═══")
    print(f"  核心大小: {len(rc)} / {n}")
    print(f"  核心节点: {', '.join(sorted(rc))}")
    print()

    # compare
    print(f"═══ 三者对照 ═══")
    top_kcore = set(layers[mx])
    print(f"  k-core 顶层 ({len(top_kcore)}): {', '.join(sorted(top_kcore))}")
    print(f"  富核      ({len(rc)}): {', '.join(sorted(rc))}")
    print(f"  骨架割点  ({len(arts)}): {', '.join(sorted(arts)[:12])}")
    print()
    inter = top_kcore & rc
    print(f"  k-core ∩ 富核 = {len(inter)} 个: {', '.join(sorted(inter)) or '（空）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
