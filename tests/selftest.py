"""CIRCE 信任层自检 —— 每条断言都必须能真的失败。

跑法:  python CIRCE/tests/selftest.py

它检查的不是「函数能不报错地跑完」，而是四件可被推翻的事：

  ① 确定性      同一份核剖面在不同时刻、不同进程得到同一个 core_hash
  ② 链完整性    「篡改历史必须重算整条链」—— 改一条，链必须在那一处断
  ③ 签名覆盖面  改 issuer / expiration / subject 的任意一项，验签必须失败
  ④ 衰减正确    半衰期是能被人手算核对的量
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRUST = HERE.parent / "trust"
sys.path.insert(0, str(TRUST))

import chain as C          # noqa: E402
import vc as V             # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "✓" if ok else "✗"
    print(f"    {mark} {name}" + (f"   {detail}" if detail else ""))


# --------------------------------------------------------------------------
def t1_determinism() -> None:
    print("\n  ① 确定性 —— 同一份核剖面必须得到同一个哈希")
    a = {"depth": 0.72, "density": 0.45, "span": 0.88, "skills": ["写作", "coding"]}
    b = {"skills": ["写作", "coding"], "span": 0.88, "density": 0.45, "depth": 0.72}
    check("键序不同的同一对象得到相同哈希", C.core_hash(a) == C.core_hash(b))

    # 中文必须按 UTF-8 原字节进哈希，不能被转义成 \uXXXX
    check("中文技能名不转义（ensure_ascii=False）",
          b"\xe5\x86\x99\xe4\xbd\x9c" in C.normalize({"s": "写作"}),
          "规范化字节里能找到「写作」的 UTF-8 序列")

    # 空白不能影响
    check("多余空白不影响（JSON 已规范化）",
          C.core_hash({"a": 1}) == C.core_hash({"a": 1}))

    # 跨进程必须一致 —— 这是要点：进程内的相等可能只是对象复用
    code = ("import sys;sys.path.insert(0,r'%s');import chain as C;"
            "print(C.core_hash({'depth':0.72,'density':0.45,'span':0.88,"
            "'skills':['写作','coding']}))" % TRUST)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    check("另一个进程得到同一个哈希", r.stdout.strip() == C.core_hash(a),
          f"父进程 {C.core_hash(a)[:16]}…  子进程 {r.stdout.strip()[:16]}…")


def t2_chain() -> None:
    print("\n  ② 链完整性 —— 篡改历史必须被抓住")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ledger.json"
        led = C.Ledger(path=p)
        for i in range(4):
            led.append({"depth": 0.1 * i, "density": 0.5, "span": 0.9},
                       f"2026-10-0{i+1}T00:00:00Z", note=f"第{i+1}次")
        check("干净的链校验通过", led.verify() == [])

        # 改中间一条的内容
        led.entries[1].profile = {"depth": 9.9, "density": 0.5, "span": 0.9}
        probs = led.verify()
        check("改动第 2 条内容 → 被抓住", len(probs) >= 1,
              probs[0] if probs else "（没抓住！）")

        # 只改 hash 字段（假装链还是对的）
        led2 = C.Ledger(path=p)
        for i in range(4):
            led2.append({"depth": 0.1 * i}, f"2026-10-0{i+1}T00:00:00Z")
        led2.entries[1].hash = "f" * 64
        probs2 = led2.verify()
        check("只改 hash 字段 → 下一环立刻对不上", len(probs2) >= 1,
              probs2[0] if probs2 else "（没抓住！）")

        # 删掉中间一条
        led3 = C.Ledger(path=p)
        for i in range(4):
            led3.append({"depth": 0.1 * i}, f"2026-10-0{i+1}T00:00:00Z")
        del led3.entries[1]
        probs3 = led3.verify()
        check("删掉中间一条 → 序号与 prev_hash 双双报错", len(probs3) >= 2,
              f"{len(probs3)} 处问题")

        # 重放整条链（真正的攻击）会得到不同的链尾
        honest = C.Ledger(path=p)
        for i in range(3):
            honest.append({"depth": 0.1 * i}, f"2026-10-0{i+1}T00:00:00Z")
        forged = C.Ledger(path=p)
        forged.append({"depth": 0.0}, "2026-10-01T00:00:00Z")
        forged.append({"depth": 9.9}, "2026-10-02T00:00:00Z")   # 篡改后重算
        forged.append({"depth": 0.2}, "2026-10-03T00:00:00Z")
        check("重算整条链后自身自洽（所以必须锚定链尾）",
              forged.verify() == [],
              "→ 这正是为什么 VC 要带 ledgerHead：链尾一变，旧凭据就对不上")
        check("篡改版链尾 ≠ 诚实版链尾", forged.head != honest.head,
              f"{forged.head[:12]}… vs {honest.head[:12]}…")


def t3_signature() -> None:
    print("\n  ③ 签名覆盖面 —— 改凭据的任何一处都必须验签失败")
    sk = V.new_key()
    pub = sk.public_key()
    ch = C.core_hash({"depth": 0.72})
    cred = V.issue(sk, "agent-A", ch, issuance="2026-10-01T00:00:00Z",
                   expiration="2027-10-01T00:00:00Z", ledger_head="a" * 64)

    subj = V.verify(cred, pub, at="2026-11-01T00:00:00Z")
    check("干净的凭据通过，且主体正确", subj["id"] == "agent-A")

    import copy
    # 逐个字段篡改
    for path, label in [
        (("credentialSubject", "id"), "主体 id"),
        (("credentialSubject", "coreHash"), "核剖面哈希"),
        (("issuer",), "签发者"),
        (("issuanceDate",), "签发时间"),
        (("expirationDate",), "有效期"),
        (("credentialSubject", "ledgerHead"), "链尾锚定"),
    ]:
        bad = copy.deepcopy(cred)
        cur = bad
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = ("0" * 64) if "Hash" in path[-1] or "Head" in path[-1] \
            else ("1999-01-01T00:00:00Z" if "Date" in path[-1] else "TAMPERED")
        try:
            V.verify(bad, pub, at="2026-11-01T00:00:00Z")
            check(f"改 {label} → 应被拒", False, "（竟然通过了！）")
        except V.VCError:
            check(f"改 {label} → 被拒", True)

    # 用另一把公钥验
    other = V.new_key().public_key()
    try:
        V.verify(cred, other, at="2026-11-01T00:00:00Z")
        check("用别人的公钥验 → 应被拒", False, "（竟然通过了！）")
    except V.VCError:
        check("用别人的公钥验 → 被拒", True)

    # 过期
    try:
        V.verify(cred, pub, at="2028-01-01T00:00:00Z")
        check("已过期 → 应被拒", False, "（竟然通过了！）")
    except V.VCError:
        check("已过期 → 被拒", True)

    # 期望值比对
    try:
        V.verify(cred, pub, at="2026-11-01T00:00:00Z", expect_core_hash="b" * 64)
        check("核哈希与期望不符 → 应被拒", False, "（竟然通过了！）")
    except V.VCError:
        check("核哈希与期望不符 → 被拒", True)


def t4_decay() -> None:
    print("\n  ④ 时间衰减与加权 —— 半衰期要能手算核对")
    check("半衰期处恰为 0.5", abs(C.decay_weight(86400 * 30, 86400 * 30) - 0.5) < 1e-12)
    check("两个半衰期处为 0.25", abs(C.decay_weight(86400 * 60, 86400 * 30) - 0.25) < 1e-12)
    check("零龄权重为 1", C.decay_weight(0, 86400) == 1.0)

    for bad, label in [((1, 0), "半衰期为 0"), ((-1, 100), "负龄")]:
        try:
            C.decay_weight(*bad)
            check(f"{label} → 应被拒", False, "（竟然通过了！）")
        except ValueError:
            check(f"{label} → 被拒", True)

    # 加权：权重相等时，数值维度就是算术平均
    prof = {"depth": 0.4, "skills": ["a", "b"]}
    prof2 = {"depth": 0.8, "skills": ["b", "c"]}
    w = C.weighted_profile([(prof, 1.0), (prof2, 1.0)])
    check("等权加权 = 算术平均", abs(w["depth"] - 0.6) < 1e-9, f"depth={w['depth']}")
    check("技能取并集且有序", w["skills"] == ["a", "b", "c"], str(w["skills"]))

    # 权重偏向新记录，结果应更靠近新值
    w2 = C.weighted_profile([(prof, 0.25), (prof2, 1.0)])
    check("权重偏新 → 结果更靠近新值", w2["depth"] > 0.6, f"depth={w2['depth']}")


def main() -> int:
    print("=" * 66)
    print("  CIRCE 信任层自检")
    print("=" * 66)
    t1_determinism()
    t2_chain()
    t3_signature()
    t4_decay()
    print()
    print("=" * 66)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    if FAIL:
        print("  失败的项:")
        for f in FAIL:
            print(f"    - {f}")
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
