"""CIRCE 信任层 —— 标准化 / 哈希链 / 仅追加日志。

设计原文（`CIRCE/design/情绪树-CIRCE原文.md`，消息 236）：

    标准化：将核剖面（深度/密度/跨度）规范化序列化（如 JCS 规范化 JSON）
    哈希：  对序列化后的数据计算 SHA-256，生成唯一指纹
    哈希链：H_t = Hash(H_{t-1} ‖ CoreProfile_t ‖ Timestamp)
            「任何人想篡改历史，必须重算整条链」

**这个模块只处理确定性输入。** CIRCE 的第一个设计关口——核剖面本身怎么算才确定——
尚未裁决（见 `CIRCE/README.md` §五），所以这里**不实现核剖面的计算**，
只实现「给定一个已确定的核剖面，如何把它锚定成不可篡改的历史」。

边界
----
    输入:  任何可 JSON 序列化的核剖面（dict / list / 标量）
    输出:  规范化字节 → SHA-256 → 链式哈希 → 仅追加日志
    不做:  核剖面怎么算、该不该信、技能条件化 R(i|k)

为什么不用 pickle / repr
-----------------------
两者都依赖运行时的对象身份或内存布局，同一个 dict 在不同进程可能得到不同字节。
JCS（RFC 8785 的核心思想：键按 UTF-16 码元排序、无多余空白、数字用最短往返表示）
让「同样的内容」在任何机器上得到「同样的字节」——**这是哈希可锚定的前提**。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

GENESIS = "0" * 64          # 链首的前驱哈希：64 个 0


# --------------------------------------------------------------------------
# ① 标准化
# --------------------------------------------------------------------------
def normalize(obj: Any) -> bytes:
    """JCS 风格的规范化序列化：键排序、无空白、UTF-8。

    与原文「键按字母排序，无空格」一致。ensure_ascii=False 是要点——
    中文技能名（如「写作」）必须按 UTF-8 原字节进哈希，
    转成 \\uXXXX 会让同一份内容在不同设置下得到不同哈希。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,        # NaN/Infinity 不是合法 JSON，且哈希不稳定
    ).encode("utf-8")


def core_hash(obj: Any) -> str:
    """核剖面的指纹：SHA-256(规范化字节)。"""
    return hashlib.sha256(normalize(obj)).hexdigest()


# --------------------------------------------------------------------------
# ② 哈希链
# --------------------------------------------------------------------------
def link(prev_hash: str, profile: Any, timestamp: str) -> str:
    """H_t = Hash(H_{t-1} ‖ CoreProfile_t ‖ Timestamp)。

    三段的拼接方式必须无歧义：直接用分隔符会与内容里出现的同一字符混淆，
    所以每段先带上长度前缀（length-prefixed framing）。
    """
    parts = [prev_hash, core_hash(profile), timestamp]
    framed = b"".join(
        len(p.encode("utf-8")).to_bytes(8, "big") + p.encode("utf-8")
        for p in parts
    )
    return hashlib.sha256(framed).hexdigest()


# --------------------------------------------------------------------------
# ③ 仅追加日志
# --------------------------------------------------------------------------
@dataclass
class Entry:
    seq: int
    timestamp: str
    profile: Any
    profile_hash: str
    prev_hash: str
    hash: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "profile": self.profile,
            "profile_hash": self.profile_hash,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "note": self.note,
        }

    @staticmethod
    def from_dict(d: dict) -> "Entry":
        return Entry(
            seq=d["seq"], timestamp=d["timestamp"], profile=d["profile"],
            profile_hash=d["profile_hash"], prev_hash=d["prev_hash"],
            hash=d["hash"], note=d.get("note", ""),
        )


@dataclass
class Ledger:
    """仅追加账本。

    「仅追加」在这里是**结构上**保证的：没有删除或修改单条记录的方法。
    唯一的写入口是 `append`，它必须接在当前链尾之后。
    """
    path: Path | None = None
    entries: list[Entry] = field(default_factory=list)

    # ---- 读 ----
    @classmethod
    def load(cls, path: Path) -> "Ledger":
        if not path.exists():
            return cls(path=path, entries=[])
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, entries=[Entry.from_dict(d) for d in raw["entries"]])

    @property
    def head(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    # ---- 写 ----
    def append(self, profile: Any, timestamp: str, note: str = "") -> Entry:
        """追加一条。timestamp 必须是 ISO-8601 字符串——由调用方给定，
        不由本模块取当前时间，因为「同一份输入得到同一条链」要求时间是入参。"""
        e = Entry(
            seq=len(self.entries) + 1,
            timestamp=timestamp,
            profile=profile,
            profile_hash=core_hash(profile),
            prev_hash=self.head,
            hash=link(self.head, profile, timestamp),
            note=note,
        )
        self.entries.append(e)
        self._flush()
        return e

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"entries": [e.to_dict() for e in self.entries]},
                       ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8", newline="\n")

    # ---- 校验 ----
    def verify(self) -> list[str]:
        """重算整条链，返回问题列表（空 = 完整）。

        这是「篡改历史必须重算整条链」那句话的可执行形式：
        改动第 k 条会让第 k 条的 hash 与第 k+1 条的 prev_hash 双双对不上。
        """
        problems: list[str] = []
        prev = GENESIS
        for i, e in enumerate(self.entries, 1):
            if e.seq != i:
                problems.append(f"第 {i} 条: seq={e.seq} 不连续")
            if e.prev_hash != prev:
                problems.append(
                    f"第 {i} 条: prev_hash={e.prev_hash[:12]}… "
                    f"但前一条的 hash={prev[:12]}… —— 链在此断开")
            recomputed = link(e.prev_hash, e.profile, e.timestamp)
            if e.hash != recomputed:
                problems.append(
                    f"第 {i} 条: hash 与内容不符 "
                    f"(记录 {e.hash[:12]}…, 重算 {recomputed[:12]}…) —— 内容被改过")
            ph = core_hash(e.profile)
            if e.profile_hash != ph:
                problems.append(
                    f"第 {i} 条: profile_hash 与 profile 不符 "
                    f"(记录 {e.profile_hash[:12]}…, 重算 {ph[:12]}…)")
            prev = recomputed
        return problems


# --------------------------------------------------------------------------
# ④ 时间衰减
# --------------------------------------------------------------------------
def decay_weight(age_seconds: float, half_life_seconds: float) -> float:
    """时间衰减因子：最近的调用权重更高（原文 §五 第 3 步）。

    半衰期形式，而不是 exp(-t/τ)，因为半衰期可以直接回答
    「多久之后这条记录只剩一半权重」——那是能被人检查的量。
    """
    if half_life_seconds <= 0:
        raise ValueError("half_life_seconds 必须为正")
    if age_seconds < 0:
        raise ValueError("age_seconds 不能为负（未来时间戳意味着时钟有问题）")
    return 0.5 ** (age_seconds / half_life_seconds)


def weighted_profile(entries: Iterable[tuple[Any, float]]) -> dict:
    """把若干 (核剖面, 权重) 折成一个加权剖面。

    只对数值维度做加权平均；非数值维度（如 skills 列表）按权重选并集，
    因为「技能是否出现」不是可平均的量。
    """
    pairs = list(entries)
    total = sum(w for _, w in pairs)
    if total <= 0:
        raise ValueError("权重之和必须为正")

    acc: dict[str, float] = {}
    seen_skills: dict[str, float] = {}
    for prof, w in pairs:
        if not isinstance(prof, dict):
            raise TypeError(f"核剖面必须是 dict，收到 {type(prof).__name__}")
        for k, v in prof.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                acc[k] = acc.get(k, 0.0) + v * w
            elif k == "skills" and isinstance(v, list):
                for s in v:
                    seen_skills[s] = max(seen_skills.get(s, 0.0), w)
            # 其余非数值维度不进加权结果（它们是身份，不是量）
    out: dict[str, Any] = {k: round(v / total, 6) for k, v in sorted(acc.items())}
    if seen_skills:
        out["skills"] = sorted(seen_skills)
    return out
