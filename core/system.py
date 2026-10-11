"""The system under test: a concept co-occurrence graph built from messages.

WHY A GRAPH AND NOT THE RAW TEXT
--------------------------------
The delete test needs "does the system still hold together after removing x". Raw text has no
such property to inspect. A co-occurrence graph does: removing a concept removes its edges, and
whether the remainder stays connected is a question with an exact answer. D2 asks about
structure, so the system has to BE a structure.

WEIGHTS
-------
An edge's weight is the number of messages in which the two concepts co-occur. Unweighted
structure could have been used, but weights let a later version ask "is this edge load-bearing"
without rebuilding the system.
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class System:
    nodes: set[str] = field(default_factory=set)
    edges: dict[tuple[str, str], int] = field(default_factory=dict)
    # which concepts occur in which message — needed for the functional-collapse test
    msg_concepts: list[tuple[int, str, set[str]]] = field(default_factory=list)
    n_messages: int = 0

    def degree(self, x: str) -> int:
        return sum(1 for (a, b) in self.edges if x in (a, b))

    def neighbours(self, x: str) -> set[str]:
        out = set()
        for (a, b) in self.edges:
            if a == x:
                out.add(b)
            elif b == x:
                out.add(a)
        return out

    def without(self, x: str) -> "System":
        """Remove one unit. This IS the delete operation of the protocol."""
        s = System(nodes=self.nodes - {x}, n_messages=self.n_messages)
        s.edges = {k: v for k, v in self.edges.items() if x not in k}
        s.msg_concepts = [(i, r, cs - {x}) for i, r, cs in self.msg_concepts]
        return s


MSG = re.compile(r"^## 消息 (\d+) — (\S+)", re.M)


def load_messages(path: Path) -> list[tuple[int, str, str]]:
    """[(messageId, role, body)] straight from conversation.md."""
    t = path.read_text(encoding="utf-8", errors="replace")
    parts = MSG.split(t)
    out = []
    for i in range(1, len(parts) - 2, 3):
        out.append((int(parts[i]), parts[i + 1], parts[i + 2]))
    return out


def build(messages, catalog: dict) -> System:
    from concepts import occurrences

    sysm = System(n_messages=len(messages))
    for mid, role, body in messages:
        cs = occurrences(body, catalog)
        if not cs:
            continue
        sysm.msg_concepts.append((mid, role, cs))
        sysm.nodes |= cs
        cl = sorted(cs)
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                key = (cl[i], cl[j])
                sysm.edges[key] = sysm.edges.get(key, 0) + 1
    return sysm


def components(sysm: System) -> list[set[str]]:
    """Connected components, exact (BFS)."""
    seen: set[str] = set()
    comps: list[set[str]] = []
    for n in sysm.nodes:
        if n in seen:
            continue
        stack, comp = [n], set()
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            for nb in sysm.neighbours(cur):
                if nb not in comp:
                    stack.append(nb)
        seen |= comp
        comps.append(comp)
    return comps


def lcc_ratio(sysm: System) -> float:
    """Largest connected component as a fraction of all nodes."""
    if not sysm.nodes:
        return 0.0
    return max(len(c) for c in components(sysm)) / len(sysm.nodes)


def stats(sysm: System) -> dict:
    comps = components(sysm)
    return {
        "nodes": len(sysm.nodes),
        "edges": len(sysm.edges),
        "components": len(comps),
        "lcc": max((len(c) for c in comps), default=0),
        "lcc_ratio": lcc_ratio(sysm),
        "isolated": sum(1 for c in comps if len(c) == 1),
    }
