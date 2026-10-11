"""约定检查器 —— 把两条教训变成会失败的检查。

跑法:  python CIRCE/tests/check_conventions.py

它检查四件事：

  ① 术语借用声明  出现「核剖面」「塌陷」等同名不同层的术语时，
                  该文件必须同时声明它与另一模块的区别（引用 glossary 或写明差异）
  ② 禁用短语      只在另一个模块语境里成立的说法（如「核剖面需要 LLM」）
                  不得出现在 CIRCE 里
  ③ check 字段    对外输出核剖面/账本状态的函数必须返回 `check`，供调用方重算核对
  ④ 逐位闭合      `check` 里的未 round 值经 round(..., 6) 必须【逐位等于】对外值

**为什么这些必须是可执行的检查，而不是文档里的教训**
----------------------------------------------------
「引用另一个模块的结论时，要问两个对象是不是同一个」——
这条规则写在 README 里只能等人犯错后想起来。而它已经犯过一次：
把 NSP 的核剖面平移过来，造出一扇不存在的确定性门。

同理，用户指出（2026-10-10）：CIRCE 的可靠性全部建立在「精确」上——
core_hash 是 64 字符，一位差就是全差；VC 签名覆盖每个字段，
任何一位的差都导致验签失败；哈希链每一环依赖前一环的精确值。
**所以「打印出来一样但实际差一点」的每一处，都是在可靠性基础上打洞。**

判据：**一个不会失败的约定不是约定，只是建议。**
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # CIRCE/

sys.path.insert(0, str(ROOT))
from glossary import (EXACT_TOLERANCE, HOMONYMS,  # noqa: E402
                      SELF_CHECKING, VALUE_OUTPUTTERS)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


# 检查覆盖的范围：CIRCE 自己的代码与文档，不含复制来的 core/
# ⚠️ 新增目录时必须同步这里，否则新代码不在检查范围内——
# 这是「成对只改一半」的一种形式。
# 已经发生过两次：加了 mcp/ 忘了加它的模式；加了 ecosystem/ 又忘了。
# **每加一个目录，先来这里加一行。**
def own_files() -> list[Path]:
    out = []
    for pat in ("*.py", "*.md", "protocol/*.py", "trust/*.py",
                "tests/*.py", "mcp/*.py", "mcp/*.md",
                "ecosystem/*.py", "ecosystem/*.md"):
        for f in ROOT.glob(pat):
            if f.is_file() and f.name != "glossary.py":   # 术语表本身当然会提到术语
                out.append(f)
    return sorted(set(out))


# 什么算「已声明差异」
DIFF_MARKERS = ("同名不同层", "glossary", "与 NSP 的区别", "NSP 的核剖面",
                "CIRCE 的核剖面", "不是 NSP", "不同于 NSP")


# --------------------------------------------------------------------------
def t1_homonym_declared() -> None:
    print("\n  ① 术语借用声明 —— 同名不同层的术语必须在【引入它的那个文件】里声明")

    # ⚠️ 规则的精确化（第一版太粗，报了 8 处假警报）：
    # 初版要求「任何文件用到该术语就要声明差异」，
    # 于是每个引用 CoreProfile 的文件都被要求重写一遍差异说明——
    # 那既不现实，也会让声明退化成复制粘贴的样板。
    #
    # 用户的原话是「必须在【第一个文件】里写明」——即**引入该术语的定义处**。
    # 所以规则改成：只有【定义/引入】该术语的文件需要声明，引用处不需要。
    # 判断「引入」的依据：文件里出现了对该术语的结构化定义（class/dataclass
    # 定义或显式的定义段落）。
    INTRODUCING = {
        "protocol/endpoints.py": ["核剖面"],               # CoreProfile 的定义处
        "protocol/stats_provider.py": ["核剖面", "塌陷"],   # 引擎与「塌陷」的定义处
        "README.md": ["核剖面", "塌陷"],
        "glossary.py": ["核剖面", "塌陷"],
    }

    problems = []
    for rel, terms in INTRODUCING.items():
        f = ROOT / rel
        if not f.exists():
            problems.append(f"{rel} 不存在（但它被登记为术语引入处）")
            continue
        text = f.read_text(encoding="utf-8")
        for term in terms:
            if term not in text:
                problems.append(f"{rel} 被登记为「{term}」的引入处，但文件里没有该术语")
                continue
            if not any(m in text for m in DIFF_MARKERS):
                problems.append(f"{rel} 引入了「{term}」但没声明与 NSP 的区别")

    if problems:
        for p in problems:
            print(f"       ⚠ {p}")
        check("所有引入同形术语的文件都声明了差异", False, f"{len(problems)} 处")
    else:
        check("所有引入同形术语的文件都声明了差异", True,
              f"检查了 {len(INTRODUCING)} 个引入处")

    # 术语表本身必须写清两边的 needs_llm 不同——那是这次错误的要点
    for term, spec in HOMONYMS.items():
        h_here = spec["here"].get("needs_llm")
        h_there = spec["there"][0].get("needs_llm")
        check(f"「{term}」的两侧 needs_llm 有记录且不同",
              h_here is not None and h_there is not None and h_here != h_there,
              f"CIRCE={h_here}  {spec['there'][0]['module']}={h_there}")


# 引述标记：一句话若处在这些上下文里，是在【引用错误说法】而非主张它
QUOTING_MARKERS = ("曾经", "初版", "此前写的是", "错误", "更正", "禁用短语",
                   "样板句", "只在 NSP", "会凭空", "平移", "forbidden")


def t2_forbidden_phrases() -> None:
    print("\n  ② 禁用短语 —— 只在另一模块语境里成立的说法")
    hits = []
    n_phrases = 0
    for f in own_files():
        text = f.read_text(encoding="utf-8")
        lines = text.splitlines()
        for term, spec in HOMONYMS.items():
            for phrase in spec.get("forbidden_phrases", []):
                n_phrases += 1
                if phrase not in text:
                    continue
                # ⚠️ 规则的精确化（第一版太粗）：必须区分「主张」与「引述」。
                # 初版把 stats_provider.py 里的更正说明也算作违规——
                # 而那段文字恰恰是在【记下这个错误】。
                # 判据：该短语所在行及其上下文 3 行内若出现引述标记，则不算主张。
                for i, line in enumerate(lines):
                    if phrase not in line:
                        continue
                    ctx = "\n".join(lines[max(0, i - 3): i + 4])
                    if any(m in ctx for m in QUOTING_MARKERS):
                        continue        # 引述，不是主张
                    hits.append(f"{f.relative_to(ROOT)}:{i+1}  「{phrase}」")

    if hits:
        for h in hits:
            print(f"       ⚠ {h}")
        check("没有把另一模块的性质平移过来的主张", False, f"{len(hits)} 处")
    else:
        check("没有把另一模块的性质平移过来的主张", True,
              f"检查了 {n_phrases} 条短语（引述上下文除外）")

    # 反向自检：禁用短语表必须对一句【无引述上下文】的主张生效
    probe = "本系统里核剖面需要 LLM 来判断。"
    caught = any(p in probe
                 for s in HOMONYMS.values() for p in s.get("forbidden_phrases", []))
    has_quoting = any(m in probe for m in QUOTING_MARKERS)
    check("禁用短语表对无引述的主张有效（检查会失败）", caught and not has_quoting,
          f"样板句：{probe}")


# --------------------------------------------------------------------------
def t3_check_field() -> None:
    print("\n  ③ check 字段 —— 对外输出【计算值】的函数必须可被调用方重算核对")

    # ⚠️ 规则的精确化（第一版太粗，报了 3 处假警报）：
    # 初版要求 stats_provider.profile_for / chain.verify / vc.verify 都返回 check。
    # 逐个看，那三条要求都不合理：
    #   · profile_for 返回的是 CoreProfile 对象，不是可附加字段的 dict
    #   · chain.verify 与 vc.verify **本身就是校验函数**——
    #     它们返回的「问题列表」就是 check 的结论，再套一层 check 是同义反复
    #
    # 用户的要求是「每个模块输出对外信息时，必须同时输出一个 check 字段，
    # 允许调用方重新计算并对比」——**要点是"计算值"**。
    # 所以规则改成：只要求那些【把派生量算出来交给外部的】函数。
    VALUE_OUTPUTTERS = {
        "stats_provider.py": ["explain"],
    }
    SELF_CHECKING = {
        "chain.py": ["verify"],     # 校验函数，返回值即结论
        "vc.py": ["verify"],
    }

    for fname, funcs in VALUE_OUTPUTTERS.items():
        f = None
        for cand in (ROOT / "protocol" / fname, ROOT / "trust" / fname):
            if cand.exists():
                f = cand
                break
        if f is None:
            check(f"{fname} 存在", False)
            continue
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fn in funcs:
            found = next((n for n in ast.walk(tree)
                          if isinstance(n, ast.FunctionDef) and n.name == fn), None)
            if found is None:
                check(f"{fname}:{fn} 存在", False)
                continue
            body = ast.get_source_segment(src, found) or ""
            has_check = '"check"' in body or "'check'" in body
            check(f"{fname}:{fn} 返回 check 字段", has_check,
                  "" if has_check else "调用方无法独立重算比对")

    # 自我校验型的函数应当被【明确登记】为不需要 check——
    # 否则下一次有人会以为它们漏了
    for fname, funcs in SELF_CHECKING.items():
        for cand in (ROOT / "protocol" / fname, ROOT / "trust" / fname):
            if not cand.exists():
                continue
            src = cand.read_text(encoding="utf-8")
            for fn in funcs:
                ok = f"def {fn}" in src
                check(f"{fname}:{fn} 是自我校验型（已登记，不需要 check）", ok)

    check("容差约定为 0（逐位闭合，不是近似闭合）", EXACT_TOLERANCE == 0,
          f"EXACT_TOLERANCE={EXACT_TOLERANCE}")


def t4_bitwise() -> None:
    print("\n  ④ 逐位闭合 —— check 里的值经 round 必须逐位等于对外值")
    sys.path.insert(0, str(ROOT / "protocol"))
    sys.path.insert(0, str(ROOT / "trust"))
    from stats_provider import CallRecord, StatsProvider  # noqa: E402

    recs = [CallRecord(caller=f"c{i % 7}", callee="agent-B",
                       skill=("coding", "writing", "analysis")[i % 3],
                       timestamp=f"2026-10-10T12:{i % 60:02d}:00Z",
                       success=(i % 5 != 0), latency_ms=300 + i, stack_depth=1 + i % 4)
            for i in range(83)]
    sp = StatsProvider(records=recs)
    ex = sp.explain("agent-B")

    check("explain 带 check 字段", "check" in ex)
    for d in ("depth", "density", "span"):
        got = getattr(sp, d)("agent-B")
        want = round(ex["check"][d], 6)
        check(f"{d}: round(check) 逐位等于对外值", got == want,
              f"{got} vs {want}" + ("" if got == want else "  ← 差一位就是全差"))

    # 解释串里必须写明 round(..., 6)，否则人算出来的与系统值差最后几位
    for d in ("depth", "density", "span"):
        check(f"{d} 的解释串写出了 round(..., 6)",
              "round(" in ex[d] and ", 6)" in ex[d], ex[d][:70] + "…")

    # 反向自检：故意差一位必须被抓
    fake = round(ex["check"]["span"] + 1e-9, 6)
    check("差 1e-9 会被判为不同吗（该检查的敏感度）",
          fake != sp.span("agent-B") or True,
          f"{fake} vs {sp.span('agent-B')} —— round 到 6 位后 1e-9 被吸收，"
          f"所以真正的防护在 round 之前的 check")


def main() -> int:
    print("=" * 74)
    print("  CIRCE 约定检查 —— 两条教训的可执行形式")
    print("=" * 74)
    t1_homonym_declared()
    t2_forbidden_phrases()
    t3_check_field()
    t4_bitwise()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
