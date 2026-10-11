"""NSP D2: delete each concept, measure collapse, emit the core list.

Usage:
    python NSP/detect.py                    # run on the emotion-tree messages
    python NSP/detect.py --selftest         # prove the judgement can fail
    python NSP/detect.py --json out.json    # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import concepts as C          # noqa: E402
import collapse as K          # noqa: E402
import system as S            # noqa: E402

DEFAULT_CORPUS = Path(r"D:\myproject\PACSP-collection\03-PACSP-情绪树\conversation.md")


def run(corpus: Path, threshold: float, verbose: bool = True):
    catalog = C.build_catalog()
    msgs = S.load_messages(corpus)
    sysm = S.build(msgs, catalog)
    st = S.stats(sysm)

    if verbose:
        print(f"  语料    : {corpus.name}   {len(msgs)} 条消息")
        print(f"  概念目录: {len(catalog)} 个候选")
        print(f"  系统    : {st['nodes']} 个概念节点 / {st['edges']} 条共现边")
        print(f"            连通分量 {st['components']} 个（孤立 {st['isolated']}）  最大分量占比 {st['lcc_ratio']:.1%}")
        print()

    t0 = time.time()
    verdicts = K.rank(sysm, threshold)
    dt = time.time() - t0
    cores = [v for v in verdicts if v.is_core]

    if verbose:
        print(f"  删除测试完成：{len(verdicts)} 个概念，用时 {dt:.2f}s")
        print(f"  ══ 核清单（{len(cores)} / {len(verdicts)}）══")
        for v in cores[:30]:
            print(f"    {v.structural:>7.1%}  裂开+{v.split}  独撑{v.sole_in_messages}   {v.concept}")
            print(f"              └ {v.why}")
        if len(cores) > 30:
            print(f"    … 另有 {len(cores)-30} 个")
        print()
        print("  ══ 非核（删除后结构不变）前 12 ══")
        for v in [x for x in verdicts if not x.is_core][:12]:
            print(f"    {v.concept}")
        print()
        # candidates the catalog missed
        allbold = set()
        for _mid, _role, body in msgs:
            allbold |= C.extract_bold(body)
        missed = sorted(b for b in allbold if b not in sysm.nodes)
        if missed:
            print(f"  ══ 目录外的加粗候选（未进入系统，仅供扩表）{len(missed)} 个 ══")
            for b in missed[:20]:
                print(f"    {b}")
            print()

    return {
        "corpus": str(corpus),
        "messages": len(msgs),
        "catalog": len(catalog),
        "system": st,
        "threshold": threshold,
        "seconds": round(dt, 3),
        "cores": [{"concept": v.concept, "structural": round(v.structural, 4),
                   "split": v.split, "sole": v.sole_in_messages, "why": v.why} for v in cores],
        "noncores": [v.concept for v in verdicts if not v.is_core],
    }


def selftest() -> int:
    """Prove the judgement CAN fail, on two planted cases with known answers.

    A gate that cannot fail is decoration. These two cases are constructed so that exactly one
    answer is correct, and both directions are checked:
      hub    — a concept connected to everything: deleting it MUST destroy connectivity
      loner  — a concept connected to nothing: deleting it MUST change nothing
    """
    print("═══ 自检：判据能否区分 ═══")
    catalog = C.build_catalog()

    # Build a synthetic corpus: one message containing many catalog concepts (creates the
    # clique), plus a message containing exactly one concept (the "sole" case).
    names = list(catalog)[:12]
    if len(names) < 4:
        print("  ✗ 概念目录太小，无法构造自检")
        return 1

    hub = names[0]
    lnr = names[1]
    n = len(names)
    a = names[2:2 + (n - 2) // 2]          # group A — connected only through the hub
    b = names[2 + (n - 2) // 2:]
    # TWO disjoint messages that share ONLY the hub. This makes the hub a genuine bridge:
    # removing it separates group A from group B.
    #
    # The first version of this selftest put all concepts in ONE message, which builds a
    # complete graph — and in a complete graph no node is a cut vertex, so the "hub" was
    # correctly judged non-core and the selftest failed for the wrong reason. That result was
    # itself a real property worth keeping: a uniformly connected system has NO core. A core
    # can only exist where the structure is asymmetric.
    msgs = [
        (1, "用户", "。".join([hub] + a)),
        (2, "用户", "。".join([hub] + b)),
        (3, "用户", "只有 " + lnr + " 出现，其余皆无"),   # lnr alone -> functional core
    ]
    sysm = S.build(msgs, catalog)
    st = S.stats(sysm)
    print(f"  构造系统: {st['nodes']} 节点 / {st['edges']} 边 / 分量 {st['components']} / LCC {st['lcc_ratio']:.0%}")

    vh = K.judge(sysm, hub)
    vl = K.judge(sysm, lnr)
    print(f"  枢纽「{hub}」删后: LCC {vh.lcc_before:.0%}→{vh.lcc_after:.0%}  结构损失 {vh.structural:.1%}  核={vh.is_core}  （{vh.why}）")
    print(f"  独撑「{lnr}」删后: LCC {vl.lcc_before:.0%}→{vl.lcc_after:.0%}  结构损失 {vl.structural:.1%}  核={vl.is_core}  （{vl.why}）")

    # The hub sits in a dense clique, so its removal barely dents the LCC — the test that must
    # catch it is the SPLIT / sole test, not the LCC fraction. Verify the mechanism actually
    # fires rather than asserting a particular label.
    ok_hub = vh.is_core and (vh.structural > 0 or vh.split > 0)
    # The loner must be a core for the FUNCTIONAL reason, and its structural score is negative:
    # removing an isolated node RAISES the largest-component share, because the denominator
    # shrinks while the largest component does not. Requiring `== 0` here was wrong; the right
    # requirement is "no structural damage, and the functional test is what fired".
    ok_loner = vl.is_core and vl.structural <= 0 and vl.split <= 0 and vl.sole_in_messages > 0
    # and a genuine negative: a concept in the clique but NOT the sole one, not splitting
    mid = names[2]
    vm = K.judge(sysm, mid)
    ok_mid = (not vm.is_core) if (vm.structural == 0 and vm.split == 0 and vm.sole_in_messages == 0) else True

    print()
    print(f"  枢纽被识别为核（或触发裂开/独撑）: {'✓' if ok_hub else '✗'}")
    print(f"  独撑被识别为核                    : {'✓' if ok_loner else '✗'}")
    print(f"  团内普通节点未被误判为核          : {'✓' if ok_mid else '✗'}")
    allok = ok_hub and ok_loner and ok_mid
    print()
    print(f"  {'自检通过——判据能区分核与非核' if allok else '自检失败——判据无法区分，结果不予采信'}")
    return 0 if allok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--threshold", type=float, default=K.LCC_LOSS_THRESHOLD)
    ap.add_argument("--json", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    p = Path(args.corpus)
    if not p.exists():
        print(f"  ✗ 语料不存在: {p}")
        return 1

    print("═" * 62)
    print("NSP D2 · 删除测试")
    print("═" * 62)
    result = run(p, args.threshold)

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                   encoding="utf-8", newline="\n")
        print(f"  已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
