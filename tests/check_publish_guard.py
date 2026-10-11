"""发布前的禁词扫描：**证明**那些内容一个都不在将要提交的文件里。

跑法:  python CIRCE/tests/check_publish_guard.py CIRCE

为什么要有它，而不是"我检查过了"
--------------------------------
脱敏是**手工**做的，而手工就会漏。C 档的要求是「每条有出处或可复现检验」——
所以「禁词不在仓库里」这句话必须由一次**可重复的扫描**给出，而不是由我的信心给出。

它与本项目的门禁同一条理由：**一个不会失败的门禁只是装饰。**
（`tools/check_gate_selftest.py` 存在的理由就是证明门禁会失败；
这里同理——`--selftest` 会注入一个禁词，确认扫描器真的抓得到。）

用法:
    python CIRCE/tests/check_publish_guard.py CIRCE              # 正常扫描，退出码 0/1
    python CIRCE/tests/check_publish_guard.py CIRCE --selftest   # 自检：注入禁词，必须被抓到
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 禁词表：**每一类都要能说清为什么禁**
FORBIDDEN: list[tuple[str, str]] = [
    (r"yuanhuanfeng", "个人邮箱的用户名部分"),
    (r"gmail\.com", "个人邮箱域名"),
    # ⚠️ 只查**公网**地址：127.0.0.1 是回环、10./192.168./172.16-31. 是私网，
    # 它们出现在本机配置里是正常的，不是可识别信息。
    # 第一版写成「任意四段数字」，于是误报 11 处——**规则比它要防的东西宽，就等于噪声。**
    (r"\b(?!127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
     r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "公网 IP 地址"),
    (r"b877781b", "账号 uuid 片段"),
    (r"[Cc]lash", "代理软件品牌名"),
    (r"7897", "本机代理端口"),
    (r"中国大陆", "地区陈述"),
    (r"不受支持的地区", "地区政策陈述"),
    (r"支持地区", "地区政策陈述"),
    (r"OVH", "代理出口 ISP"),
    (r"翻墙|科学上网", "翻墙类表述"),
    (r"代理出口", "代理出口表述"),
]

# 扫描时跳过的目录/文件（与 .gitignore 一致）
SKIP_DIRS = {".git", "__pycache__", ".private", "data", "claude-导出"}
SKIP_EXT = {".pdf", ".zip", ".pyc"}
# 扫描器自己当然含有禁词（它就是那张表），跳过自己
SKIP_FILES = {"check_publish_guard.py"}


def iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_EXT:
            continue
        if p.name in SKIP_FILES:
            continue
        yield p


def scan(root: Path) -> list[tuple[str, int, str, str]]:
    hits = []
    for p in iter_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat, why in FORBIDDEN:
                if re.search(pat, line):
                    hits.append((str(p.relative_to(root)), i, why, line.strip()[:110]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--selftest", action="store_true",
                    help="先注入一个禁词，确认扫描器抓得到（门禁自检）")
    args = ap.parse_args()
    root = args.root
    if not root.is_dir():
        print(f"✗ 不是目录: {root}")
        return 2

    if args.selftest:
        bait = root / "_scan_selftest.md"
        bait.write_text("这是一行注入的禁词：Clash 代理与 yuanhuanfeng\n",
                        encoding="utf-8")
        try:
            got = scan(root)
        finally:
            bait.unlink(missing_ok=True)
        caught = any("_scan_selftest" in h[0] for h in got)
        print(f"  门禁自检：注入禁词后扫描 {'✓ 抓到了' if caught else '✗ 没抓到'}")
        if not caught:
            print("  ✗ 扫描器无效——不要相信它的结果")
            return 1
        print("  （已还原：诱饵文件已删除）")
        print("  扫描器有效 ✓")
        return 0

    files = list(iter_files(root))
    hits = scan(root)
    print("=" * 72)
    print(f"  发布前禁词扫描：{root}")
    print("=" * 72)
    print(f"  检查了 {len(files)} 个文件，{len(FORBIDDEN)} 条禁词规则")
    if not hits:
        print("\n  ✓ 零命中 —— 这些内容一个都不在将要提交的文件里")
        print("=" * 72)
        return 0
    print(f"\n  ✗ {len(hits)} 处命中：")
    for f, ln, why, text in hits:
        print(f"    {f}:{ln}  [{why}]")
        print(f"        {text}")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
