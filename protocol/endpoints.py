"""CIRCE 协议接口 —— 三个端点。

⚠️ 术语声明：**本模块的「核剖面」不是 NSP 的「核剖面」**（同名不同层）
--------------------------------------------------------------------
见 `CIRCE/glossary.py`。两边最容易混淆的一点：

    CIRCE 的核剖面：对一个 agent 的【调用历史】做统计（深度/密度/跨度）。
                    **不需要 LLM**，输入是 CallRecord 序列。
    NSP   的核剖面：对一份【文本 / 概念图】做删除测试。
                    **需要 LLM** 判断「删掉这个概念，文本结构会不会塌陷」。

**「需要 LLM 判断」是 NSP 那边的性质。** 曾经有人（就是我）把这句话平移过来，
凭空造出一个「核剖面不确定 → core_hash 不稳定 → 哈希链会断」的问题，
并为它设计了三套本不需要的方案。见 `CIRCE/README.md` §五 的更正一节。

**所以本模块里的 CoreProfile 是可确定性计算的统计量**，
它的值只依赖调用日志，不依赖任何模型。

---

设计原文（`CIRCE/design/情绪树-CIRCE原文.md`，消息 232 §四）：

    CIRCE 协议接口
      · Readiness Probe   (就绪探测)
      · Core Profile Query (核剖面查询)
      · Trust Update       (信任更新)

**为什么这一层不依赖那个未裁决的关口**
--------------------------------------
CIRCE 的第一个设计关口是「核剖面的值怎么算才确定」（甲/乙/丙三条出路，见 README §五）。
但三条出路只影响 `CoreProfile` 的**值从哪来**，不影响它的**形状与接口**——
原文已定形状是三维 `{depth, density, span}`，而协议关心的是「怎么问、怎么答、怎么签」。

所以本模块把 **`CoreProfileProvider` 抽成接口**：
    谁来提供剖面，由调用方注入。
    - 甲：一个锁定模型+温度0的确定性探测器
    - 乙：一个返回「判断所依据的完整输入」的提供者
    - 丙：一个只给可确定计算的提供者
**协议不选边。** 裁决之后写一个实现类即可，协议不动。

三个端点的语义差别（这是它们不能合并的原因）
--------------------------------------------
  Readiness Probe     「你现在能接活吗」—— 一次便宜的探活，**不必带核剖面**
  Core Profile Query  「删掉你，我会失去什么」—— 取核剖面 + 凭据，**这是 CIRCE 的核心问题**
  Trust Update        「我刚调用过你，结果是这样」—— 有状态写入，**调用方驱动**

Readiness 与 Core Profile Query 必须分开，因为探活要便宜、要能高频；
把它做成「顺便返回核剖面」会让每次探活都背上签名与账本写入的代价。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
PROFILE_DIMS = ("depth", "density", "span")


@dataclass
class CoreProfile:
    """核剖面。形状由原文定：深度 / 密度 / 跨度。

    三者都是 [0,1] 的强度量，不是计数——计数会随交互次数无界增长，
    而「不可替代性」必须在有界区间上可比。
    """
    depth: float
    density: float
    span: float
    agent_id: str = ""
    skills: tuple[str, ...] = ()
    call_count: int = 0
    last_updated: str = ""

    def __post_init__(self) -> None:
        for d in PROFILE_DIMS:
            v = getattr(self, d)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise TypeError(f"{d} 必须是数值，收到 {type(v).__name__}")
            if not (0.0 <= float(v) <= 1.0):
                raise ValueError(f"{d}={v} 超出 [0,1]——不可替代性必须在有界区间上可比")

    def to_dict(self) -> dict:
        d: dict[str, Any] = {k: round(float(getattr(self, k)), 6) for k in PROFILE_DIMS}
        if self.agent_id:
            d["agent_id"] = self.agent_id
        if self.skills:
            d["skills"] = sorted(self.skills)
        if self.call_count:
            d["call_count"] = self.call_count
        if self.last_updated:
            d["last_updated"] = self.last_updated
        return d

    @staticmethod
    def from_dict(d: dict) -> "CoreProfile":
        return CoreProfile(
            depth=d["depth"], density=d["density"], span=d["span"],
            agent_id=d.get("agent_id", ""),
            skills=tuple(d.get("skills", ())),
            call_count=d.get("call_count", 0),
            last_updated=d.get("last_updated", ""),
        )


# --------------------------------------------------------------------------
# 可注入的提供者（协议不选边）
# --------------------------------------------------------------------------
@runtime_checkable
class CoreProfileProvider(Protocol):
    """谁来提供核剖面，由裁决决定。协议只依赖这个签名。"""

    def profile_for(self, agent_id: str,
                    skill: str | None = None) -> CoreProfile:
        """返回该 agent（在给定技能维度上）的核剖面。

        skill=None 表示综合剖面；给定 skill 时是技能条件化的 R(i|k)。
        """
        ...


class StaticProvider:
    """固定剖面 —— 用于测试与演示，不是生产实现。

    它存在的意义是让协议层**可以被验证**，而不必等裁决。
    生产实现必须替换它（并在此处说明它凭什么确定）。
    """

    def __init__(self, profiles: dict[str, CoreProfile]) -> None:
        self._p = profiles

    def profile_for(self, agent_id: str, skill: str | None = None) -> CoreProfile:
        if agent_id not in self._p:
            raise KeyError(f"没有 {agent_id!r} 的剖面")
        return self._p[agent_id]


# --------------------------------------------------------------------------
# 三个端点的请求/响应（纯数据，便于序列化与跨语言）
# --------------------------------------------------------------------------
@dataclass
class ReadinessRequest:
    caller: str
    skill: str | None = None


@dataclass
class ReadinessResponse:
    agent_id: str
    ready: bool
    # 探活刻意【不】返回核剖面：那会让高频探活背上签名与账本写入的代价
    note: str = ""


@dataclass
class QueryRequest:
    caller: str
    target: str
    skill: str | None = None
    want_credential: bool = True


@dataclass
class QueryResponse:
    profile: dict
    credential: dict | None          # 核剖面的 VC（可能为 None，若目标未签发）
    ledger_head: str                 # 链尾锚定 —— 防止「重算整条链」式伪造
    ledger_seq: int
    note: str = ""


@dataclass
class UpdateRequest:
    caller: str
    target: str
    skill: str
    success: bool
    latency_ms: int
    timestamp: str
    note: str = ""
    # 本次调用发生在调用栈第几层。**必须由调用方给**——
    # 只有 caller 知道自己在哪一层发起这次调用，被调用方看不到。
    # 初版漏了这个字段，于是服务端重算 depth 时只能填默认值（见 server.py 的注释）。
    stack_depth: int = 1


@dataclass
class UpdateResponse:
    accepted: bool
    ledger_seq: int                  # 追加后的序号
    ledger_head: str
    new_profile: dict | None         # 重算后的剖面（若实现选择返回）
    note: str = ""


# --------------------------------------------------------------------------
# 端点常量（避免字符串散落）
# --------------------------------------------------------------------------
PROBE_PATH = "/circe/readiness"
QUERY_PATH = "/circe/core-profile"
UPDATE_PATH = "/circe/trust-update"
