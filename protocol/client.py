"""CIRCE 客户端 —— 调用方一侧，含「核探测后再决定是否调用」的判据。

设计原文（`CIRCE/design/情绪树-CIRCE原文.md`，消息 232 §八）：

    「它不告诉 AI『你是否可信』，它告诉 AI『删掉你，我会失去什么』。」

所以客户端的核心方法不是 `is_trustworthy()`，而是 `irreplaceability()`：
**问的是「删掉它我损失多少」，然后由调用方自己的策略决定要不要调。**

这一区分不是措辞游戏，它决定了接口形状：
    可信度是目标的属性 → 调用方只能接受/拒绝
    不可替代性是**关系的**属性 → 同一个目标对不同调用方可以有不同的值
                                    （所以 QueryRequest 必须带 caller）
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# 让本模块能被直接跑（`python client.py`）——与 server.py 同样的理由。
# 初版给 server.py 加了、漏了 client.py，于是「每个模块自包含」这条
# 只在服务端成立。**成对的东西要一起改**，这条漏了两次同类错误（vc.py 那次是另一次）。
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import PROBE_PATH, QUERY_PATH, UPDATE_PATH, CoreProfile  # noqa: E402


@dataclass
class PeerInfo:
    agent_id: str
    profile: dict
    credential: dict | None
    ledger_head: str
    ledger_seq: int
    verified: bool
    verify_note: str = ""


class CirceClient:
    """对某个 peer 的 CIRCE 客户端。"""

    def __init__(self, caller: str, base_url: str,
                 peer_public_key=None) -> None:
        self.caller = caller
        self.base = base_url.rstrip("/")
        self.peer_pub = peer_public_key

    # ---- 内部 ----
    def _get(self, path: str, timeout: float = 5.0) -> dict:
        req = urllib.request.Request(self.base + path,
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path: str, body: dict, timeout: float = 5.0) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 端点一：探活（便宜，不带核剖面）----
    def probe(self) -> dict:
        return self._get(PROBE_PATH)

    # ---- 端点二：核剖面查询（CIRCE 的核心问题）----
    def query_profile(self, skill: str | None = None,
                      want_credential: bool = True) -> PeerInfo:
        q = f"{QUERY_PATH}?caller={self.caller}"
        if skill:
            q += f"&skill={skill}"
        if not want_credential:
            q += "&want_credential=0"
        try:
            data = self._get(q)
        except urllib.error.HTTPError as e:
            # 服务方拒签（账本不可信）时返回 409。
            # **这必须与「服务方挂了」区分开**——前者是「它在，但它的话不可信」，
            # 后者是「它不在」。把两者混为一谈，会让一个被篡改的 peer
            # 与一个下线的 peer 得到同样的处理。
            if e.code == 409:
                detail = ""
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("detail", "")
                except Exception:
                    pass
                return PeerInfo(agent_id="", profile={}, credential=None,
                                ledger_head="", ledger_seq=0,
                                verified=False,
                                verify_note=f"服务方拒签（409）：账本不可信。{detail}")
            raise

        verified, note = False, "未提供公钥，跳过验签"
        if self.peer_pub is not None and data.get("credential"):
            try:
                from trust import vc as VC  # type: ignore
                subj = VC.verify(data["credential"], self.peer_pub)
                # 凭据里的核哈希必须与刚取回的剖面一致，否则「凭据是旧的/别人的」
                from trust.chain import core_hash  # type: ignore
                if subj["coreHash"] != core_hash(data["profile"]):
                    verified, note = False, "凭据里的核哈希与返回的剖面不符"
                elif subj.get("ledgerHead") not in (None, data.get("ledger_head")):
                    verified, note = False, (
                        "凭据锚定的链尾与当前链尾不符 —— 账本被重算过，"
                        "或返回的凭证早于当前状态")
                else:
                    verified, note = True, "签名有效，核哈希与链尾均吻合"
            except Exception as e:
                verified, note = False, f"验签失败: {e}"

        return PeerInfo(
            agent_id=data.get("profile", {}).get("agent_id", ""),
            profile=data["profile"], credential=data.get("credential"),
            ledger_head=data.get("ledger_head", ""),
            ledger_seq=data.get("ledger_seq", 0),
            verified=verified, verify_note=note)

    # ---- 端点三：信任更新（调用之后）----
    def report_call(self, target: str, skill: str, success: bool,
                    latency_ms: int, timestamp: str, note: str = "",
                    stack_depth: int = 1) -> dict:
        """把一次真实调用写进对方的账本。

        `stack_depth` **必须由调用方给**：只有 caller 知道自己在哪一层发起调用，
        被调用方看不到（那是 caller 的上下文，不是 callee 的状态）。
        它也是核剖面里 `depth` 维度的唯一来源。
        """
        return self._post(UPDATE_PATH, {
            "caller": self.caller, "target": target, "skill": skill,
            "success": success, "latency_ms": latency_ms,
            "timestamp": timestamp, "note": note, "stack_depth": stack_depth})

    # ---- 不可替代性：CIRCE 真正回答的问题 ----
    def irreplaceability(self, skill: str | None = None) -> dict:
        """把一个核剖面折成一个可比较的标量，并保留三个维度的明细。

        折算方式必须显式，因为不同调用方对「深度 / 密度 / 跨度」的重视不同：
        一个只做一次高风险决策的调用方，在意 depth；
        一个要长期反复调用的调用方，在意 density。

        所以这里给出**三个权重都要传**的接口，而不是内置一个"标准权重"——
        内置权重会把「谁的不可替代性更高」偷换成一个与调用方无关的排名。
        """
        info = self.query_profile(skill=skill)
        p = info.profile
        return {
            "agent_id": info.agent_id,
            "depth": p.get("depth"), "density": p.get("density"),
            "span": p.get("span"),
            "verified": info.verified, "verify_note": info.verify_note,
            "profile": p,
        }


def irreplaceability_score(profile: dict, w_depth: float,
                           w_density: float, w_span: float) -> float:
    """按调用方给定的权重把三维折成一个标量。

    权重必须由调用方给，且必须和为 1——否则「分数」会随权重尺度漂移，
    两个调用方的分数不可比。
    """
    total = w_depth + w_density + w_span
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"三个权重之和必须为 1，收到 {total}")
    for name, w in (("w_depth", w_depth), ("w_density", w_density), ("w_span", w_span)):
        if w < 0:
            raise ValueError(f"{name} 不能为负")
    return (float(profile.get("depth", 0.0)) * w_depth
            + float(profile.get("density", 0.0)) * w_density
            + float(profile.get("span", 0.0)) * w_span)
