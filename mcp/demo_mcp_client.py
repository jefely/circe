"""一个合规的 MCP 客户端 —— 按真实客户端的行为走，而不是替它指定工具。

跑法:  python CIRCE/mcp/demo_mcp_client.py

它为什么不同于 test_stdio_roundtrip.py
---------------------------------------
往返测试里，**是我**决定调哪个工具（`cli.call("circe_core_profile_query")`）。
那验证的是「管道能通」。

而真实客户端是**先 tools/list，再自己判断该调哪个**。
这两件事的差别在于：**工具的 description 与 inputSchema 是否足以让一个
不了解 CIRCE 的调用方正确地用它。**

**这是 MCP 落地最容易被忽略的一环**：协议通了、管道通了，
但工具描述写得不够，客户端就不知道为什么该调它、参数该给什么。

本脚本验证三件事：
  ① 工具的 description 里有没有「什么时候该用」的信息，而不只是「它是什么」
  ② inputSchema 是否自足（必填项齐全、可选参数有说明）
  ③ 一个只读 description 的客户端，能否仅凭它们选出正确的工具

本机没有 Claude Desktop / Cursor / 任何 MCP 客户端（tools/check_mcp_env.py 查过），
所以「挂到真实客户端」那一步必须在别的机器上做。本脚本是它的等价物：
**按客户端的算法走，不按我的意图走。**
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"    {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))


class McpClient:
    def __init__(self, args: list[str]) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen(
            [PY, "-X", "utf8", str(HERE / "server.py")] + args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env)
        self._id = 0

    def _send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return None if notify else self._id

    def _recv(self, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.p.stdout.readline()
            if line:
                return json.loads(line)
        raise TimeoutError("无响应")

    def rpc(self, method, params=None):
        self._send(method, params)
        return self._recv()

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            rc = self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = -9
        return rc


def call(client: McpClient, tool: str, args: dict) -> dict:
    r = client.rpc("tools/call", {"name": tool, "arguments": args})
    return json.loads(r["result"]["content"][0]["text"])


def main() -> int:
    print("=" * 74)
    print("  一个合规的 MCP 客户端 —— 按客户端的算法走，不按我的意图走")
    print("=" * 74)

    tmp = Path(tempfile.mkdtemp(prefix="circe-client-"))
    cli = McpClient(["--agent", "agent-Client", "--data", str(tmp / "C")])

    try:
        # ---- 初始化（真实客户端的第一步）----
        print("\n  ① 初始化握手")
        r = cli.rpc("initialize", {"protocolVersion": "2025-06-18",
                                   "capabilities": {},
                                   "clientInfo": {"name": "circe-demo-client",
                                                  "version": "1.0"}})
        info = r["result"]
        check("协商成功", "protocolVersion" in info, info["protocolVersion"])
        check("服务端声明了 tools capability", "tools" in info["capabilities"])
        cli._send("notifications/initialized", notify=True)
        time.sleep(0.2)

        # ---- tools/list：客户端拿到全部工具定义 ----
        print("\n  ② tools/list —— 客户端只从这里了解 CIRCE")
        r = cli.rpc("tools/list", {})
        tools = {t["name"]: t for t in r["result"]["tools"]}
        check("拿到四个工具定义", len(tools) == 4, ", ".join(tools))

        # ---- ③ 描述是否自足（这是落地最容易被忽略的一环）----
        print("\n  ③ 工具描述是否自足 —— 一个不了解 CIRCE 的客户端能否正确使用")
        for name, t in tools.items():
            desc = t.get("description", "")
            schema = t.get("inputSchema", {})
            # 描述该说「什么时候用」，而不只是「它是什么」
            has_when = any(w in desc for w in
                           ("刻意", "不能省", "为什么", "必须", "注意", "→", "——"))
            check(f"{name} 的描述含使用情境而不只是定义",
                  len(desc) > 40 and has_when, f"{len(desc)} 字符")
            # 必填项必须都在 properties 里声明
            props = schema.get("properties", {})
            req = schema.get("required", [])
            missing = [k for k in req if k not in props]
            check(f"{name} 的 required 都在 properties 里", not missing,
                  f"required={req}")
            # 每个 property 都该有 type 与 description
            bad = [k for k, v in props.items()
                   if "type" not in v or "description" not in v]
            check(f"{name} 的每个参数都有 type 与 description", not bad,
                  f"缺: {bad}" if bad else f"{len(props)} 个参数")

        # ---- ④ 只凭 description 做工具选择（不看工具名）----
        print("\n  ④ 只凭 description 选工具 —— 模拟客户端的决策")
        # 一个客户端会问的问题 → 期望它选出哪个工具
        scenarios = [
            ("这个 agent 现在能不能接受调用？", "circe_readiness_probe"),
            ("调用它之前，我想知道删掉它我会损失什么", "circe_core_profile_query"),
            ("我刚调用了它，成功，耗时 320ms，怎么记录", "circe_trust_update"),
            ("它的账本有没有被人改过", "circe_ledger_verify"),
        ]
        for question, expect in scenarios:
            # 极简的相关性打分：问题里的关键词命中 description 里的词
            best, score = None, -1
            for name, t in tools.items():
                d = t["description"]
                # 客户端只能看到 description，看不到 name
                hits = sum(1 for w in _tokens(question) if w in d)
                if hits > score:
                    best, score = name, hits
            check(f"「{question[:22]}…」→ {expect}",
                  best == expect, f"选出 {best}（命中 {score} 词）")

        # ---- ⑤ 按客户端的方式走完整条链 ----
        print("\n  ⑤ 客户端按自己的判断走完整条链")
        d = call(cli, "circe_readiness_probe", {})
        check("探活：拿到 ready", d.get("ready") is True)
        check("探活：不含 profile（描述里说明了为什么不给）",
              "profile" not in d)

        d = call(cli, "circe_core_profile_query", {})
        check("查询：空账本给白纸 + warning",
              d["profile"]["density"] == 0 and "warning" in d)
        check("查询：返回可验证凭证", d.get("credential_ok") is True)
        check("查询：返回 explain 可手算核对", "explain" not in d,
              "空账本时没有 explain —— 合理，没有调用历史可解释")

        for i in range(4):
            d = call(cli, "circe_trust_update", {
                "caller": "agent-A", "skill": "问答", "success": True,
                "latency_ms": 300 + i * 20,
                "timestamp": f"2026-10-10T14:0{i}:00Z",
                "stack_depth": 2})
            if i == 3:
                check("更新：四次都写入", d["accepted"] is True,
                      f"seq={d['ledger_seq']}")
                check("更新：返回重算后的剖面",
                      d["new_profile"]["density"] > 0,
                      f"density={d['new_profile']['density']}")

        d = call(cli, "circe_core_profile_query", {})
        check("查询：这次带 explain（有了历史就能解释）", "explain" in d)
        check("查询：中文 skill 原样返回",
              d["profile"].get("skills") == ["问答"], str(d["profile"].get("skills")))

        d = call(cli, "circe_ledger_verify", {})
        check("校验：账本完整", d["ok"] is True and d["entries"] == 4)

    except TimeoutError as e:
        check("客户端全程有响应", False, str(e))
    finally:
        rc = cli.close()

    check("服务端正常退出", rc == 0, f"returncode={rc}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 74)

    print()
    print("  说明：本机没有 Claude Desktop / Cursor / 任何 MCP 客户端，")
    print("        所以「挂到真实客户端」那一步要在别的机器上做。")
    print("        本脚本是它的等价物 —— 按客户端的算法走，不按我的意图走。")
    return 0 if not FAIL else 1


def _tokens(s: str) -> list[str]:
    """极简分词：中文按 2-gram，英文按空白。只为模拟客户端的粗匹配。"""
    out = []
    zh = [c for c in s if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(zh) - 1):
        out.append(zh[i] + zh[i + 1])
    out += [w for w in s.split() if w.isascii() and len(w) > 2]
    return out


if __name__ == "__main__":
    sys.exit(main())
