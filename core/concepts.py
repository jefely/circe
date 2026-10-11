"""Concept extraction for the NSP framework.

THE ONE DESIGN CONSTRAINT
-------------------------
NSP's D2 works by REMOVING a unit and asking whether the system collapses. So whatever counts
as a "unit" has to be something a reader would agree is a thing — otherwise the collapse test
measures the extractor, not the system.

That rules out naive n-gram mining. A survey of this corpus showed the bold phrases are split
roughly evenly between real terms (意义技术, 匿名前反思自身意识, 非对称条件概率, 负空间) and
structural template words (版本, 建议, 定义, 状态, 结论). Feeding the second group into a
delete test would produce "cores" that are just formatting.

So concepts come from three explicit sources, and each is tagged with where it came from:
  register  — toolkit/philosophy/concepts.json (the project's own term ledger)
  paper     — maths/engineering terms carried by the papers
  nsp       — terms introduced by the NSP turn itself
A concept is only used if it actually occurs in the system under test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REGISTER = Path(r"D:\myproject\toolkit\philosophy\concepts.json")

# Structural words that appear bold in the corpus but name no concept. Kept as an explicit
# list rather than a length/frequency heuristic, because a heuristic would silently change
# what the delete test is measuring.
TEMPLATE_WORDS = {
    "版本", "建议", "定义", "状态", "结论", "分析用户输入", "中文全称", "英文全称", "关键",
    "核心变更", "适用", "协议", "深度", "还原", "标识意义", "哲学层", "数学层", "工程层",
    "不能", "函数描述", "必然", "摘要", "要点", "问题", "目标", "方案", "方法", "示例",
    "注意", "小结", "步骤", "说明", "输出", "输入", "结果", "原因", "证据", "依据",
}

# Terms the papers carry that the philosophy register deliberately does not hold.
PAPER_TERMS = {
    "认知沉积总量": ["C_T", "认知沉积总量", "沉积总量"],
    "认知曲率": ["认知曲率", "Fisher信息矩阵"],
    "路径测度": ["路径测度", "全变差测度"],
    "勒贝格分解": ["勒贝格分解", "Lebesgue"],
    "Girsanov变换": ["Girsanov"],
    "情绪树": ["情绪树", "层级化情绪树", "情绪轮"],
    "Clifford代数": ["Clifford", "clifford", "多重向量"],
    "Keci": ["Keci", "dicee", "dice-embeddings"],
    "压抑势能": ["压抑势能"],
    "墓碑制积分": ["墓碑制积分", "墓碑"],
    "六层防护": ["六层密码学防护", "六层防护", "L1–L6", "L1-L6"],
    "失活": ["失活"],
    "非对称条件概率": ["非对称条件概率", "非對稱條件概率"],
    "分布体量": ["Vol_dist", "分布体量"],
}

# Terms introduced by the NSP turn (2026-10-09). These did not exist in the corpus before it
# and are the framework's own vocabulary.
NSP_TERMS = {
    "负空间协议": ["NSP", "负空间协议", "负空间"],
    "正空间": ["正空间"],
    "剖面空间": ["剖面空间", "剖面"],
    "主体性空间": ["主体性空间", "主体性"],
    "存在意义权": ["存在意义权"],
    "删除测试": ["删除测试", "剥离验证", "剥离"],
    "塌陷": ["塌陷"],
    "不可删核": ["不可删核", "素核", "完美核", "核剥离"],
    "结构伦理": ["结构伦理"],
    "跨基质同构": ["跨基质同构", "同构"],
}


def load_register() -> dict[str, list[str]]:
    """Terms from the project ledger. Deprecated ones are excluded — they exist only as foils."""
    out: dict[str, list[str]] = {}
    if not REGISTER.exists():
        return out
    doc = json.loads(REGISTER.read_text(encoding="utf-8"))
    for t in doc.get("terms", []):
        if t.get("status") == "deprecated":
            continue
        zh = t.get("zh")
        if zh:
            out[zh] = [zh]
    return out


def build_catalog() -> dict[str, dict]:
    """{zh: {aliases, source}} — every candidate concept, tagged with where it came from."""
    cat: dict[str, dict] = {}
    for zh, alts in load_register().items():
        cat[zh] = {"aliases": alts, "source": "register"}
    for zh, alts in PAPER_TERMS.items():
        cat.setdefault(zh, {"aliases": alts, "source": "paper"})
    for zh, alts in NSP_TERMS.items():
        cat.setdefault(zh, {"aliases": alts, "source": "nsp"})
    return cat


def extract_bold(text: str) -> set[str]:
    """Bold Chinese phrases as CANDIDATES, with template words removed.

    Candidates are reported separately and never enter the system on their own — they are for
    seeing what the catalog missed, not for silently widening the unit set.
    """
    found = set()
    for m in re.finditer(r"\*\*([^*\n]{2,20})\*\*", text):
        s = m.group(1).strip()
        if not re.search(r"[\u4e00-\u9fff]", s):
            continue
        if s in TEMPLATE_WORDS:
            continue
        if len(s) < 2:
            continue
        found.add(s)
    return found


def occurrences(text: str, catalog: dict[str, dict]) -> set[str]:
    """Which catalog concepts occur in this text."""
    hit = set()
    for zh, meta in catalog.items():
        if any(a in text for a in meta["aliases"]):
            hit.add(zh)
    return hit
