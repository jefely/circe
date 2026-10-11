"""CIRCE 端到端演示 —— 原文 §六 的「首个实验」。

原文：

    首个实验：在两个本地模型之间建立 CIRCE 信任通道，
              一方提供服务，另一方通过核探测决定是否调用。

本演示用两个本地 agent 进程（同一台机器、不同端口）走完三个端点：

    agent-A（调用方）           agent-B（服务方）
        │                            │
        │  ① Readiness Probe  ──────►│   便宜，不带核剖面
        │  ◄────── ready             │
        │                            │
        │  ② Core Profile Query ────►│   取核剖面 + VC + 链尾
        │  ◄── profile + VC + head   │
        │      验签 / 核哈希比对 / 链尾比对
        │                            │
        │  ③ 决定是否调用（按调用方自己的权重）│
        │                            │
        │  ④ Trust Update ──────────►│   追加账本，时间衰减重算
        │  ◄── seq + head           │
        │                            │
        │  ⑤ 再查一次：剖面应随交互变化 │

跑法:  python CIRCE/protocol/demo.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # endpoints
sys.path.insert(0, str(HERE.parent))          # trust

from endpoints import CoreProfile                      # noqa: E402
from server import serve                               # noqa: E402
from client import CirceClient, irreplaceability_score  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("  CIRCE 端到端演示 —— 两个 agent 之间的信任通道")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="circe-demo-"))
    try:
        # ---- 起服务方 B ----
        portB = 18771
        profB = CoreProfile(depth=0.82, density=0.61, span=0.44,
                            agent_id="agent-B", skills=("coding", "analysis"),
                            call_count=0, last_updated="2026-10-10T00:00:00Z")
        srvB, _ = serve("agent-B", portB, tmp / "B", profB)
        print(f"\n  服务方 agent-B 起在 127.0.0.1:{portB}")
        print(f"  初始核剖面: depth={profB.depth} density={profB.density} span={profB.span}")

        # 服务方的公钥交给调用方（真实场景经带外交换或 DID 解析）
        pubB = srvB.state.sk.public_key()
        cli = CirceClient("agent-A", f"http://127.0.0.1:{portB}", peer_public_key=pubB)

        # ---- ① Readiness Probe ----
        print("\n  ① Readiness Probe")
        r = cli.probe()
        print(f"     → {json.dumps(r, ensure_ascii=False)}")
        check("探活返回 ready", r.get("ready") is True)
        check("探活不返回核剖面（高频调用要便宜）", "profile" not in r)

        # ---- ② Core Profile Query ----
        print("\n  ② Core Profile Query")
        info = cli.query_profile()
        print(f"     agent={info.agent_id}  ledger_seq={info.ledger_seq}")
        print(f"     剖面={json.dumps(info.profile, ensure_ascii=False)}")
        print(f"     验签: verified={info.verified}  {info.verify_note}")
        check("拿到核剖面", info.profile.get("depth") is not None)
        check("凭据验签通过且核哈希吻合", info.verified, info.verify_note)
        check("链尾非空（锚定存在）", len(info.ledger_head) == 64)

        # ---- ③ 按调用方自己的权重决定 ----
        print("\n  ③ 决定是否调用 —— 权重由调用方给，不是内置的")
        # 场景一：只做一次高风险决策 → 看重 depth
        s_once = irreplaceability_score(info.profile, 0.7, 0.1, 0.2)
        # 场景二：要长期反复调用 → 看重 density
        s_repeat = irreplaceability_score(info.profile, 0.1, 0.7, 0.2)
        print(f"     一次性高风险调用方视角: {s_once:.4f}")
        print(f"     长期反复调用方视角:     {s_repeat:.4f}")
        check("同一个 peer 在不同调用方视角下分数不同",
              abs(s_once - s_repeat) > 1e-6,
              "→ 不可替代性是【关系】属性，不是 peer 的属性")
        try:
            irreplaceability_score(info.profile, 0.5, 0.5, 0.5)
            check("权重和不为 1 → 应被拒", False, "（竟然通过了！）")
        except ValueError:
            check("权重和不为 1 → 被拒", True)

        # ---- ④ Trust Update ----
        print("\n  ④ Trust Update —— 调用之后写账本")
        u1 = cli.report_call("agent-B", "coding", True, 812,
                             "2026-10-10T12:00:00Z", note="首次调用")
        print(f"     {json.dumps(u1, ensure_ascii=False)[:150]}")
        check("更新被接受", u1.get("accepted") is True)
        check("账本序号推进到 1", u1.get("ledger_seq") == 1)
        check("链尾随之改变", u1.get("ledger_head") != info.ledger_head)

        for i, ok in enumerate([True, False, True], start=2):
            cli.report_call("agent-B", "analysis", ok, 500 + i * 100,
                            f"2026-10-10T12:0{i}:00Z")
        u_last = cli.report_call("agent-B", "coding", True, 300,
                                 "2026-10-10T12:05:00Z")
        check("多次更新后序号连续", u_last.get("ledger_seq") == 5,
              f"seq={u_last.get('ledger_seq')}")
        np_ = u_last.get("new_profile") or {}
        check("重算里带上了成功率", "success_rate" in np_,
              f"success_rate={np_.get('success_rate')}")

        # ---- ⑤ 时间衰减真的在起作用 ----
        print("\n  ⑤ 时间衰减 —— 旧的调用权重更低")
        w = np_.get("weighted_calls")
        raw = u_last.get("ledger_seq")
        check("加权调用数小于原始条数（衰减在起作用）",
              w is not None and w < raw,
              f"加权 {w} < 原始 {raw} 条（间隔 5 分钟，半衰期 30 天，故只略小）")

        # ---- ⑥ 账本没被动过 ----
        print("\n  ⑥ 账本完整性")
        probs = srvB.state.ledger.verify()
        check("服务方自己的账本校验通过", probs == [], str(probs[:1]))

        # ---- ⑦ 篡改会被调用方发现 ----
        print("\n  ⑦ 篡改检测 —— 改了账本，服务方必须【拒签】而不是签一份看着有效的")
        srvB.state.ledger.entries[0].profile["success"] = False   # 篡改历史
        probs2 = srvB.state.ledger.verify()
        check("篡改账本第 1 条 → 服务方自己就报错", len(probs2) >= 1,
              probs2[0] if probs2 else "（没抓住！）")

        info2 = cli.query_profile()
        print(f"     篡改后再查: verified={info2.verified}  {info2.verify_note}")
        check("调用方拿到的是「拒签」而非一份有效凭据", not info2.verified,
              info2.verify_note)
        check("拒签的原因指向账本，而不是「peer 不在」",
              "账本" in info2.verify_note or "409" in info2.verify_note,
              info2.verify_note)

        # 链尾恰好没变这个事实，值得单独记录：它是初版漏洞的成因
        check("改中间一条不会改变链尾（所以只锚链尾不够）",
              srvB.state.ledger.head == u_last.get("ledger_head"),
              "→ 正因如此，签发前必须先 verify()")

        srvB.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 70)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
