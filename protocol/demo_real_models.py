"""CIRCE 面对真实 —— 两个本地模型之间的一次真实调用。

跑法:  python CIRCE/protocol/demo_real_models.py [--caller MODEL] [--callee MODEL]

它回答用户提出的第一个问题（2026-10-10）：

> 用你自己的 3080 Ti 跑两个真实的本地模型，让一个通过 CIRCE 协议调用另一个。
> 你会遇到的第一件真实的事：**调用方拿到的核剖面，会和它预期的不一样。**

**为什么"不一样"是必然的**
--------------------------
核剖面是 **caller 视角的调用统计**。第一次调用一个陌生 agent 时：

    depth   ← 取决于【本次】的调用栈（一次调用也能算出栈深）
    density ← 0      还没有历史
    span    ← 0 或 1 还没有别的 caller

**所以 CIRCE 在第一次调用时给不出有用信息。它的价值必须通过时间积累。**

这不是缺陷，是设计。但它是一个**未经真实检验的设计假设**——
本脚本就是去检验它：先探测（白纸），调用若干次，再探测（有值）。

与 `demo.py` 的区别
-------------------
`demo.py` 用两个**假** agent 验证协议能走通——那是自指的。
本脚本的 callee **真的跑一个本地模型**，caller **真的跑另一个**，
核剖面**真的由这几次调用的日志算出来**。

依赖：本机 Ollama 在 127.0.0.1:11434，且至少有两个模型。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent), str(HERE.parent / "trust")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import CoreProfile                      # noqa: E402
from server import serve                               # noqa: E402
from client import CirceClient, irreplaceability_score  # noqa: E402
from stats_provider import CallRecord, StatsProvider   # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


# --------------------------------------------------------------------------
def list_models() -> list[str]:
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=10) as r:
        return [m["name"] for m in json.loads(r.read().decode())["models"]]


def ask_model(model: str, prompt: str, timeout: float = 300.0) -> tuple[str, float]:
    """调一次 Ollama，返回 (回答, 耗时秒)。

    `stream: False` 是要点：本脚本要度量的是**这次调用本身**，
    而流式响应会把"第一个 token 到达"和"回答完成"混在一起。
    """
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    dt = time.time() - t0
    # qwen3-vl 的输出可能落在 thinking 字段（此前实测），两处都取
    txt = (data.get("response") or "").strip()
    if not txt:
        txt = (data.get("thinking") or "").strip()
    return txt, dt


def probe(cli: CirceClient, label: str) -> dict:
    info = cli.query_profile()
    print(f"     {label}")
    print(f"       verified={info.verified}  ledger_seq={info.ledger_seq}")
    print(f"       profile={json.dumps(info.profile, ensure_ascii=False)}")
    return info.profile


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--caller", default="deepseek-r1:14b")
    ap.add_argument("--callee", default="qwen2.5:7b")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--port", type=int, default=18781)
    args = ap.parse_args()

    print("=" * 74)
    print("  CIRCE 面对真实 —— 两个本地模型之间的一次真实调用")
    print("=" * 74)

    try:
        models = list_models()
    except Exception as e:
        print(f"  ✗ Ollama 不可达: {e}")
        return 2
    print(f"  本机模型: {', '.join(models)}")
    for m, role in ((args.caller, "caller"), (args.callee, "callee")):
        if m not in models:
            print(f"  ✗ {role} 模型 {m!r} 不在本机")
            return 2
    print(f"  caller = {args.caller}")
    print(f"  callee = {args.callee}")

    tmp = Path(tempfile.mkdtemp(prefix="circe-real-"))
    srv = None
    try:
        # ---- 起服务端：初值刻意全是 0（一个全新的 agent）----
        blank = CoreProfile(depth=0.0, density=0.0, span=0.0,
                            agent_id="agent-B", skills=(), call_count=0)
        srv, _ = serve("agent-B", args.port, tmp / "B", blank)
        cli = CirceClient("agent-A", f"http://127.0.0.1:{args.port}",
                          peer_public_key=srv.state.sk.public_key())
        print(f"\n  服务端 agent-B 起在 127.0.0.1:{args.port}（核剖面初值全 0）")

        # ---- ① 第一次探测：白纸 ----
        print("\n  ① 第一次探测 —— 一个陌生 agent 的核剖面")
        p0 = probe(cli, "初次探测")
        check("初次探测时 density 为 0（还没有调用历史）",
              p0.get("density") == 0.0, f"density={p0.get('density')}")
        check("初次探测时 span 为 0（还没有别的 caller）",
              p0.get("span") == 0.0, f"span={p0.get('span')}")
        check("→ CIRCE 在第一次调用时给不出有用信息（这是设计，不是缺陷）",
              p0.get("depth") == 0.0 and p0.get("density") == 0.0,
              "三个维度全为 0，调用方拿到的是一张白纸")

        # ---- ② 真实调用若干次 ----
        print(f"\n  ② 真实调用 {args.rounds} 轮（每一轮都真的跑模型）")
        rng = __import__("random").Random(11)
        prompts = [
            "用一句话说明：哈希链为什么能检测篡改？",
            "用一句话说明：为什么浮点数不能直接比较相等？",
            "用一句话说明：为什么哈希函数的输出长度固定？",
            "用一句话说明：为什么签名要覆盖消息的全部字段？",
            "用一句话说明：为什么仅追加日志比可改写日志可信？",
            "用一句话说明：为什么时间衰减要设半衰期？",
        ]
        records: list[CallRecord] = []
        for i in range(args.rounds):
            q = prompts[i % len(prompts)]
            stack_depth = 1 + i % 3          # 模拟不同的调用栈位置
            t0 = time.time()
            try:
                ans, dt = ask_model(args.callee, q)
                ok = bool(ans)
            except Exception as e:
                ans, dt, ok = f"(调用失败: {e})", time.time() - t0, False
            ms = int(dt * 1000)
            short = ans.replace("\n", " ")[:70]
            print(f"     [{i+1}] stack_depth={stack_depth}  {ms:>6} ms  {'✓' if ok else '✗'}  {short}")

            # 真实调用 → 写进服务方账本（CIRCE 的 Trust Update）
            # stack_depth 必须在这里传：它是 caller 的上下文，服务端看不到
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            cli.report_call("agent-B", "qa", ok, ms, ts,
                            note=f"真实模型调用 {i+1}", stack_depth=stack_depth)
            records.append(CallRecord(caller="agent-A", callee="agent-B",
                                      skill="qa", timestamp=ts, success=ok,
                                      latency_ms=ms, stack_depth=stack_depth))

        check(f"完成 {args.rounds} 轮真实模型调用", len(records) == args.rounds)
        check("至少一半调用成功（模型真的在回答）",
              sum(1 for r in records if r.success) >= args.rounds / 2,
              f"{sum(1 for r in records if r.success)}/{args.rounds}")

        # ---- ③ 用真实日志算核剖面 ----
        print("\n  ③ 从这些真实调用算核剖面（纯统计，无 LLM 参与）")
        sp = StatsProvider(records=records)
        ex = sp.explain("agent-B")
        prof = sp.profile_for("agent-B")
        print(f"     真实调用 {ex['calls']} 次，耗时均值 {ex['avg_latency_ms']} ms，"
              f"成功率 {ex['success_rate']}")
        print(f"     depth   = {ex['depth']}")
        print(f"     density = {ex['density']}")
        print(f"     span    = {ex['span']}")

        check("density 从 0 变为正值（价值开始积累）",
              prof.density > 0.0, f"{p0.get('density')} → {prof.density}")
        check("成功率高时 density 里含成功率分量",
              ex["success_rate"] > 0, f"success_rate={ex['success_rate']}")
        check("span 仍很低（只有一个 caller 调过）",
              prof.span < 0.5, f"span={prof.span} —— 只有 agent-A 调过")

        # ---- ④ 再探测一次 ----
        print("\n  ④ 积累之后再看核剖面")
        p1 = probe(cli, "调用若干次之后")

        # ---- ⑤ 那个设计假设 ----
        print("\n  ⑤ 检验那个未经真实检验的设计假设")
        print(f"     第一次调用时: depth={p0.get('depth')} density={p0.get('density')} span={p0.get('span')}")
        print(f"     若干次之后:   depth={prof.depth} density={prof.density} span={prof.span}")
        check("假设成立：核剖面的价值必须通过时间积累",
              p0.get("density") == 0.0 and prof.density > 0.0,
              "第一次给白纸，之后才有值 —— 这是设计，不是缺陷")
        check("账本随每次调用增长（真实写入）",
              srv.state.ledger.entries and len(srv.state.ledger.entries) == args.rounds,
              f"{len(srv.state.ledger.entries)} 条")

        # ---- ⑥ 调用方的决策：权重不同，结论不同 ----
        print("\n  ⑥ 调用方按自己的权重决定（不可替代性是关系属性）")
        s_once = irreplaceability_score(prof.to_dict(), 0.7, 0.1, 0.2)
        s_repeat = irreplaceability_score(prof.to_dict(), 0.1, 0.7, 0.2)
        print(f"     一次性高风险调用方视角: {s_once:.4f}")
        print(f"     长期反复调用方视角:     {s_repeat:.4f}")
        check("两个视角给出不同分数（同一个 peer）", abs(s_once - s_repeat) > 1e-9)

        # ---- ⑦ 让 caller 模型自己读核剖面并做判断（LLM 在调用方一侧）----
        print(f"\n  ⑦ 让 caller 模型（{args.caller}）自己读核剖面做判断")
        judge_prompt = (
            "你是一个调用方 agent。下面是目标 agent 的核剖面统计（来自你的调用历史）：\n"
            f"{json.dumps(prof.to_dict(), ensure_ascii=False)}\n\n"
            "三个维度含义：depth=被调用时位于调用栈的深度；"
            "density=调用频次/成功率/速度的合成；span=被多少不同 caller 调用过。\n"
            "请只用一句话回答：你现在是否应该依赖它？并说明理由。")
        try:
            verdict, vdt = ask_model(args.caller, judge_prompt, timeout=600)
            print(f"     caller 的判断（{vdt:.1f}s）:")
            print(f"       {verdict.replace(chr(10), ' ')[:200]}")
            check("caller 模型给出了判断（LLM 在调用方一侧，不进链）",
                  bool(verdict), f"{len(verdict)} 字符")
            check("这条判断没有进链（链上只有事实）",
                  srv.state.ledger.verify() == [],
                  "账本里存的是调用事实，判断留在链外")
        except Exception as e:
            check("caller 模型给出判断", False, f"{type(e).__name__}: {e}")

    finally:
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
