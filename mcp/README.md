# CIRCE 的 MCP 服务器

**让任何支持 MCP 的 AI 都能查询核剖面，而无需知道 CIRCE 是什么。**

---

## 为什么挂 MCP，而不是让别人实现我的协议

CIRCE 原本有自己的三个端点与自己的 HTTP 实现。但那意味着：
**想让别的 AI 调用 CIRCE，就得让它先学会 CIRCE 的协议。**

而生态正在用 MCP。**所以正确的位置不是"成为一个新协议"，而是"成为现有协议上的一个服务"。**

```
改写前：别的 AI  →  学 CIRCE 的协议  →  查核剖面
改写后：别的 AI  →  MCP（已经会）    →  查核剖面
```

---

## 四个工具

| MCP tool | 回答什么 | 为什么不合并 |
|---|---|---|
| **`circe_readiness_probe`** | 「这个 agent 现在能接活吗」 | **刻意不返回核剖面**——探活要能高频调用，返回剖面会让每次探活都背上签名与账本写入的代价 |
| **`circe_core_profile_query`** | 「删掉它，调用方会失去什么」 | **CIRCE 的核心问题**。返回剖面 + VC + 链尾 + 可选解释串 |
| **`circe_trust_update`** | 「我刚调用过它，结果是这样」 | 有状态写入，**由调用方驱动** |
| **`circe_ledger_verify`** | 「它的账本被改过吗」 | **这一步不能省**：链尾只锚最后一条，改中间某条而不重算时链尾不变 |

**`readiness_probe` 与 `core_profile_query` 必须分开**：探活便宜、要高频；
把它做成「顺便返回核剖面」会污染每一次探活。

**`ledger_verify` 是从真实交互里长出来的工具**——见 `README.md` §七 洞二：
签发凭据前必须先验账本，否则会签出一份「隔着被篡改的历史仍验签通过」的凭据。

---

## 配置

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "circe": {
      "command": "python",
      "args": [
        "D:/myproject/CIRCE/mcp/server.py",
        "--agent", "agent-B",
        "--data", "D:/myproject/CIRCE/mcp/data/agent-B"
      ]
    }
  }
}
```

### Cursor

`%USERPROFILE%\.cursor\mcp.json` —— 同样的 `mcpServers` 结构。

### 任何 MCP 宿主

只要它能启动一个 stdio 子进程并说 JSON-RPC，就能接。

**`--data` 指向账本与密钥的存放目录**。**同一个 agent 必须始终指向同一个目录**——
账本是真相来源，指向别的目录等于换了一个 agent。

---

## 零依赖

本服务器**只用 Python 标准库**。本机查过（`tools/check_mcp_env.py`）：

```
Python mcp 包    无
Node SDK         无
```

**而这不构成障碍**：MCP 的 stdio 传输就是「一行一个 JSON-RPC 2.0 消息」。
自己实现不到 100 行，而**少一层依赖意味着别人的环境里少一个装不上的东西**。

**这个取舍与 CIRCE 其它部分一致**：`protocol/server.py` 用 stdlib 的 `http.server`
而不是 Flask/FastAPI，理由相同——
**依赖越重，「这条通道能通」这个结论就越依赖环境，越不像在证明协议本身。**

---

## 已验证

```
python CIRCE/mcp/server.py --selftest                          66 / 0
python CIRCE/mcp/test_stdio_roundtrip.py                       23 / 0
python CIRCE/mcp/test_ollama_agent.py --model qwen2.5:7b       23 / 0
python CIRCE/mcp/test_ollama_agent.py --model qwen3-vl:8b      23 / 0
```

> `--selftest` 从 52 增至 66：+13 是 2026-10-10 为 F1/F3 补的拒绝路径断言，
> +1 是 2026-10-11 为**工具名可移植性**补的护栏（见下 §"一个真实客户端"）
> （未来时间戳必须被拒且**账本不增长**、skill 噪声不得抬高 `span`）——
> 见 [`FINDINGS.md`](FINDINGS.md) §二之补。

**为什么三个都要跑**：它们各答一半，**中间缺的那一格是第三个**。

| 套件 | 它证明了什么 | 它证明不了什么 |
|---|---|---|
| `--selftest`（**进程内**，直接调分发函数） | 我的分发逻辑对 | **没经过管道** |
| `test_stdio_roundtrip.py`（真的起子进程、真的走管道） | 管道能通、中文不乱码、stdout 纯净 | **调哪个工具是我指定的** |
| `test_ollama_agent.py`（真的 LLM 在读 description） | **一个 LLM 只凭四个 description 能选对工具**，以及模型→MCP→账本→重算整条链 | **不是 Claude**——模型换掉结论要重测 |

stdio 传输有一批只在真实管道里才暴露的问题：

```
· stdout 混入非协议输出（print 调试、库的 banner）→ 客户端解析失败
· 没有 flush                                    → 客户端永远等不到响应
· 行缓冲/编码问题                                → 中文乱码
· 子进程启动失败但父进程不知道
```

所以 `test_stdio_roundtrip.py` **真的起子进程、真的走管道**，并检查：

```
① initialize 协商 protocolVersion
② tools/list 四个工具
③ 三个工具各走一次真实往返；凭据的 base64url 在管道里没被截断
④ 中文 skill 原样读回（skills=['问答']）—— 过了一整趟 Python→管道→Python
⑤ 自动增量在管道里也成立（洞四的回归检查）
⑥ 错误路径不做死服务器（缺参数返回 isError，之后仍能应答）
⑦ stdout 纯净（调试输出走 stderr）；进程正常退出
```

**第三个套件为什么必须存在**：`demo_mcp_client.py` 的「选工具」用的是脚本自己写的
**关键词打分**（它自己的注释：「极简的相关性打分」）——**那不是一个模型在读 description**。
于是「工具的 description 是否足以让调用方正确使用」这个问题**从来没被真的问过**。
`test_ollama_agent.py` 把这个问题交给一个真的 LLM，并且**当场抓到了三条
CIRCE 自己的字段设计问题**——见 [`FINDINGS.md`](FINDINGS.md) §二。

**这与 CIRCE 里那几条教训同形**：

> **一个层次上做了正确的事，但这个正确性依赖于另一个层次上没有被保证的东西。**
> 分发逻辑对 ≠ 管道能通。管道能通 ≠ 工具选得对。工具选得对 ≠ **字段填得对**。

---

## 一处设计选择：调用方的判断不进链

`circe_core_profile_query` 返回的是**统计量与凭据**，不是建议。

**要判断「是否该依赖它」，那是调用方模型自己的事**——
它可以用 LLM 读这份剖面、权衡、决定，**而那一步不进链、不进 hash**。

账本里只有事实：「谁在什么时候调用了谁，结果如何」。
**这正是哈希链只需要确定性输入的原因。** 见 `README.md` §五之二。
