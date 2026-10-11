"""Collapse judgements for the NSP framework.

NSP names three kinds of collapse (functional / structural / consistency) but gives no
executable test for any of them. An untestable "collapse" cannot support a core claim, so each
one implemented here is reduced to a quantity that can be computed and that can FAIL.

STRUCTURAL — connectivity. NSP says structural collapse means "the topology no longer holds".
Connectivity is the smallest computable proxy for topology, and crucially it uses no embedding,
no scale parameter and no vocabulary — so it cannot inherit D1's mediators.
    score = (LCC(G) - LCC(G-x)) / LCC(G)      in [0, 1]
plus the change in component count (a removal that splits the graph is a collapse even when
the largest component barely shrinks).

FUNCTIONAL — a concept that is the ONLY concept in some message carries that message alone.
Removing it leaves that message with nothing attached. This is a countable fact, not a
threshold.

CONSISTENCY — NOT IMPLEMENTED. It belongs to axiom systems and proof chains, where
"contradiction" has a definition. A co-occurrence graph has no notion of self-consistency, so
any score here would measure the constructor's imagination rather than the system. Left out
deliberately, and reported as left out.
"""
from __future__ import annotations

from dataclasses import dataclass

from system import System, components, lcc_ratio


@dataclass
class Verdict:
    concept: str
    lcc_before: float
    lcc_after: float
    comps_before: int
    comps_after: int
    structural: float          # relative LCC loss, 0..1
    split: int                 # increase in component count
    sole_in_messages: int      # messages where this concept is the only one
    is_core: bool
    why: str


# A removal counts as structural collapse when EITHER the largest component loses this much,
# OR the graph splits into more pieces. Both are exact; the fraction is the only tunable, and
# it is exposed rather than buried.
LCC_LOSS_THRESHOLD = 0.10


def judge(sysm: System, x: str, lcc_loss_threshold: float = LCC_LOSS_THRESHOLD) -> Verdict:
    before = lcc_ratio(sysm)
    comps_b = len(components(sysm))
    red = sysm.without(x)
    after = lcc_ratio(red)
    comps_a = len(components(red))

    structural = (before - after) / before if before > 0 else 0.0
    split = comps_a - comps_b
    sole = sum(1 for _mid, _role, cs in sysm.msg_concepts if cs == {x})

    # Core if removal collapses structure OR the concept alone carries some message.
    is_core = (structural >= lcc_loss_threshold) or (split > 0) or (sole > 0)

    reasons = []
    if structural >= lcc_loss_threshold:
        reasons.append(f"最大连通分量损失 {structural:.1%} ≥ {lcc_loss_threshold:.0%}")
    if split > 0:
        reasons.append(f"图裂开 +{split} 个分量")
    if sole > 0:
        reasons.append(f"独撑 {sole} 条消息")
    why = "；".join(reasons) if reasons else "删除后结构不变"

    return Verdict(x, before, after, comps_b, comps_a, structural, split, sole, is_core, why)


def rank(sysm: System, threshold: float = LCC_LOSS_THRESHOLD) -> list[Verdict]:
    """Every concept gets a verdict — the protocol is exhaustive, not sampling."""
    out = [judge(sysm, x, threshold) for x in sorted(sysm.nodes)]
    out.sort(key=lambda v: (-v.structural, -v.split, -v.sole_in_messages))
    return out
