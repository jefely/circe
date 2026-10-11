"""CIRCE 服务端 —— 三个端点的本地实现（stdlib only，无外部依赖）。

为什么用 http.server 而不是 Flask/FastAPI
----------------------------------------
CIRCE 的首个实验是「两个本地模型之间建立信任通道」（原文 §六）。
那种场景下最怕的是**为了跑通演示而引入一堆依赖**——
依赖越重，"这条通道能通"这个结论就越依赖环境，越不像在证明协议本身。
stdlib 的 http.server 足够承载三个端点，且换任何一台机器都能跑。

状态
----
每个 agent 一个 `trust/chain.py` 的 `Ledger`（仅追加账本）+ 一份当前 `CoreProfile`。
核剖面由注入的 `CoreProfileProvider` 提供 —— **协议不选边**（见 endpoints.py 顶部说明）。
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 让本模块能被直接跑（`python server.py`）——见 vc.py 顶部同样的说明。
# protocol/ 需要 endpoints（同目录）与 trust/（上一级）。
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import (PROBE_PATH, QUERY_PATH, UPDATE_PATH, CoreProfile,  # noqa: E402
                       CoreProfileProvider, QueryResponse, ReadinessResponse,
                       StaticProvider, UpdateRequest, UpdateResponse)
from trust.chain import Ledger, core_hash, decay_weight, weighted_profile  # noqa: E402
from trust import vc as VC  # noqa: E402
from stats_provider import StatsProvider, records_from_ledger  # noqa: E402


class LedgerTampered(AssertionError):
    """账本自校验失败 —— 拒绝签发凭据，而不是签一份「看起来有效」的。"""


class TimestampRejected(ValueError):
    """这次请求自报的时间戳不可信 —— **拒绝写入，而不是静默换一个值**。

    只在**本次请求自己**的时间戳超出容差时抛。已有的历史记录里若存在未来时间戳，
    不抛（那会让这个 agent 从此写不进任何东西），而是夹住并在响应里**报出来**。
    理由见 `AgentState.update`。
    """


def parse_timestamp(raw: Any) -> datetime:
    """解析 ISO-8601，带 `Z` 的、**不带时区的**都要能接住。

    ⚠️ 不带时区那一条是被一个真实 LLM 逼出来的（`CIRCE/mcp/FINDINGS.md` §二 F2）：
    `circe_trust_update` 的 schema 只说 `"description": "ISO-8601 时间戳"`，
    **没有要求带偏移量**；而不给模型时钟时它会编一个时间戳，
    编出来的格式**不受约束**。`datetime.fromisoformat` 对不带偏移量的输入
    返回 **naive** 的 datetime，与 tz-aware 的相减直接 `TypeError`。
    一个 LLM 调用方省略偏移量是完全正常的，所以这里必须接住，
    并**明确按 UTC 解释**——而不是把一个类型错误丢出去。
    """
    s = str(raw).strip()
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class AgentState:
    """一个 agent 的全部状态：当前剖面 + 仅追加账本 + 签发密钥。

    账本只增不改。剖面是根据账本重算出来的缓存，**不是**真相来源——
    真相在账本里。这一点让「篡改历史」必须改账本，而账本受链尾锚定保护。
    """

    def __init__(self, agent_id: str, profile: CoreProfile,
                 provider: CoreProfileProvider,
                 data_dir: Path,
                 skill_vocabulary: frozenset[str] | None = None) -> None:
        self.agent_id = agent_id
        self.provider = provider
        self.data_dir = data_dir
        # 受控词表（可选）。**只影响派生统计，不影响账本**——
        # 见 stats_provider.skill_key() 的 docstring 与 FINDINGS.md §二 F1。
        self.skill_vocabulary = skill_vocabulary
        data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger.load(data_dir / "ledger.json")
        self.sk = VC.new_key()
        VC.save_key(self.sk, data_dir / "issuer.pem")
        self._lock = threading.Lock()

    # ---- 核剖面 ----
    def current_profile(self) -> CoreProfile:
        """当前核剖面 —— **由账本重算，而不是返回注入的固定值**。

        ⚠️ 这里曾经有一个真实交互才暴露的缺口（2026-10-10，demo_real_models 抓出）：
        初版直接 `return self.provider.profile_for(...)`，于是：

            账本里有 6 条真实调用记录，而查询端点仍返回 density=0.0

        **「自动增量」名义上实现了，实际断了**——
        `update()` 写账本，但账本没有回到 profile 的计算里。

        **为什么自指测试抓不到**：`tests/` 里的 provider 与账本从不同时使用——
        provider 是静态的，账本是另测的。**只有当真实调用真的走完整条链
        （模型回答 → Trust Update → 再查询）时，两者才第一次相遇。**

        设计上的要点：**账本是真相来源，剖面是它的派生缓存。**
        所以重算而不是存值——存值会引入第二条真相来源，而两者会漂移。
        """
        recs = records_from_ledger(self.ledger.entries)
        if recs:
            return StatsProvider(
                records=recs,
                skill_vocabulary=self.skill_vocabulary).profile_for(self.agent_id)
        # 账本为空：还没有任何调用历史，返回注入的初值（通常是全 0 的"白纸"）
        return self.provider.profile_for(self.agent_id)

    def issue_credential(self) -> dict:
        """给当前核剖面签发一份 VC，并把账本链尾一起锚进去。

        为什么必须锚 `ledgerHead`：见 `CIRCE/README.md` §七——
        哈希链只能证明链内自洽，防不住「改了历史再重算整条链」。

        ⚠️ **签发前必须先验自己的账本**（2026-10-10，端到端演示抓出的洞）：
        初版直接签发，于是「账本第 1 条被改、链尾没变」时，
        端点仍用那个没变的链尾签出一份**验签通过**的凭据——
        调用方看到 verified=True，而服务方的历史已经被改过。

        为什么链尾没变：`head` 是【最后一条】的 hash。
        改中间某条而不重算，链尾不变；而重算整条链虽会改变链尾，
        却仍能自洽——所以**只有先 verify() 才能确定这份账本没被动过**。
        """
        problems = self.ledger.verify()
        if problems:
            raise LedgerTampered(
                f"{self.agent_id} 的账本校验失败（{len(problems)} 处），拒绝签发凭据："
                f"{problems[0]}")
        p = self.current_profile()
        return VC.issue(self.sk, self.agent_id, core_hash(p.to_dict()),
                        profile_seq=len(self.ledger.entries),
                        ledger_head=self.ledger.head)

    # ---- 信任更新 ----
    def update(self, req: UpdateRequest,
               half_life_seconds: float = 86400 * 30,
               max_future_skew_seconds: float = 300.0) -> UpdateResponse:
        """追加一条记录，并按时间衰减重算调用统计。

        ⚠️ **2026-10-10 改了两处，都是被一个真实 LLM 调用方逼出来的**
        （`CIRCE/mcp/FINDINGS.md` §二 F2/F3）。改之前是：

            now = datetime.fromisoformat(req.timestamp.replace("Z", "+00:00"))
            ...
            pairs.append((e.profile, decay_weight(max(age, 0.0), half_life_seconds)))

        **① 参考时钟原来取自【本次请求自己带的 timestamp】。**
        于是每一个调用方都自己选定衰减的参考系：
        一个未来的时间戳（或一个很旧的）就让**整条历史**的 age 变大、权重趋零，
        而它自己那条 age≈0 → 权重 1.0。半衰期 30 天下，
        未来 10 年的那一条 vs 2026 年的全部历史：

            0.5^(315619200 / 2592000) = 2.2e-37

        **一个字段就能把加权聚合的参考系挪到自己身上。**
        现在参考时钟是**服务端时钟**；调用方给的时间戳只作为**这条记录自己的时间**。

        **② `max(age, 0.0)` 把 `decay_weight` 的守卫消掉了。**
        `trust/chain.py` 的 `decay_weight` 明确 `if age_seconds < 0: raise`，
        理由是「未来时间戳意味着时钟有问题」——而那个 `max` 让它永远收不到负数。
        现在分成两种情形，**因为它们的可修复性完全不同**：

        · **本次请求自己**的时间戳超出容差 → `TimestampRejected`，**拒绝写入**。
          调用方能改，所以报错是有用的；而且**校验在写账本之前**——
          账本只能追加，写进去就撤不回来。
        · **历史里已有**未来时间戳 → **不抛**（否则这个 agent 从此写不进任何东西），
          而是夹到 age=0 并把问题**列在响应的 `clock_skew_problems` 里**。
          **报出来，而不是猜。**

        `max_future_skew_seconds` 的默认 300 秒是**显式参数**：
        「快几秒」是正常时钟漂移，「快十年」是伪造，两者必须被区别对待——
        去掉夹紧会让前者也一起被拒，那是把一个可用性问题换成一个完整性问题。
        """
        # ⚠️ 先校验、再写账本。账本只能追加，写进去就撤不回来。
        req_ts = parse_timestamp(req.timestamp)      # 不可解析 → ValueError，不写
        now = datetime.now(timezone.utc)
        req_skew = (now - req_ts).total_seconds()
        if req_skew < -max_future_skew_seconds:
            raise TimestampRejected(
                f"timestamp={req.timestamp!r} 比服务端时钟晚 {abs(req_skew):.0f} 秒，"
                f"超出容差 {max_future_skew_seconds:.0f} 秒。"
                "**拒绝写入**：账本一旦追加就撤不回来，"
                "而一个未来的时间戳会把整条历史的衰减权重压到近零、"
                "让自己那条拿满权重（见本方法 docstring）。")

        with self._lock:
            # ⚠️ 账本里必须写下**核剖面需要的全部字段**（2026-10-10）：
            # `current_profile()` 现在从账本重算，而重算要用到
            # callee（筛出属于本 agent 的记录）与 stack_depth（算 depth）。
            # 初版只写了 caller/skill/success/latency_ms，于是重算时
            # callee 全是 "?"、stack_depth 全是默认 1 —— 剖面永远算不对。
            # **这是「判据分支与数据 schema 不同步」的又一次同形出现。**
            entry = self.ledger.append(
                {"caller": req.caller, "callee": req.target, "skill": req.skill,
                 "success": req.success, "latency_ms": req.latency_ms,
                 "stack_depth": int(getattr(req, "stack_depth", 1) or 1)},
                req.timestamp, note=req.note)
            entries = list(self.ledger.entries)
            head = self.ledger.head

        # 按时间衰减重算调用统计（原文 §五 第 3 步：最近的调用权重更高）
        pairs = []
        skew_problems: list[str] = []
        for e in entries:
            t = parse_timestamp(e.timestamp)
            age = (now - t).total_seconds()          # ← 服务端时钟，不是 req.timestamp
            if age < 0:
                # 历史里有未来时间戳：夹住，但**报出来**。
                skew_problems.append(
                    f"第 {getattr(e, 'seq', '?')} 条的时间戳比服务端时钟晚 "
                    f"{abs(age):.0f} 秒（{e.timestamp}），权重按 age=0 计")
                age = 0.0
            pairs.append((e.profile, decay_weight(age, half_life_seconds)))
        agg = weighted_profile(pairs) if pairs else {}
        new_profile = {
            "success_rate": round(
                sum(1 for e in entries if e.profile.get("success"))
                / max(len(entries), 1), 4),
            "weighted_calls": round(sum(w for _, w in pairs), 4),
            "by_skill": agg,
        }
        # 只在真的有时才加这个键——保证没有时钟问题的响应与改动前逐字节相同。
        if skew_problems:
            new_profile["clock_skew_problems"] = skew_problems
        return UpdateResponse(
            accepted=True, ledger_seq=entry.seq, ledger_head=head,
            new_profile=new_profile, note=f"已追加第 {entry.seq} 条")


class Handler(BaseHTTPRequestHandler):
    server_version = "CIRCE/0.1"

    def log_message(self, fmt, *args):        # 静音，避免污染演示输出
        pass

    # ---- 工具 ----
    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---- 三个端点 ----
    def do_GET(self) -> None:
        if self.path.startswith(PROBE_PATH):
            st: AgentState = self.server.state
            p = st.current_profile()
            self._json({"agent_id": st.agent_id, "ready": True,
                        "note": "探活刻意不返回核剖面——那会让高频探活背上签名与账本写入的代价",
                        "has_profile": bool(p)})
            return
        if self.path.startswith(QUERY_PATH):
            st = self.server.state
            p = st.current_profile()
            try:
                cred = st.issue_credential()
            except LedgerTampered as e:
                # 账本被动过 —— **拒签**，而不是签一份"看起来有效"的。
                # 返回 409 而不是 200：调用方必须能区分「peer 不可用」与「peer 的账本不可信」。
                self._json({"error": "ledger-tampered", "detail": str(e),
                            "agent_id": st.agent_id}, 409)
                return
            self._json({"profile": p.to_dict(), "credential": cred,
                        "ledger_head": st.ledger.head,
                        "ledger_seq": len(st.ledger.entries),
                        "note": ""})
            return
        self._json({"error": "unknown path", "path": self.path}, 404)

    def do_POST(self) -> None:
        if self.path.startswith(UPDATE_PATH):
            st = self.server.state
            body = self._read()
            try:
                req = UpdateRequest(
                    caller=body["caller"], target=body["target"],
                    skill=body["skill"], success=bool(body["success"]),
                    latency_ms=int(body.get("latency_ms", 0)),
                    timestamp=body["timestamp"], note=body.get("note", ""),
                    stack_depth=int(body.get("stack_depth", 1)))
            except (KeyError, TypeError, ValueError) as e:
                self._json({"error": f"请求不合法: {e}"}, 400)
                return
            if req.target != st.agent_id:
                self._json({"error": f"这是 {st.agent_id}，不是 {req.target}"}, 400)
                return
            try:
                r = st.update(req)
            except TimestampRejected as e:
                # 400 而不是 500：这是**调用方给错了参数**，不是服务端故障。
                # 也绝不返回 accepted=True —— 那会让调用方以为写进去了。
                self._json({"error": "timestamp-rejected", "detail": str(e)}, 400)
                return
            self._json({"accepted": r.accepted, "ledger_seq": r.ledger_seq,
                        "ledger_head": r.ledger_head, "new_profile": r.new_profile,
                        "note": r.note})
            return
        self._json({"error": "unknown path", "path": self.path}, 404)


class CirceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, state: AgentState):
        super().__init__(addr, Handler)
        self.state = state


def serve(agent_id: str, port: int, data_dir: Path,
          profile: CoreProfile | None = None,
          skill_vocabulary: frozenset[str] | None = None
          ) -> tuple[CirceServer, threading.Thread]:
    """起一个 agent 的服务端，返回 (server, thread)。"""
    if profile is None:
        profile = CoreProfile(depth=0.5, density=0.5, span=0.5, agent_id=agent_id)
    provider = StaticProvider({agent_id: profile})
    state = AgentState(agent_id, profile, provider, data_dir,
                       skill_vocabulary=skill_vocabulary)
    srv = CirceServer(("127.0.0.1", port), state)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t
