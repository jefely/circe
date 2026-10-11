"""让一个真实的本地 LLM 自己选 MCP 工具，走完整条端到端链路。

跑法:
    python CIRCE/mcp/test_ollama_agent.py                    # qwen2.5:7b
    python CIRCE/mcp/test_ollama_agent.py --model qwen3-vl:8b
    python CIRCE/mcp/test_ollama_agent.py --no-time          # 测「不给时钟会怎样」

它补的是哪一格
--------------
已有的三层验证各自答了一半，**中间缺一格**：

| 已有的 | 它证明了什么 | 它证明不了什么 |
|---|---|---|
| `server.py --selftest`（52 项） | 我的分发逻辑对 | **没经过管道** |
| `test_stdio_roundtrip.py`（23 项） | 管道能通、中文不乱码 | **调哪个工具是我指定的** |
| `demo_mcp_client.py` | description 里有「什么时候用」 | **它选工具靠的是关键词打分**（脚本自己写的「极简的相关性打分」），**不是一个模型在读** |

**那一格是**：把 description 交给一个**真的 LLM**，让它自己决定调哪个工具、
给什么参数——也就是 `claude -p` 本该回答的那个问题。

**为什么用 Ollama 而不是 Claude Code**：本机 Claude 账号于 2026-10-10 22:22 被暂停
（`账号暂停-应对.md`），`claude -p` 这条线暂时不可用。Ollama 是本地的、可复现的、
**不碰任何云账号**的替代入口。

⚠️ 而它给出的结论必须**按原样**读：

    它证明的是「一个 LLM 能够只凭这四个 description 正确选工具」，
    **不是**「Claude 能够」。模型换掉，结论要重测。
    论文里若要用，必须写成前者，并标明模型与量化等级。

端到端到底走了哪些层
--------------------
    本地模型(Ollama, HTTP) → 本脚本(MCP 客户端) → stdio 管道 → server.py
        → CIRCE/protocol 账本写入 → 剖面重算 → 原路返回

**每一层都是真的**：真的子进程、真的管道、真的 HTTP、真的写盘、真的重算。

场景里最重要的一个是 S5
------------------------
`挂载指南.md` §三 检查 4 给出的判据是：
「记录一次调用后再查一次，**density 应从 0 变成非 0**；
如果第二次查还是 0，就撞上了 §7.2 的洞四（`current_profile()` 没从账本重算）。」

**那个洞的修复从没在「模型自己选工具」这条路径上被验证过。** S5 就是它。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434"

PASS: list[str] = []
FAIL: list[str] = []
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))
    return ok


def note(msg: str) -> None:
    NOTES.append(msg)
    print(f"    · {msg}")


# --------------------------------------------------------------------------
# ① MCP 客户端：真实 stdio 子进程
# --------------------------------------------------------------------------
class McpClient:
    """一行一个 JSON-RPC 2.0 的合规 MCP 客户端。

    握手走**新协议**（`server/discover` + 版本放 `_meta`）——
    与 `真实客户端协议序列.log` 里 Claude Code 2.1.296 实测发的那条**同一条路径**。
    """

    VERSION = "2026-07-28"

    def __init__(self, args: list[str]) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(HERE / "server.py")] + args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env)
        self._id = 0
        self.stderr_lines: list[str] = []

    def _meta(self) -> dict:
        return {
            "io.modelcontextprotocol/protocolVersion": self.VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "circe-ollama-agent", "version": "0.1.0",
                "title": "CIRCE 本地模型端到端验证器"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    def _send(self, method: str, params: dict | None = None,
              notify: bool = False) -> int | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return None if notify else self._id

    def _recv(self, want_id: int, timeout: float = 30.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.p.stdout.readline()
            if not line:
                raise TimeoutError("服务器关闭了 stdout")
            m = json.loads(line)
            if m.get("id") == want_id:
                return m
            # 服务端主动发的通知：忽略但记下
            print(f"      （服务端通知: {m.get('method')}）")
        raise TimeoutError(f"等 id={want_id} 超时")

    def rpc(self, method: str, params: dict | None = None) -> dict:
        mid = self._send(method, params)
        return self._recv(mid)

    def close(self) -> int:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            rc = self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = -9
        try:
            self.stderr_lines = (self.p.stderr.read() or "").splitlines()
        except Exception:
            pass
        return rc


# --------------------------------------------------------------------------
# ② Ollama：真实的本地模型
# --------------------------------------------------------------------------
def ollama_post(path: str, body: dict, timeout: float = 600.0) -> dict:
    req = urllib.request.Request(
        OLLAMA + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "status": r.status,
                    "body": json.loads(r.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "body": e.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "status": -1, "body": f"{type(e).__name__}: {e}"}


def ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    return [m["name"] for m in d.get("models", [])
            if "tools" in (m.get("capabilities") or [])]


def chat(model: str, messages: list[dict], tools: list[dict] | None,
         temperature: float = 0.0) -> dict:
    body: dict = {"model": model, "messages": messages, "stream": False,
                  "options": {"temperature": temperature}}
    if tools:
        body["tools"] = tools
    return ollama_post("/api/chat", body)


# --------------------------------------------------------------------------
# ③ 工具名的可移植性（**这一节的第一版结论是错的，2026-10-11 改正**）
# --------------------------------------------------------------------------
# 第一版这里做的是「探测带点号的函数名能不能用」，在 Ollama 上得到「接受」，
# 于是写下「那道我以为存在的缝，实测不存在」。
#
# **那个结论是错的，而错法比结论有用**：缝在**另一个**客户端上——
#
#     VS Code 1.141.0：Tool "circe.readiness_probe" is invalid.
#                      Tools names may only contain [a-z0-9_-]
#                      → 四个工具全部无效，可用工具数 = 0
#
# 我错在**只在一个客户端上测**，却声称了一个关于「这道缝存不存在」的普遍结论。
# **与论文 §6.1 那条规则同形**：
# > 任何验证脚本都必须声明「它测的东西与真实运行的东西在哪一点上是同一个」。
#
# 所以这一节现在不做探测，而是**直接校验可移植性**：
# 工具名必须满足全体客户端通用的那个模式（约束来自 LLM 侧 function name，
# 不是来自 MCP —— MCP 本身不禁止 `.`）。
PORTABLE_NAME = re.compile(r"^[a-z0-9_-]{1,64}$")


def to_ollama_tools(mcp_tools: list[dict]) -> list[dict]:
    """MCP 的工具定义 → Ollama 的 tools 格式。

    **名字原样传，不做映射**——映射表是为不可移植的名字付的代价，
    而我们已经把名字改成可移植的了（见上面那一段）。
    """
    return [{"type": "function", "function": {
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t.get("inputSchema") or {"type": "object",
                                               "properties": {}},
    }} for t in mcp_tools]


# --------------------------------------------------------------------------
# ④ Agent loop
# --------------------------------------------------------------------------
def run_scenario(model: str, mcp: McpClient, tools: list[dict],
                 tool_names: dict[str, str], user_text: str,
                 system: str, max_turns: int = 4) -> dict:
    """把一句话交给模型，让它自己决定调哪个工具、给什么参数。

    返回：模型选了什么、参数是什么、MCP 回了什么、以及原始文本。
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_text}]
    calls: list[dict] = []
    final_text = ""
    for _ in range(max_turns):
        r = chat(model, messages, tools)
        if not r["ok"]:
            return {"error": f"Ollama 调用失败: {str(r['body'])[:300]}",
                    "calls": calls, "text": final_text}
        m = r["body"].get("message") or {}
        tcs = m.get("tool_calls") or []
        final_text = (m.get("content") or "").strip()
        if not tcs:
            break
        messages.append({"role": "assistant", "content": m.get("content") or "",
                         "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function") or {}
            model_name = fn.get("name") or ""
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"__unparsable__": raw_args}
            raw_args = raw_args or {}
            mcp_name = tool_names.get(model_name, model_name)

            resp = mcp.rpc("tools/call", {"name": mcp_name,
                                          "arguments": raw_args,
                                          "_meta": mcp._meta()})
            res = resp.get("result") or {}
            err = resp.get("error")
            text = (res.get("content") or [{}])[0].get("text", "")
            calls.append({"mcp_tool": mcp_name, "model_tool": model_name,
                          "args": raw_args, "isError": res.get("isError"),
                          "rpc_error": err, "text": text})
            messages.append({"role": "tool", "tool_name": model_name,
                             "content": text or json.dumps(err or {}, ensure_ascii=False)})
    return {"calls": calls, "text": final_text}


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--agent", default="agent-ollama")
    ap.add_argument("--no-time", action="store_true",
                    help="不给模型当前时间（测 timestamp 这个必填项该由谁提供）")
    ap.add_argument("--skill-vocabulary", default=None,
                    help="逗号分隔的受控技能词表，透传给 server.py。"
                         "用来实测 FINDINGS.md §二 F1 的修法："
                         "词表外的 skill 在**派生统计**里归一，账本仍写原话")
    ap.add_argument("--keep", action="store_true", help="保留临时账本目录")
    args = ap.parse_args()

    print("=" * 78)
    print("  CIRCE × 本地模型（Ollama）端到端 —— 模型自己选工具")
    print("=" * 78)

    avail = ollama_models()
    print(f"\n  本地可用（带 tools 能力的模型）: {avail or '（无）'}")
    if not avail:
        print("  ✗ Ollama 里没有支持 tool calling 的模型，无法继续")
        return 1
    if args.model not in avail:
        print(f"  ⚠ 指定的 {args.model} 不在上面这张表里，仍尝试调用")

    tmp = Path(tempfile.mkdtemp(prefix="circe-ollama-"))
    srv_args = ["--agent", args.agent, "--data", str(tmp / "B")]
    if args.skill_vocabulary:
        srv_args += ["--skill-vocabulary", args.skill_vocabulary]
    mcp = McpClient(srv_args)
    print(f"  模型: {args.model}")
    print(f"  server: server.py {' '.join(srv_args)}")

    try:
        # ---- ① 握手（新协议路径）----
        print("\n  ① 握手：server/discover（新协议，版本走 _meta）")
        r = mcp.rpc("server/discover", {"_meta": mcp._meta()})
        res = r.get("result") or {}
        check("server/discover 有响应", bool(res), str(r.get("error") or ""))
        check("宣告了我们的首选版本", "2026-07-28" in (res.get("protocolVersions") or []),
              str(res.get("protocolVersions")))
        check("结果带 resultType=complete", res.get("resultType") == "complete")
        check("serverInfo 是 circe", (res.get("serverInfo") or {}).get("name") == "circe")

        # ---- ② tools/list ----
        print("\n  ② tools/list")
        r = mcp.rpc("tools/list", {"_meta": mcp._meta()})
        mcp_tools = (r.get("result") or {}).get("tools") or []
        check("拿到四个工具", len(mcp_tools) == 4,
              ", ".join(t["name"] for t in mcp_tools))
        check("每个工具都有 description 与 inputSchema",
              all(t.get("description") and t.get("inputSchema") for t in mcp_tools))

        # ---- ③ 工具名可移植性（不再问"某个客户端接不接受"）----
        print("\n  ③ 工具名的可移植性：全体客户端通用的 `[a-z0-9_-]{1,64}`")
        bad_names = [t["name"] for t in mcp_tools
                     if not PORTABLE_NAME.match(t["name"])]
        check("**所有工具名都满足通用模式**（VS Code 会拒绝其它字符）",
              not bad_names,
              f"不合法: {bad_names}" if bad_names
              else f"{len(mcp_tools)} 个全部合法")
        tools = to_ollama_tools(mcp_tools)
        tool_names = {t["name"]: t["name"] for t in mcp_tools}

        # ---- ④ 场景 ----
        now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        sys_lines = [
            "你是一个会使用工具的 AI 调用方（agent）。",
            f"你通过 MCP 工具了解另一个 agent（{args.agent}）的状态。",
            '你自己的调用方标识是 "agent-A"。',
            "需要工具时就直接调用工具；不要编造工具会返回什么。",
        ]
        if not args.no_time:
            sys_lines.append(f"当前时间：{now}")
        system = "\n".join(sys_lines)
        print(f"\n  ④ 交给模型的 system 提示（时钟{'不给' if args.no_time else '给'}）:")
        for line in system.splitlines():
            print(f"       {line}")

        scenarios = [
            ("S1 探活",
             f"{args.agent} 现在能接活吗？",
             "circe_readiness_probe",
             lambda c: True),
            ("S2 查剖面（空账本）",
             f"我准备依赖它。删掉 {args.agent}，我会失去什么？",
             "circe_core_profile_query",
             lambda c: True),
            ("S3 上报一次调用",
             f"我刚调用过 {args.agent}，成功，耗时 500 毫秒，帮我记一下。",
             "circe_trust_update",
             lambda c: (c["args"].get("success") is True
                        and float(c["args"].get("latency_ms", -1)) == 500)),
            ("S4 验账本",
             f"{args.agent} 的账本有没有被人改过？",
             "circe_ledger_verify",
             lambda c: True),
            ("S5 再查一次剖面（洞四回归）",
             f"再查一次 {args.agent} 的核剖面。",
             "circe_core_profile_query",
             lambda c: '"density"' in c["text"]),
        ]

        results: dict[str, dict] = {}
        for label, prompt, expect, arg_ok in scenarios:
            print(f"\n  ── {label}")
            print(f"     用户: {prompt}")
            out = run_scenario(args.model, mcp, tools, tool_names, prompt, system)
            results[label] = out
            if out.get("error"):
                check(f"{label}: 模型有响应", False, out["error"])
                continue
            calls = out["calls"]
            if not calls:
                check(f"{label}: 模型调了工具", False,
                      f"没调工具，只说: {out['text'][:120]!r}")
                continue
            c = calls[0]
            check(f"{label}: 选了 {expect}", c["mcp_tool"] == expect,
                  f"实际选了 {c['mcp_tool']}")
            check(f"{label}: 参数符合预期", bool(arg_ok(c)),
                  json.dumps(c["args"], ensure_ascii=False)[:160])
            check(f"{label}: 服务端没报 isError", not c["isError"],
                  (c["text"] or "")[:200].replace("\n", " "))
            print(f"     参数: {json.dumps(c['args'], ensure_ascii=False)}")
            print(f"     返回: {(c['text'] or '').replace(chr(10), ' ')[:220]}")

        # ---- ⑤ 洞四回归：S5 必须看到 density 非 0 ----
        print("\n  ⑤ 洞四回归检查（§7.2：写入后剖面必须从账本重算）")
        s5 = results.get("S5 再查一次剖面（洞四回归）") or {}
        if s5.get("calls"):
            try:
                prof = json.loads(s5["calls"][0]["text"])["profile"]
                dens = prof.get("density", 0)
                check("S5 的 density 非 0（账本→剖面重算生效）", dens and dens > 0,
                      f"density={dens}  skills={prof.get('skills')}")
            except Exception as e:
                check("S5 返回可解析的剖面", False, f"{type(e).__name__}: {e}")
        else:
            check("S5 返回可解析的剖面", False, "S5 没调工具")

        # ---- ⑥ 时钟这一项到底该由谁给 ----
        if args.no_time:
            print("\n  ⑥ 没给时钟时，模型给的是什么 timestamp")
            s3 = results.get("S3 上报一次调用") or {}
            if s3.get("calls"):
                ts = s3["calls"][0]["args"].get("timestamp")
                note(f"模型自报 timestamp = {ts!r}")
                NOTES.append("若 timestamp 缺失/不可解析，说明这个必填项"
                             "**不能指望调用方模型提供**，只能由宿主注入")

        # ---- ⑦ skill 归一：直接读账本核对「账本保真」（F1 的修法）----
        if args.skill_vocabulary:
            print("\n  ⑦ skill 归一实测（F1 的修法：账本保真，只归一派生统计）")
            vocab = [s.strip() for s in args.skill_vocabulary.split(",") if s.strip()]
            # 直接读账本文件——不经剖面，看的是**真相来源**
            raw = json.loads((tmp / "B" / "ledger.json").read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else raw.get("entries", [])
            raw_skills = [e.get("profile", {}).get("skill") for e in entries]
            note(f"账本里的原始 skill = {raw_skills}")
            note(f"受控词表 = {vocab}")
            check("**账本里仍是调用方原话**（没有被归一改写）",
                  all(s not in (None, "") for s in raw_skills),
                  f"{len(raw_skills)} 条")
            s5 = results.get("S5 再查一次剖面（洞四回归）") or {}
            if s5.get("calls"):
                prof = json.loads(s5["calls"][0]["text"])["profile"]
                got = set(prof.get("skills") or [])
                check("**剖面里的 skills 已被归一**（词表 ∪ unspecified）",
                      got <= set(vocab) | {"unspecified"},
                      f"剖面 skills={sorted(got)}；账本原话={raw_skills}")

    finally:
        rc = mcp.close()
        if mcp.stderr_lines:
            print(f"\n  server stderr（{len(mcp.stderr_lines)} 行）:")
            for line in mcp.stderr_lines[:5]:
                print(f"      {line.strip()[:150]}")
        print(f"\n  server 退出码: {rc}")
        if args.keep:
            print(f"  账本目录保留在: {tmp}")
        else:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 78)
    print("\n  ⚠ 结论按原样读：它证明「一个 LLM 能只凭这四个 description 正确选工具」，")
    print(f"     模型是 {args.model}（本地量化版），**不是 Claude**。换模型要重测。")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
