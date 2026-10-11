"""论文自查（C 档要求）：数字与实测一致、引用可核、术语未退化。

跑法:  python CIRCE/paper/check_paper.py

为什么论文需要这样一个脚本
--------------------------
C 档的要求是「每条有出处或可复现检验」。而论文最容易出的错**不是论证**，
是**数字在搬运中变了**——作者写「200 项断言」，而实际跑到第 200 项时已经变了。

**所以论文里的每个数字都该由脚本从源头重新算一遍，而不是我抄一遍。**
这与本文 §6.3 那条方法论同形：**"我抄对了"是一个未经核对的假设。**
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\myproject")
# ⚠️ 指向**当前工作稿**。初版指向的是更早那份草稿，于是它报的
# 「论文声明 200 / MCP 自检 33」全都来自那份旧文件——
# **一个检查报出了正确的失败，但它检查的不是我以为的那份文档。**
# 这与 §6 那条同形：检查的正确性依赖于「它读的是哪个文件」这个未声明的假设。
PAPER = ROOT / "CIRCE" / "paper" / "CIRCE-论文-v0.2.md"
PY = sys.executable

SUITES = [
    ("信任层", ROOT / "CIRCE/tests/selftest.py", 28),
    ("核剖面", ROOT / "CIRCE/tests/selftest_stats.py", 30),
    ("span 修正", ROOT / "CIRCE/tests/selftest_span_correction.py", 15),
    ("协议层", ROOT / "CIRCE/protocol/demo.py", 18),
    ("约定", ROOT / "CIRCE/tests/check_conventions.py", 17),
    ("MCP 自检", ROOT / "CIRCE/mcp/server.py", 66),   # 两代协议 + 真实序列重放 + 洞五拒绝路径 + 工具名可移植性
    ("MCP stdio", ROOT / "CIRCE/mcp/test_stdio_roundtrip.py", 23),  # 含无握手路径
    ("MCP 客户端", ROOT / "CIRCE/mcp/demo_mcp_client.py", 30),
    ("MCP 本地模型", ROOT / "CIRCE/mcp/test_ollama_agent.py", 23),  # 真实 LLM 自己选工具
    ("真实模型", ROOT / "CIRCE/protocol/demo_real_models.py", 13),
]

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


def run(path: Path, extra: list[str] | None = None) -> int | None:
    cmd = [PY, "-X", "utf8", str(path)] + (extra or [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", timeout=1800, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return None
    m = re.search(r"通过\s+(\d+)\s+失败\s+(\d+)", p.stdout or "")
    if not m:
        return None
    if int(m.group(2)) != 0:
        return -1
    return int(m.group(1))


def fresh_data_dir(tag: str) -> Path:
    """每次跑都用一个新的临时目录。

    ⚠️ 这一点踩过一次（2026-10-10）：初版给 MCP 自检传的是一个**固定**的 `--data` 路径，
    于是第二次运行自查时那个目录里已经有上一轮的账本，
    `server.py --selftest` 里「空账本给白纸」那条断言就失败了——
    **而报出来的是「MCP 自检有失败项」，看起来像系统的问题。**

    **那是检查脚本的状态污染，不是被测系统的问题。**
    与本文 §6 那条同形：检查的正确性依赖于一个未被声明的假设（目录是空的）。
    """
    import tempfile
    return Path(tempfile.mkdtemp(prefix=f"paper-check-{tag}-"))


def main() -> int:
    print("=" * 76)
    print("  论文自查 —— 数字 / 引用 / 术语")
    print("=" * 76)

    text = PAPER.read_text(encoding="utf-8")

    # ---- ① 断言总数：从源头跑出来，而不是抄论文里的数 ----
    print("\n  ① 断言总数（逐套跑，与论文声明的数对照）")
    total = 0
    for label, path, declared in SUITES:
        extra = (["--agent", "paper-check", "--data",
                  str(fresh_data_dir("mcp")), "--selftest"]
                 if path.name == "server.py" else None)
        if path.name == "demo_real_models.py":
            extra = ["--rounds", "3"]
        if path.name == "test_ollama_agent.py":
            # ⚠️ 它需要一个**明确指定**的本地模型：不指定会走脚本自己的默认值，
            # 而默认值将来若改了，这一套的项数就跟着变——**论文里的数字会跟着漂**。
            # 与 fresh_data_dir 那条教训同形：别把一个未声明的假设留在检查里。
            extra = ["--model", "qwen2.5:7b"]
        n = run(path, extra)
        if n is None:
            check(f"{label} 可跑", False, "输出里没有『通过 N 失败 M』")
            continue
        if n < 0:
            check(f"{label} 无失败", False, "有失败项")
            continue
        total += n
        check(f"{label} = {n}（论文声明 {declared}）", n == declared,
              "" if n == declared else f"**实际 {n}**")
    print(f"    实测合计 = {total}")
    # 论文里声明的三个数（协议版本变更后它们全变过一次，这个检查抓住了）
    for pat, label in ((r"合计\s*(\d+)\s*项断言", "status 行的合计"),
                       (r"合计\s*\*\*(\d+)\s*项", "正文的合计"),
                       (r"\*\*(\d+)\s*项断言\*\*", "摘要的断言数")):
        m = re.search(pat, text)
        if m:
            check(f"论文里{label} = {m.group(1)}",
                  int(m.group(1)) == total,
                  "" if int(m.group(1)) == total else f"**实际 {total}**")
    # 附录 A 的分节注释也要对。
    # ⚠️ 初版的规则是 `# N（...含某层名...）`——**它漏掉了不带层名的那些行**
    # （如 `# 28` 与 `# ── 合计 226`），于是附录里的旧数字没被抓到。
    # 现在改成：附录 A 代码块里的**每一个** `# <数字>` 都必须与某一套的项数相符，
    # 且 `合计 <数字>` 必须等于总数。**规则要覆盖它声称覆盖的全部，而不只是好匹配的那部分。**
    apx = text[text.find("## 附录 A"):] if "## 附录 A" in text else ""
    code = "\n".join(l for l in apx.splitlines() if l.strip().startswith("python")
                     or l.strip().startswith("#") or l.strip().startswith("python CIRCE"))
    declared_in_apx = [int(m) for m in re.findall(r"#\s*(\d+)\b", apx)]
    valid = {d for _, _, d in SUITES}
    stray = sorted({n for n in declared_in_apx if n not in valid and n != total})
    check("附录 A 里的每个数字都对应某一套的项数（或合计）", not stray,
          f"无归属的: {stray}" if stray else f"检查了 {len(declared_in_apx)} 处")
    m = re.search(r"合计\s*(\d+)", apx)
    if m:
        check(f"附录 A 的合计 = {m.group(1)}", int(m.group(1)) == total,
              "" if int(m.group(1)) == total else f"**应 {total}**")
    # 表格里的数字也要对
    for label, _, declared in SUITES:
        # 表格行形如：| 信任层 | 28 | ... |
        m = re.search(rf"\|\s*{re.escape(label)}\s*\|\s*(\d+)\s*\|", text)
        if m:
            check(f"表格里 {label} = {m.group(1)}", int(m.group(1)) == declared,
                  "" if int(m.group(1)) == declared else f"**应 {declared}**")
        else:
            check(f"表格里有 {label} 一行", False, "没找到")

    # ---- ② span 修正的关键数字 ----
    print("\n  ② §3.3 的两个数值（必须与自检的实测一致）")
    for name, want in (("原始 span", "0.22619"), ("span_by_task", "0.074074")):
        check(f"论文写了 {name} = {want}", want in text)

    # ---- ③ 引用可核：论文里每个 arXiv id 都该在元数据里有 ----
    print("\n  ③ 引用可核（论文里的 arXiv id 必须能对上元数据）")
    import json
    meta_path = ROOT / "tools" / "related_work_meta.json"
    if not meta_path.exists():
        check("元数据文件存在", False, str(meta_path))
    else:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ids = set()
        for entries in meta.values():
            for e in entries:
                ids.add(e["id"].split("/")[-1].split("v")[0])
        cited = set(re.findall(r"arXiv:(\d{4}\.\d{4,5})", text))
        check("论文引用了 arXiv 条目", len(cited) > 0, f"{len(cited)} 个")
        missing = sorted(c for c in cited if c not in ids)
        check("每个被引 arXiv id 都在抓取的元数据里", not missing,
              f"缺: {missing}" if missing else f"{len(cited)} 个全部对上")

    # ---- ④ 术语未退化（本项目自己的中文术语必须保持中文） ----
    print("\n  ④ 术语：本项目自定的中文术语不得被译成英文")
    forbidden = {
        "su-parameter": "素参",
        "sediment": "沉积",
        "meaning sovereignty": "意义权",
        "cognitive curvature": "认知曲率",
    }
    for eng, zh in forbidden.items():
        hit = re.search(re.escape(eng), text, re.I)
        check(f"未出现 {eng!r}（应保持「{zh}」）", hit is None,
              "" if hit is None else f"出现在第 {text[:hit.start()].count(chr(10))+1} 行")

    # ---- ⑤ 论文不该有的自指错误 ----
    print("\n  ⑤ 内部一致性")
    # 摘要里声明的断言数该与正文一致
    m = re.search(r"\*\*(\d+)\s*项断言\*\*", text)
    if m:
        check(f"摘要声明的 {m.group(1)} 项与实测一致",
              int(m.group(1)) == total, f"实测 {total}")
    # 章节编号连续
    heads = re.findall(r"^## ([一二三四五六七八九十]+)、", text, re.M)
    check("章节编号无重复", len(heads) == len(set(heads)),
          f"{len(heads)} 节: {heads}")

    print()
    print("=" * 76)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 76)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
