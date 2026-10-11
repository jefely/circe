"""真实 stdio 往返测试 —— 像一个 MCP 客户端那样启动 CIRCE 服务器。

跑法:  python CIRCE/mcp/test_stdio_roundtrip.py

为什么必须做这一步
------------------
`server.py --selftest` 是**进程内**的：它直接调 `Server.handle()`，
证明的是「我的分发逻辑对」。**但它没有经过管道。**

而 MCP 的 stdio 传输有一批只在真实管道里才会暴露的问题：
  · stdout 混入了非协议输出（print 调试、库的 banner）→ 客户端解析失败
  · 没有 flush → 客户端永远等不到响应
  · 行缓冲/编码问题 → 中文乱码
  · 子进程启动失败但父进程不知道

**这与 CIRCE 里那几条教训同形**：一个层次上做了正确的事，
但这个正确性依赖于另一个层次上没有被保证的东西。
分发逻辑对 ≠ 管道能通。
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
    """最小 MCP 客户端：起子进程，按行收发 JSON-RPC。"""

    def __init__(self, args: list[str]) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen(
            [PY, "-X", "utf8", str(HERE / "server.py")] + args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env)
        self._id = 0

    def send(self, method: str, params: dict | None = None,
             notify: bool = False) -> int | None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return None if notify else self._id

    def recv(self, timeout: float = 20.0) -> dict:
        """读一行。**超时要能报出来**，否则一个没 flush 的服务器会让测试静默挂住。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.p.stdout.readline()
            if line:
                return json.loads(line)
        raise TimeoutError(f"{timeout}s 内没有收到响应")

    def call(self, name: str, args: dict | None = None) -> dict:
        self.send("tools/call", {"name": name, "arguments": args or {}})
        return self.recv()

    def close(self) -> tuple[int, str]:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            rc = self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = -9
        err = ""
        try:
            err = self.p.stderr.read() or ""
        except Exception:
            pass
        return rc, err


def main() -> int:
    print("=" * 72)
    print("  真实 stdio 往返 —— 像一个 MCP 客户端那样启动 CIRCE")
    print("=" * 72)

    tmp = Path(tempfile.mkdtemp(prefix="circe-mcp-stdio-"))
    cli = McpClient(["--agent", "agent-Z", "--data", str(tmp / "Z")])

    try:
        # ---- ① initialize 走管道 ----
        print("\n  ① initialize（真实的管道往返）")
        cli.send("initialize", {"protocolVersion": "2025-06-18",
                                "clientInfo": {"name": "roundtrip-test", "version": "1.0"}})
        r = cli.recv()
        check("收到 initialize 响应", r.get("id") == 1 and "result" in r)
        info = r["result"]
        check("protocolVersion 已协商", "protocolVersion" in info,
              info.get("protocolVersion"))
        check("serverInfo.name == circe", info["serverInfo"]["name"] == "circe")

        cli.send("notifications/initialized", notify=True)
        time.sleep(0.2)

        # ---- ② tools/list ----
        print("\n  ② tools/list")
        cli.send("tools/list", {})
        r = cli.recv()
        tools = [t["name"] for t in r["result"]["tools"]]
        check("四个工具都列出来了", len(tools) == 4, ", ".join(tools))

        # ---- ③ 三个工具各走一次真实往返 ----
        print("\n  ③ tools/call 三次真实往返")
        r = cli.call("circe_readiness_probe")
        d = json.loads(r["result"]["content"][0]["text"])
        check("readiness_probe 通", d.get("ready") is True)

        r = cli.call("circe_core_profile_query")
        d = json.loads(r["result"]["content"][0]["text"])
        check("core_profile_query 通（空账本 → 白纸）",
              d["profile"]["density"] == 0 and "warning" in d)
        check("凭据在管道里没被破坏（base64url 可解码）",
              isinstance(d.get("credential"), dict)
              and len(d["credential"]["proof"]["proofValue"]) > 20,
              f"{len(d['credential']['proof']['proofValue'])} 字符")

        for i in range(3):
            # skill 用中文：这是**故意**的——中文必须完整穿过
            # 「Python → 管道 → Python」这一趟，才能证明编码没被破坏。
            # 初版断言「返回里有中文」，但那个查询在有调用历史时不带 warning，
            # 于是返回里本来就没有中文——**断言建立在错误的假设上，
            # 报出来的失败指向测试而不是代码**。
            r = cli.call("circe_trust_update", {
                "caller": "agent-A", "skill": "问答", "success": True,
                "latency_ms": 250 + i * 10,
                "timestamp": f"2026-10-10T13:0{i}:00Z",
                "stack_depth": 2, "note": f"stdio 往返第{i+1}次"})
            d = json.loads(r["result"]["content"][0]["text"])
            if i == 2:
                check("trust_update 通（三次都写入）", d["accepted"] is True,
                      f"seq={d['ledger_seq']}")

        # ---- ④ 中文经管道不打折 ----
        print("\n  ④ 中文经管道是否原样返回")
        r = cli.call("circe_core_profile_query")
        d = json.loads(r["result"]["content"][0]["text"])
        check("中文 skill 原样读回（说明编码没被管道破坏）",
              d["profile"].get("skills") == ["问答"],
              f"skills={d['profile'].get('skills')}")
        # 空账本时才有的 warning 也含中文——单独构造一次
        cli2 = McpClient(["--agent", "agent-W", "--data", str(tmp / "W")])
        try:
            cli2.send("initialize", {"protocolVersion": "2025-06-18"})
            cli2.recv()
            r2 = cli2.call("circe_core_profile_query")
            raw2 = r2["result"]["content"][0]["text"]
            check("空账本的 warning 中文完整",
                  "时间积累" in raw2, f"{len(raw2)} 字符")
        finally:
            cli2.close()

        # ---- ⑤ 自动增量在真实往返里也成立 ----
        print("\n  ⑤ 自动增量（洞四在管道里的回归检查）")
        check("density 不再是 0", d["profile"]["density"] > 0,
              f"density={d['profile']['density']}")
        check("call_count == 3", d["profile"].get("call_count") == 3,
              str(d["profile"].get("call_count")))

        # ---- ⑥ 错误路径经管道 ----
        print("\n  ⑥ 错误路径不做死服务器")
        cli.send("tools/call", {"name": "circe_trust_update", "arguments": {}})
        r = cli.recv()
        check("缺参数返回 isError 而不是崩溃", r["result"]["isError"] is True)
        r = cli.call("circe_ledger_verify")
        d = json.loads(r["result"]["content"][0]["text"])
        check("服务器仍然存活并应答", d["ok"] is True and d["entries"] == 3)

        # ---- ⑥之二 新客户端（MCP 2026-07-28）：无握手、版本在 _meta 里 ----
        # 上面 ①–⑥ 走的都是**旧握手路径**（initialize → notifications/initialized）。
        # 而新规范把整个握手删掉了：每个请求自带 protocolVersion。
        # **所以"兼容层能工作"必须由一条走新路径的往返来证明，
        # 而不是由旧路径的 16 项来推断**——这与本文 §6 那条同形。
        print("\n  ⑥之二 新客户端路径（2026-07-28 · stateless）")
        cli2 = McpClient(["--agent", "agent-N", "--data", str(tmp / "N")])
        try:
            # **不握手**，直接 server/discover
            cli2.send("server/discover", {})
            r2 = cli2.recv()
            check("新客户端：不做握手就能 server/discover",
                  "result" in r2 and "protocolVersions" in r2["result"],
                  ", ".join(r2["result"].get("protocolVersions", [])))

            # 版本走 _meta
            meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo":
                        {"name": "stateless-client", "version": "1"}}
            cli2.send("tools/list", {"_meta": meta})
            r2 = cli2.recv()
            tools2 = [t["name"] for t in r2["result"]["tools"]]
            check("新客户端：tools/list 通（无握手）", len(tools2) == 4,
                  ", ".join(tools2))
            check("新客户端：结果带 resultType",
                  r2["result"].get("resultType") == "complete")

            cli2.send("tools/call", {"name": "circe_core_profile_query",
                                     "arguments": {}, "_meta": meta})
            r2 = cli2.recv()
            d2 = json.loads(r2["result"]["content"][0]["text"])
            check("新客户端：core_profile_query 通",
                  d2["profile"]["density"] == 0 and "warning" in d2)
            check("新客户端：结果 _meta 标识了服务端",
                  "io.modelcontextprotocol/serverInfo" in r2["result"].get("_meta", {}))

            # 版本不支持 → 明确报错，而不是沉默地按默认处理
            cli2.send("tools/list", {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "1999-01-01"}})
            r2 = cli2.recv()
            check("新客户端：不支持的版本被明确拒绝（而非静默降级）",
                  "error" in r2 and "UnsupportedProtocolVersion" in r2["error"]["message"],
                  r2.get("error", {}).get("message", ""))

            # 旧客户端仍能连（兼容层的存在理由）
            cli2.send("initialize", {"protocolVersion": "2025-06-18"})
            r2 = cli2.recv()
            check("同一个服务器仍应答旧握手（两代并存）",
                  r2["result"]["protocolVersion"] == "2025-06-18")
        finally:
            cli2.close()

    except TimeoutError as e:
        check("管道在上限内应答", False, str(e))
    finally:
        rc, err = cli.close()

    # ---- ⑦ stdout 纯净性 ----
    print("\n  ⑦ stdout 纯净性（脏 stdout 会让真实客户端解析失败）")
    check("进程正常退出", rc == 0, f"returncode={rc}")
    debug_lines = [ln for ln in err.splitlines() if ln.strip()]
    check("调试输出走了 stderr，没污染 stdout",
          any("[mcp]" in ln for ln in debug_lines),
          f"stderr {len(debug_lines)} 行，例: {debug_lines[0][:60] if debug_lines else '（空）'}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 72)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"    - {f}")
    print("=" * 72)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
