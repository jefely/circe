"""CIRCE 的 MCP 服务器 —— 把三个端点暴露成 MCP tool。

跑法（由 MCP 客户端启动，不手动跑）:
    python CIRCE/mcp/server.py --agent agent-B --data CIRCE/mcp/data/agent-B

MCP 客户端配置示例（Claude Desktop / Cursor / 任何 MCP 宿主）:
    {
      "mcpServers": {
        "circe": {
          "command": "python",
          "args": ["D:/myproject/CIRCE/mcp/server.py",
                   "--agent", "agent-B",
                   "--data", "D:/myproject/CIRCE/mcp/data/agent-B"]
        }
      }
    }

为什么自己实现 JSON-RPC 而不装 SDK
----------------------------------
本机没有 `mcp` Python 包，也没有 Node 的 `@modelcontextprotocol/sdk`
（`tools/check_mcp_env.py` 查过）。而 MCP 的 stdio 传输就是
**一行一个 JSON-RPC 2.0 消息**——这不需要库。

**这个取舍与 CIRCE 其它部分一致**：`protocol/server.py` 用 stdlib 的 http.server
而不是 Flask/FastAPI，理由是「为了让跑通演示不依赖环境」。
**依赖越重，"这条通道能通"这个结论就越依赖环境，越不像在证明协议本身。**

安装 SDK 也能work，但那会让「CIRCE 能被生态接进来」这件事
取决于对方能不能装上同一个版本的 SDK。

本服务器扮演什么角色
--------------------
**它是一个 agent 的信任基础设施的门面**：MCP 客户端（别的 AI）通过它
查询/更新**本机这个 agent**的核剖面。所以：

    circe_readiness_probe    「这个 agent 现在能接活吗」——便宜，不带剖面
    circe_core_profile_query 「删掉它，调用方会失去什么」——剖面 + VC + 链尾
    circe_trust_update       「我刚调用过它，结果是这样」——追加账本

**调用方模型（LLM）的判断留在它自己那边，不进链。** 见 README §五之二。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CIRCE = HERE.parent
for _p in (str(CIRCE), str(CIRCE / "protocol"), str(CIRCE / "trust")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from endpoints import CoreProfile, UpdateRequest          # noqa: E402
from server import (AgentState, LedgerTampered,           # noqa: E402
                    TimestampRejected)
from trust import vc as VC                                # noqa: E402
from trust.chain import core_hash                         # noqa: E402
from stats_provider import StatsProvider, records_from_ledger  # noqa: E402

# ---------------------------------------------------------------- 协议版本
# 我们**同时**支持两代协议，因为客户端仍在迁移期：
#
#   2025-06-18（旧）  有 initialize / notifications/initialized 握手，有 ping
#   2026-07-28（新）  stateless：握手与 ping 被移除，改为
#                     · 每个请求在 _meta 里自带 protocolVersion 与 clientCapabilities
#                     · 服务端 MUST 实现 server/discover
#                     · 每个结果 MUST 带 resultType
#                     · 版本不匹配返回 UnsupportedProtocolVersionError
#
# 上游 changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog.md
# 影响分析: CIRCE/mcp/MCP-2026-07-28-影响.md
#
# **为什么要兼容而不是直接改成新规范**：CIRCE §2.4 那条设计理由
# ——「挂在 MCP 上意味着对方已经会了」——**只在能连上真实客户端时才成立**。
# 客户端还没迁完，纯新实现会让 CIRCE 连不上任何东西；
# 而纯旧实现会被正在迁移的客户端判为版本不支持。**兼容层的代价只是几十行。**
PROTOCOL_VERSION = "2026-07-28"        # 我们首选（对外声明）的版本
PROTOCOL_VERSION_LEGACY = "2025-06-18"  # 兼容期保留
PROTOCOL_VERSIONS = (PROTOCOL_VERSION, "2025-11-25", PROTOCOL_VERSION_LEGACY)

SERVER_NAME = "circe"
SERVER_VERSION = "0.1.0"

# 新规范把版本与控制信息放在每个请求的 _meta 里，键是带命名空间的
META_PREFIX = "io.modelcontextprotocol/"
KEY_VERSION = META_PREFIX + "protocolVersion"
KEY_CLIENT_CAPS = META_PREFIX + "clientCapabilities"
KEY_CLIENT_INFO = META_PREFIX + "clientInfo"
KEY_SERVER_INFO = META_PREFIX + "serverInfo"


def _meta_of(msg: dict) -> dict:
    """取出请求里的 _meta（params._meta 优先，顶层 _meta 兜底）。"""
    params = msg.get("params") or {}
    if isinstance(params.get("_meta"), dict):
        return params["_meta"]
    if isinstance(msg.get("_meta"), dict):
        return msg["_meta"]
    return {}


def request_version(msg: dict) -> str:
    """这个请求声明的协议版本。

    **两个位置都要看**——这是被实测抓出来的（2026-10-10，用 recording proxy）：

        C→S  server/discover  _meta.protocolVersion = 2026-07-28   （新协议位置）
        C→S  initialize       params.protocolVersion = 2025-11-25  （旧协议位置）

    初版只看 `_meta`，于是对 `initialize` 请求一律兜底到 `2025-06-18`，
    **导致应答里 `result.protocolVersion` 是客户端要的 `2025-11-25`，
    而 `_meta.protocolVersion` 是 `2025-06-18`——同一个响应里两个版本号不一致。**

    客户端没在意（它继续握手了），但那是真的不一致：
    **一个响应不该对同一个量给出两个答案。**
    """
    meta = _meta_of(msg)
    v = meta.get(KEY_VERSION)
    if isinstance(v, str) and v:
        return v
    # 旧协议把版本放在 params.protocolVersion
    params = msg.get("params") or {}
    v = params.get("protocolVersion")
    if isinstance(v, str) and v:
        return v
    return PROTOCOL_VERSION_LEGACY



# --------------------------------------------------------------------------
# 工具定义（tools/list 的返回）
# --------------------------------------------------------------------------
def tool_defs(agent_id: str) -> list[dict]:
    """工具定义。

    ⚠️ 描述用词要站在【调用方】那一侧（2026-10-10，MCP 客户端验证抓出）：
    初版的描述用的是 CIRCE 的术语（「写入仅追加账本」「校验账本是否被改动过」），
    而调用方问的是它自己的话（「怎么记录这次调用」「账本有没有被人改过」）。
    一个只读 description 的客户端因此选错了工具。

    **这不是文案问题**：MCP 客户端（LLM）**只能**从 description 了解这个工具。
    描述里没有它会的词，它就找不到这个工具——**工具等于不存在。**
    所以每条 description 都同时写：它做什么、**什么时候该用**、以及**调用方会怎么问**。
    """
    return [
        {
            "name": "circe_readiness_probe",
            "description": (
                f"探活 / 检查可用性：{agent_id} 现在能否接受调用、是否在线、能不能接活。"
                "在你准备调用一个 agent 之前，先用它确认对方可用。"
                "刻意不返回核剖面——探活要能被高频调用，"
                "返回剖面会让每次探活都背上签名与账本写入的代价。"),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "circe_core_profile_query",
            "description": (
                f"查询 {agent_id} 的核剖面（深度/密度/跨度）+ 可验证凭证 + 账本链尾。"
                "什么时候该用：在决定**是否值得调用 / 依赖**某个 agent 之前，"
                "想知道「删掉它，我会失去什么」、「它的不可替代性有多高」、"
                "「它被多少人依赖过」时用它。"
                "CIRCE 的核心问题不是「它可不可信」，而是「删掉它，调用方会失去什么」。"
                "三个维度都是调用日志的确定性统计量，不含 LLM 判断。"
                "**账本为空时三维全为 0**——那表示这个 agent 还没有被调用过（新加入的、"
                "白纸），不是它没有价值。CIRCE 的价值必须通过时间积累。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string",
                              "description": "只看某个技能维度（可选）。depth 是位置属性，不随技能变。"},
                    "explain": {"type": "boolean",
                                "description": "是否附上可手算核对的解释串（默认 true）"},
                },
                "required": [],
            },
        },
        {
            "name": "circe_trust_update",
            "description": (
                f"记录一次调用 / 上报交互结果：把「谁调用了 {agent_id}、成功与否、"
                "耗时多少、什么时候」写进它不可篡改的仅追加账本。"
                "什么时候该用：**你刚调用完一个 agent 之后**，无论成功还是失败，"
                "都该记录一次——因为核剖面就是从这些记录里积累出来的，"
                "不记录则对方的价值永远显示为 0。"
                "**stack_depth 必须由你（调用方）给**——只有 caller 知道自己在哪一层"
                "发起调用，被调用方看不到。它也是 depth 维度的唯一来源。"
                "写完之后核剖面会自动重算（账本是真相来源，剖面是它的派生缓存）。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "caller": {"type": "string", "description": "调用方标识（谁调用的）"},
                    "skill": {
                        "type": "string",
                        "description": (
                            "这次调用属于哪一类任务。**同一类任务每次都要用同一个字符串**"
                            "——因为它直接进 span 维度（「跨多少种任务类型」），"
                            "同一个任务换个名字写就会被算成多了一种。"
                            "用简短的名词，例如 \"问答\" / \"检索\" / \"代码审查\" / \"翻译\"。"
                            "**不确定时用 \"unspecified\"**，不要编一个只此一次的名字。")},
                    "success": {"type": "boolean", "description": "调用是否成功"},
                    "latency_ms": {"type": "number", "description": "耗时（毫秒）"},
                    "timestamp": {
                        "type": "string",
                        "description": (
                            "ISO-8601 时间戳。**必须由宿主/调用方把当前时间填进来**"
                            "——模型自己不知道现在几点，不要凭印象编一个："
                            "服务端会用服务端时钟核对它，"
                            "偏离超过容差的会被**拒绝写入**。"
                            "带时区偏移量（如 `2026-10-10T23:24:45+08:00`）。")},
                    "stack_depth": {"type": "number", "description": "调用栈深度（≥1）"},
                    "note": {"type": "string", "description": "备注（可选）"},
                },
                "required": ["caller", "skill", "success", "latency_ms", "timestamp"],
            },
        },
        {
            "name": "circe_ledger_verify",
            "description": (
                f"校验 {agent_id} 的账本有没有被人改过 / 是否被篡改 / 是否完整。"
                "返回问题列表；空列表表示链完整、未被改动。"
                "什么时候该用：**在信任对方的核剖面之前，或在它签发凭据之前**，"
                "先验一次。**这一步不能省**：链尾只锚最后一条，"
                "改中间某条而不重算时链尾不变，"
                "所以「只锚链尾」防不住中间改写——签发凭据前必须先跑这个。"),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
    ]


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------
class CirceMcp:
    def __init__(self, agent_id: str, data_dir: Path,
                 profile: CoreProfile | None = None,
                 skill_vocabulary: frozenset[str] | None = None) -> None:
        self.agent_id = agent_id
        prof = profile or CoreProfile(depth=0.0, density=0.0, span=0.0,
                                      agent_id=agent_id)
        from endpoints import StaticProvider
        self.state = AgentState(agent_id, prof,
                                StaticProvider({agent_id: prof}), data_dir,
                                skill_vocabulary=skill_vocabulary)

    # ---- 工具 ----
    def probe(self) -> dict:
        p = self.state.current_profile()
        return {"agent_id": self.agent_id, "ready": True,
                "has_history": p.call_count > 0,
                "note": "探活不返回核剖面（那会让高频探活变贵）"}

    def query(self, skill: str | None = None, explain: bool = True) -> dict:
        p = self.state.current_profile()
        out: dict = {
            "agent_id": self.agent_id,
            "profile": p.to_dict(),
            "ledger_seq": len(self.state.ledger.entries),
            "ledger_head": self.state.ledger.head,
            "warm": p.call_count > 0,
        }
        if p.call_count == 0:
            out["warning"] = (
                "账本为空，三维全为 0。这表示**尚未被调用过**，不是没有价值——"
                "CIRCE 的价值必须通过时间积累。第一次探测给不出有用信息是设计，不是缺陷。")
        try:
            out["credential"] = self.state.issue_credential()
            out["credential_ok"] = True
        except LedgerTampered as e:
            out["credential"] = None
            out["credential_ok"] = False
            out["error"] = f"拒签：账本不可信。{e}"
        if explain:
            recs = records_from_ledger(self.state.ledger.entries)
            if recs:
                out["explain"] = StatsProvider(
                    records=recs,
                    skill_vocabulary=self.state.skill_vocabulary
                ).explain(self.agent_id)
        return out

    def update(self, caller: str, skill: str, success: bool, latency_ms: int,
               timestamp: str, stack_depth: int = 1, note: str = "") -> dict:
        req = UpdateRequest(caller=caller, target=self.agent_id, skill=skill,
                            success=success, latency_ms=int(latency_ms),
                            timestamp=timestamp, note=note,
                            stack_depth=int(stack_depth))
        r = self.state.update(req)
        p = self.state.current_profile()
        return {"accepted": r.accepted, "ledger_seq": r.ledger_seq,
                "ledger_head": r.ledger_head, "new_profile": p.to_dict(),
                "note": r.note}

    def ledger_verify(self) -> dict:
        problems = self.state.ledger.verify()
        return {"ok": not problems, "problems": problems,
                "entries": len(self.state.ledger.entries),
                "head": self.state.ledger.head}


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------
class Server:
    def __init__(self, impl: CirceMcp) -> None:
        self.impl = impl
        self.tools = {t["name"]: t for t in tool_defs(impl.agent_id)}

    # ---- 分发 ----
    def handle(self, msg: dict) -> dict | None:
        """处理一条消息。返回 None 表示这是通知，不需要响应。"""
        method = msg.get("method")
        mid = msg.get("id")

        # 通知（无 id）
        if mid is None and method:
            if method == "notifications/initialized":
                pass          # 客户端确认初始化完成；无需回应
            return None

        try:
            if method == "initialize":
                # 旧协议（≤2025-11-25）的握手。新规范已移除它，
                # 但兼容期仍要应答——否则还没迁的客户端连不上。
                return self._ok(mid, self._initialize(msg.get("params") or {}),
                                msg, include_meta=True)
            if method == "notifications/initialized":
                pass          # 通知无响应（旧协议）
                return None
            if method == "server/discover":
                # **新规范 MUST**：服务端用它宣告支持的版本、能力、身份。
                return self._ok(mid, self._discover(), msg, include_meta=True)
            if method == "ping":
                # 新规范已移除 ping；保留为兼容，并在 _meta 里标注
                return self._ok(mid, {}, msg, include_meta=True)
            if method == "tools/list":
                # 新规范：tools/list 不再随连接变化（我们本来就是这样）
                v = request_version(msg)
                if not self._version_ok(v):
                    return self._version_error(mid, v)
                return self._ok(mid, {"tools": list(self.tools.values())},
                                msg, include_meta=True)
            if method == "tools/call":
                v = request_version(msg)
                if not self._version_ok(v):
                    return self._version_error(mid, v)
                return self._ok(mid, self._call(msg.get("params") or {}),
                                msg, include_meta=True)
            return self._err(mid, -32601, f"不认识的方法: {method}")
        except LedgerTampered as e:
            return self._err(mid, -32000, f"账本不可信: {e}")
        except TimestampRejected as e:
            # **-32602 (Invalid params)** 而不是 -32603 (Internal error)：
            # 这是调用方给错了参数，不是服务端故障。调用方看到这个码才知道
            # 该改的是它自己的时间戳 —— 见 protocol/server.py TimestampRejected。
            return self._err(mid, -32602, f"时间戳被拒: {e}")
        except Exception as e:
            return self._err(mid, -32603, f"{type(e).__name__}: {e}")

    @staticmethod
    def _version_ok(v: str) -> bool:
        return v in PROTOCOL_VERSIONS

    def _version_error(self, mid, got: str) -> dict:
        """新规范的 `UnsupportedProtocolVersionError`。

        旧协议没有这个错误码，所以**只在请求真的声明了不支持的版本时**才回它；
        完全没声明版本的请求按旧客户端处理（见 request_version）。
        """
        return {"jsonrpc": "2.0", "id": mid, "error": {
            "code": -32001,
            "message": f"UnsupportedProtocolVersionError: {got}",
            "data": {"supported": list(PROTOCOL_VERSIONS)},
        }}

    def _discover(self) -> dict:
        """`server/discover` 的返回：版本、能力、身份。

        新规范要求它 **MUST** 实现，且客户端可以在任何别的请求之前调它
        （用于前置版本选择，或在 STDIO 上做向后兼容探测）。
        """
        return {
            "protocolVersions": list(PROTOCOL_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "CIRCE：核不可替代性识别与持续评估。"
                "它不测量可靠性，它测量不可替代性——"
                "不问「你是否可信」，而问「删掉你，调用方会失去什么」。"
                "核剖面是调用日志的确定性统计量（深度/密度/跨度），不含 LLM 判断。"),
        }

    def _initialize(self, params: dict) -> dict:
        """旧协议的握手应答。

        新规范已移除 `initialize`，但兼容期必须应答——
        **注意这里回的是「客户端要的版本」而不是我们首选的版本**：
        旧客户端拿到一个它不认识的版本号可能直接断开。
        """
        want = params.get("protocolVersion")
        if isinstance(want, str) and want in PROTOCOL_VERSIONS:
            reply = want
        else:
            reply = PROTOCOL_VERSION_LEGACY
        return {
            "protocolVersion": reply,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "CIRCE：核不可替代性识别与持续评估。"
                "它不测量可靠性，它测量不可替代性——"
                "不问「你是否可信」，而问「删掉你，调用方会失去什么」。"
                "核剖面是调用日志的确定性统计量（深度/密度/跨度），不含 LLM 判断。"),
        }

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in self.tools:
            return {"content": [{"type": "text", "text": f"未知工具: {name}"}],
                    "isError": True}

        if name == "circe_readiness_probe":
            result = self.impl.probe()
        elif name == "circe_core_profile_query":
            result = self.impl.query(skill=args.get("skill"),
                                     explain=bool(args.get("explain", True)))
        elif name == "circe_trust_update":
            missing = [k for k in ("caller", "skill", "success",
                                   "latency_ms", "timestamp") if k not in args]
            if missing:
                return {"content": [{"type": "text",
                                     "text": f"缺参数: {missing}"}], "isError": True}
            result = self.impl.update(
                caller=args["caller"], skill=args["skill"],
                success=bool(args["success"]), latency_ms=args["latency_ms"],
                timestamp=args["timestamp"],
                stack_depth=args.get("stack_depth", 1),
                note=args.get("note", ""))
        elif name == "circe_ledger_verify":
            result = self.impl.ledger_verify()
        else:
            return {"content": [{"type": "text", "text": f"未实现: {name}"}],
                    "isError": True}

        return {"content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False,
                                                indent=2, sort_keys=True)}],
                "isError": False}

    @staticmethod
    def _ok(mid, result, msg: dict | None = None,
            include_meta: bool = False) -> dict:
        """构造成功响应。

        **两代协议的差别都在这里：**

        旧（≤2025-11-25）：`{"jsonrpc","id","result"}`
        新（2026-07-28）：
          · 每个结果 **MUST** 带 `resultType`（普通结果为 `"complete"`）
          · 服务端 **SHOULD** 在自己的结果里标识自己
            （`_meta["io.modelcontextprotocol/serverInfo"]`）
          · 版本与客户端信息从请求的 `_meta` 读，不再靠握手

        **我们总是带上 `resultType` 与 `_meta`**——因为旧客户端会忽略
        `result` 里它不认识的字段（JSON-RPC 允许扩展成员），
        而新客户端**要求**它们存在。**多给一个旧客户端不看的字段，
        比少给一个新客户端需要的字段安全。**
        """
        if isinstance(result, dict):
            result.setdefault("resultType", "complete")
        if include_meta:
            meta = result.setdefault("_meta", {}) if isinstance(result, dict) else {}
            meta[KEY_SERVER_INFO] = {"name": SERVER_NAME, "version": SERVER_VERSION}
            if msg is not None:
                rv = request_version(msg)
                meta[KEY_VERSION] = rv
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}


def serve_stdio(impl: CirceMcp) -> int:
    """一行一个 JSON-RPC 消息，直到 stdin 关闭。

    **stdout 只能放协议消息**——任何调试输出都必须走 stderr，
    否则会污染 JSON-RPC 流，客户端会解析失败。
    """
    srv = Server(impl)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[mcp] 无法解析输入: {e}", file=sys.stderr)
            continue
        resp = srv.handle(msg)
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CIRCE 的 MCP stdio 服务器")
    ap.add_argument("--agent", default="agent-B", help="本服务器代表的 agent id")
    ap.add_argument("--data", default=None,
                    help="账本与密钥的存放目录（默认 CIRCE/mcp/data/<agent>）")
    ap.add_argument("--skill-vocabulary", default=None,
                    help="逗号分隔的受控技能词表（可选）。给了词表后，"
                         "词表外的 skill 在**派生统计**里计入 'unspecified' 一个桶——"
                         "**账本里仍写调用方原话**。不传 = 只做格式归一（与旧行为一致）。"
                         "理由见 CIRCE/mcp/FINDINGS.md §二 F1")
    ap.add_argument("--selftest", action="store_true",
                    help="不走 stdio，直接跑内置自检")
    args = ap.parse_args()

    data = Path(args.data) if args.data else (HERE / "data" / args.agent)
    vocab = (frozenset(s.strip() for s in args.skill_vocabulary.split(",") if s.strip())
             if args.skill_vocabulary else None)
    impl = CirceMcp(args.agent, data, skill_vocabulary=vocab)

    if args.selftest:
        return selftest(impl)

    print(f"[mcp] CIRCE server 启动: agent={args.agent} data={data}", file=sys.stderr)
    return serve_stdio(impl)


# --------------------------------------------------------------------------
def selftest(impl: CirceMcp) -> int:
    """不依赖外部客户端，直接验证协议层与三个工具。"""
    srv = Server(impl)
    PASS, FAIL = [], []

    def check(name, ok, detail=""):
        (PASS if ok else FAIL).append(name)
        print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))

    print("=" * 70)
    print("  CIRCE MCP 服务器自检")
    print("=" * 70)

    print("\n  ① 生命周期")
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "clientInfo": {"name": "test", "version": "0"}}})
    check("initialize 返回 protocolVersion",
          r["result"]["protocolVersion"] == "2025-06-18")
    check("声明 tools capability", "tools" in r["result"]["capabilities"])
    check("有 serverInfo.name", r["result"]["serverInfo"]["name"] == "circe")

    r = srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("通知不产生响应（返回 None）", r is None)

    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    # ⚠️ 这条断言因协议变化改过一次（2026-10-10）：
    # 初版写的是 `r["result"] == {}`，而新规范要求**所有结果**带 `resultType`，
    # 于是 ping 的结果变成了 `{"resultType": "complete"}`。
    # **失败指向的是断言，不是代码**——而这正是"断言也要跟着协议走"的一个实例。
    check("ping 返回空结果（+新规范要求的 resultType）",
          r["result"] == {} or r["result"].get("resultType") == "complete",
          str(r["result"]))

    print("\n  ② tools/list")
    r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = [t["name"] for t in r["result"]["tools"]]
    check("暴露四个工具", len(names) == 4, ", ".join(names))
    for want in ("circe_readiness_probe", "circe_core_profile_query",
                 "circe_trust_update", "circe_ledger_verify"):
        check(f"含 {want}", want in names)
    for t in r["result"]["tools"]:
        ok = "inputSchema" in t and t["inputSchema"].get("type") == "object"
        check(f"{t['name']} 的 inputSchema 合法", ok)

    # ---- ②之二 工具名的可移植性护栏（2026-10-11 新增）----
    #
    # ⚠️ 这一条是被一个真实客户端打出来的**缺陷**换来的：
    # **MCP 协议不禁止工具名里出现 `.`**（我们原本用 `circe.` 做命名空间），
    # 而 **VS Code 1.141.0 拒绝它**，把四个工具全部判为无效：
    #
    #     Tool "circe.readiness_probe" is invalid. Tools names may only contain [a-z0-9_-]
    #
    # **可用工具数 = 0。** 而 Claude Code 与本地 Ollama 都接受带点号的名字——
    # 所以这是**客户端分歧**，不是"没有这道缝"。
    #
    # 约束的真正来源不在 MCP 里，在 **LLM 侧 function name 的模式**
    # （`[a-z0-9_-]`）。而工具名是给模型看的标识符，所以**这条约束是承重的**。
    #
    # 把断言写进自检，是为了**让不可移植的名字无法被写回来**——
    # 只靠文档记一笔是不够的：改的人不会读到那段文档，而门禁会拦住他。
    import re as _re
    _NAME_RE = _re.compile(r"^[a-z0-9_-]{1,64}$")
    bad = [n for n in names if not _NAME_RE.match(n)]
    check("**工具名全部满足客户端通用的 `[a-z0-9_-]{1,64}`**"
          "（VS Code 会拒绝其它字符；见 README）", not bad,
          f"不合法: {bad}" if bad else f"{len(names)} 个全部合法")

    print("\n  ③ circe_readiness_probe")
    r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "circe_readiness_probe", "arguments": {}}})
    txt = r["result"]["content"][0]["text"]
    d = json.loads(txt)
    check("返回 ready", d.get("ready") is True)
    check("不返回核剖面（高频调用要便宜）", "profile" not in d)

    print("\n  ④ circe_core_profile_query（空账本 → 白纸 + 警告）")
    r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "circe_core_profile_query", "arguments": {}}})
    d = json.loads(r["result"]["content"][0]["text"])
    check("三维全为 0", d["profile"]["depth"] == 0 and d["profile"]["density"] == 0)
    check("带 warning 说明这是「尚未被调用」而非「没有价值」",
          "warning" in d and "时间积累" in d["warning"])
    check("签发了凭据", d.get("credential_ok") is True and d["credential"] is not None)

    print("\n  ⑤ circe_trust_update（真实写入）")
    for i in range(5):
        r = srv.handle({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                        "params": {"name": "circe_trust_update", "arguments": {
                            "caller": "agent-A", "skill": "qa", "success": True,
                            "latency_ms": 300 + i * 50,
                            "timestamp": f"2026-10-10T12:0{i}:00Z",
                            "stack_depth": 1 + i % 3, "note": f"第{i+1}次"}}})
        check(f"第 {i+1} 次更新被接受",
              json.loads(r["result"]["content"][0]["text"])["accepted"] is True)

    print("\n  ⑥ 自动增量 —— 剖面必须随账本变化（洞四的回归检查）")
    r = srv.handle({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                    "params": {"name": "circe_core_profile_query", "arguments": {}}})
    d = json.loads(r["result"]["content"][0]["text"])
    check("density 不再是 0（账本已重算进剖面）",
          d["profile"]["density"] > 0, f"density={d['profile']['density']}")
    check("call_count 反映真实调用次数", d["profile"].get("call_count") == 5,
          f"call_count={d['profile'].get('call_count')}")
    check("带 explain 可手算核对", "explain" in d and "round(" in d["explain"]["depth"])

    print("\n  ⑦ circe_ledger_verify + 篡改检测")
    r = srv.handle({"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                    "params": {"name": "circe_ledger_verify", "arguments": {}}})
    d = json.loads(r["result"]["content"][0]["text"])
    check("干净账本校验通过", d["ok"] is True and d["problems"] == [],
          f"{d['entries']} 条")

    impl.state.ledger.entries[0].profile["success"] = False
    r = srv.handle({"jsonrpc": "2.0", "id": 22, "method": "tools/call",
                    "params": {"name": "circe_ledger_verify", "arguments": {}}})
    d = json.loads(r["result"]["content"][0]["text"])
    check("篡改第 1 条 → 被抓住", d["ok"] is False and d["problems"],
          d["problems"][0][:60] if d["problems"] else "")
    r = srv.handle({"jsonrpc": "2.0", "id": 23, "method": "tools/call",
                    "params": {"name": "circe_core_profile_query", "arguments": {}}})
    d = json.loads(r["result"]["content"][0]["text"])
    check("篡改后查询 → credential_ok=False（拒签，而不是签一份看着有效的）",
          d.get("credential_ok") is False, d.get("error", "")[:70])

    print("\n  ⑧ 错误路径")
    r = srv.handle({"jsonrpc": "2.0", "id": 30, "method": "不存在的/方法"})
    check("未知方法返回 -32601", r["error"]["code"] == -32601)
    r = srv.handle({"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                    "params": {"name": "circe_trust_update", "arguments": {}}})
    check("缺参数返回 isError", r["result"]["isError"] is True)
    r = srv.handle({"jsonrpc": "2.0", "id": 32, "method": "tools/call",
                    "params": {"name": "不存在的工具", "arguments": {}}})
    check("未知工具返回 isError", r["result"]["isError"] is True)

    # ---- ⑧之二 时间戳与 skill 的拒绝路径（2026-10-10 新增）----
    # 这两组断言来自 FINDINGS.md §二 F2/F3 与 F1：
    # **它们是被一个真实 LLM 调用方逼出来的**，所以测试也按那个场景写——
    # 「模型编了一个时间戳」「模型给 skill 编了同一个东西的五个名字」。
    print("\n  ⑧之二 时间戳：编一个未来的会被拒绝写（F3）")
    import tempfile
    from datetime import datetime, timedelta, timezone
    tmp_ts = Path(tempfile.mkdtemp(prefix="circe-ts-"))
    srv_ts = Server(CirceMcp("agent-ts", tmp_ts))

    def _up(s, ts, skill="qa", caller="agent-A"):
        return s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "circe_trust_update", "arguments": {
                             "caller": caller, "skill": skill, "success": True,
                             "latency_ms": 100, "timestamp": ts}}})

    now = datetime.now(timezone.utc)
    n_before = len(srv_ts.impl.state.ledger.entries)

    # ① 未来 10 年 → 必须被拒，且**账本一条都不能多**
    future = (now + timedelta(days=3650)).isoformat()
    r = _up(srv_ts, future)
    check("未来 10 年的时间戳被拒（-32602，不是 -32603）",
          r.get("error", {}).get("code") == -32602,
          str(r.get("error", {}).get("message"))[:70])
    check("**被拒时账本没有增长**（校验在写入之前）",
          len(srv_ts.impl.state.ledger.entries) == n_before,
          f"{n_before} → {len(srv_ts.impl.state.ledger.entries)}")

    # ② 容差内（客户端时钟快一点）→ 必须接受，否则把一个可用性问题
    #    换成了一个完整性问题
    r = _up(srv_ts, (now + timedelta(seconds=60)).isoformat())
    d = json.loads(r["result"]["content"][0]["text"]) if "result" in r else {}
    check("快 60 秒（容差 300 秒内）被接受", d.get("accepted") is True,
          str(r.get("error", {}).get("message"))[:70])

    # ③ 不带时区偏移量 → 按 UTC 解释，而不是 TypeError
    r = _up(srv_ts, now.strftime("%Y-%m-%dT%H:%M:%S"))
    check("不带时区的时间戳被接受（按 UTC 解释，不报 TypeError）",
          "result" in r and json.loads(r["result"]["content"][0]["text"])["accepted"],
          str(r.get("error", {}).get("message"))[:70])

    # ④ 参考时钟是服务端时钟：一个很旧的时间戳不再能把整条历史压成 0
    r = _up(srv_ts, (now - timedelta(days=3650)).isoformat())
    d = json.loads(r["result"]["content"][0]["text"])
    check("很旧的时间戳被接受（过时不是伪造）", d.get("accepted") is True)
    check("账本已有记录时，响应里不出现 clock_skew_problems（无未来记录）",
          "clock_skew_problems" not in d["new_profile"],
          str(d["new_profile"].get("clock_skew_problems")))

    print("\n  ⑧之三 skill 归一：噪声不再抬高 span（F1）")
    tmp_a = Path(tempfile.mkdtemp(prefix="circe-skill-"))
    noise = ["n1", "n2", "n3"]
    skills_a = ["问答"] + noise

    def _build(vocab, skills, tmp):
        s = Server(CirceMcp("agent-s", tmp, skill_vocabulary=vocab))
        for i, sk in enumerate(skills):
            s.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                      "params": {"name": "circe_trust_update", "arguments": {
                          "caller": "agent-A", "skill": sk, "success": True,
                          "latency_ms": 100,
                          "timestamp": (now - timedelta(minutes=len(skills) - i)
                                        ).isoformat()}}})
        q = s.handle({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                      "params": {"name": "circe_core_profile_query", "arguments": {}}})
        return s, json.loads(q["result"]["content"][0]["text"])

    sA, dA = _build(None, skills_a, tmp_a)
    sB, dB = _build(frozenset({"问答"}), skills_a, tmp_a / "b")
    sC, dC = _build(frozenset({"问答"}), skills_a + ["n4", "n5"], tmp_a / "c")

    spanA, spanB, spanC = (d["profile"]["span"] for d in (dA, dB, dC))
    check("不传词表 = 旧行为（每个噪声都算一种技能）",
          len(dA["profile"]["skills"]) == 4, str(dA["profile"]["skills"]))
    check("传词表 → 词表外的都归到一个桶",
          len(dB["profile"]["skills"]) == 2, str(dB["profile"]["skills"]))
    check("**归一把 skill 噪声对 span 的抬高掐掉了**（spanA > spanB）",
          spanA > spanB, f"spanA={spanA} spanB={spanB}")
    check("**再编一个新名字，span 一点都不动**（spanB == spanC）",
          spanB == spanC, f"spanB={spanB} spanC={spanC}")
    check("**账本保真**：账本里仍是调用方原话，不是归一后的桶名",
          {e.profile["skill"] for e in sB.impl.state.ledger.entries} == set(skills_a),
          str(sorted({e.profile["skill"] for e in sB.impl.state.ledger.entries})))

    desc = next(t for t in srv.tools.values()
                if t["name"] == "circe_trust_update")["inputSchema"]["properties"]
    check("skill 的 description 交代了「同一类任务要用同一个名字、不确定用 unspecified」",
          "unspecified" in desc["skill"]["description"]
          and "span" in desc["skill"]["description"])
    check("timestamp 的 description 交代了「必须由宿主填、会被服务端核对」",
          "宿主" in desc["timestamp"]["description"]
          and "拒绝" in desc["timestamp"]["description"])

    import shutil
    shutil.rmtree(tmp_ts, ignore_errors=True)
    shutil.rmtree(tmp_a, ignore_errors=True)

    # ---- ⑨ MCP 2026-07-28：新规范的三条 + 一条错误码 ----
    # 上游 changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog.md
    # 这里测的是**新规范要求的东西**，与上面那些旧握手断言并存——
    # 因为兼容层的存在理由就是要让两代客户端都能连。
    print("\n  ⑨ MCP 2026-07-28（新规范）")

    r = srv.handle({"jsonrpc": "2.0", "id": 40, "method": "server/discover"})
    check("server/discover 已实现（新规范 MUST）", "result" in r)
    d = r["result"]
    check("discover 宣告支持的版本列表",
          isinstance(d.get("protocolVersions"), list) and d["protocolVersions"],
          ", ".join(d.get("protocolVersions", [])))
    check("discover 里含 2026-07-28",
          "2026-07-28" in d.get("protocolVersions", []))
    check("discover 宣告 capabilities 与 serverInfo",
          "capabilities" in d and "serverInfo" in d)

    r = srv.handle({"jsonrpc": "2.0", "id": 41, "method": "tools/list"})
    check("结果带 resultType（新规范 MUST）",
          r["result"].get("resultType") == "complete",
          r["result"].get("resultType"))

    r = srv.handle({"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                    "params": {"name": "circe_readiness_probe", "arguments": {}}})
    check("tools/call 的结果也带 resultType",
          r["result"].get("resultType") == "complete")
    meta = r["result"].get("_meta", {})
    check("结果 _meta 里有 serverInfo（新规范 SHOULD）",
          "io.modelcontextprotocol/serverInfo" in meta, str(list(meta)))

    # 新客户端：版本走 _meta，不靠握手
    r = srv.handle({"jsonrpc": "2.0", "id": 43, "method": "tools/call",
                    "params": {"name": "circe_readiness_probe", "arguments": {},
                               "_meta": {
                                   "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                   "io.modelcontextprotocol/clientInfo":
                                       {"name": "new-client", "version": "1"}}}})
    check("新客户端（版本在 _meta 里、无握手）可直接调用",
          r["result"]["isError"] is False)
    check("响应 _meta 回显了协商后的版本",
          r["result"]["_meta"].get("io.modelcontextprotocol/protocolVersion")
          == "2026-07-28",
          r["result"]["_meta"].get("io.modelcontextprotocol/protocolVersion"))

    r = srv.handle({"jsonrpc": "2.0", "id": 44, "method": "tools/list",
                    "params": {"_meta": {
                        "io.modelcontextprotocol/protocolVersion": "1999-01-01"}}})
    check("不支持的版本返回 UnsupportedProtocolVersionError",
          "error" in r and "UnsupportedProtocolVersion" in r["error"]["message"],
          r.get("error", {}).get("message", ""))
    check("错误里带出支持的版本列表",
          "supported" in r.get("error", {}).get("data", {}))

    # ---- ⑩ 两代并存：同一份工具定义对两代客户端都可用 ----
    print("\n  ⑩ 兼容性：旧客户端仍然工作")
    r = srv.handle({"jsonrpc": "2.0", "id": 50, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}})
    check("旧握手仍应答，且回客户端要的那个版本",
          r["result"]["protocolVersion"] == "2025-06-18",
          r["result"]["protocolVersion"])
    # ⚠️ 下面这条是**用 recording proxy 抓真实客户端时发现的**：
    # Claude Code 2.1.296 先发 server/discover（新），再发 initialize
    # 且 params.protocolVersion = 2025-11-25。而初版的 _ok() 只从 _meta 读版本，
    # 于是同一个响应里 result.protocolVersion=2025-11-25 而 _meta 里是 2025-06-18。
    # **一个响应不该对同一个量给出两个答案。**
    r = srv.handle({"jsonrpc": "2.0", "id": 53, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"}})
    body_v = r["result"]["protocolVersion"]
    meta_v = r["result"].get("_meta", {}).get(KEY_VERSION)
    check("旧握手的 result 与 _meta 版本一致（真客户端抓出的 bug）",
          body_v == meta_v == "2025-11-25",
          f"result={body_v}  _meta={meta_v}")

    r = srv.handle({"jsonrpc": "2.0", "id": 51, "method": "ping"})
    check("旧 ping 仍应答（新规范已移除，保留为兼容）", r["result"] == {} or
          r["result"].get("resultType") == "complete")
    r = srv.handle({"jsonrpc": "2.0", "id": 52, "method": "tools/list"})
    n_new = len(r["result"]["tools"])
    check("新旧客户端拿到同一份工具表", n_new == 4, f"{n_new} 个")

    # ---- ⑪ 真实客户端 Claude Code 2.1.296 的实际序列（用 proxy 抓到的）----
    print("\n  ⑪ 按真实客户端抓到的序列重放一遍")
    # 序列来源：tools/mcp_recording_proxy.py 捕获的 C→S 方向
    seq = [
        # ① 新协议探测
        ({"jsonrpc": "2.0", "id": "server-discover-probe-1",
          "method": "server/discover",
          "params": {"_meta": {
              "io.modelcontextprotocol/protocolVersion": "2026-07-28",
              "io.modelcontextprotocol/clientInfo":
                  {"name": "claude-code", "version": "2.1.296"}}}},
         "server/discover 被应答", lambda r: "protocolVersions" in r.get("result", {})),
        # ② 回退到旧握手（版本 2025-11-25）
        ({"jsonrpc": "2.0", "id": 0, "method": "initialize",
          "params": {"protocolVersion": "2025-11-25"}},
         "回退 initialize 被应答", lambda r: r.get("result", {}).get("protocolVersion")
         == "2025-11-25"),
        # ③ 旧协议的通知
        ({"jsonrpc": "2.0", "method": "notifications/initialized"},
         "通知不产生响应", lambda r: r is None),
        # ④ 取工具表
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
         "取到四个工具", lambda r: len(r.get("result", {}).get("tools", [])) == 4),
    ]
    for msg, label, pred in seq:
        r = srv.handle(msg)
        ok = pred(r)
        check(label, ok,
              "" if ok else f"实际 {json.dumps(r, ensure_ascii=False)[:120]}")

    print()
    print("=" * 70)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
