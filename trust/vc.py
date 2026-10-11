"""CIRCE 可验证凭证（VC）—— Ed25519 签发与验证。

设计原文（`CIRCE/design/情绪树-CIRCE原文.md`，消息 236 §3）：

    单纯的哈希链只能证明「没被改过」，但无法证明「这个核剖面是被某个权威认可的」。
    签发：由 CIRCE 系统（或第三方验证者）对核剖面进行签名，生成一个 VC。
          VC 中包含：主体（那个 Agent）、核剖面摘要、签发者、有效期。
    验证：任何其他 Agent 收到这个 VC 后，可以用签发者的公钥验证签名。

    「MD5 只能证明『数据没变』，VC 能证明『数据是谁说的，以及谁为它的真实性背书』。」

凭据结构（对齐 W3C VC 的核心字段，但不实现完整规范——原文明确允许「无需完整 W3C 规范，但结构对齐」）
------------------------------------------------------------------------------------------------
    {
      "@context": ["https://www.w3.org/2018/credentials/v1"],
      "type": ["VerifiableCredential", "CIRCEcoreProfile"],
      "issuer": "<签发者的公钥指纹>",
      "issuanceDate": "<ISO-8601>",
      "expirationDate": "<ISO-8601 或 null>",
      "credentialSubject": {
        "id": "<被签的 agent id>",
        "coreHash": "<核剖面的 SHA-256>",
        "profileSeq": <账本序号，可选>,
        "ledgerHead": "<签发时的链尾哈希，可选>"
      },
      "proof": {"type": "Ed25519Signature2020", "created": "...", "proofValue": "<base64url>"}
    }

签名的覆盖范围
--------------
签名覆盖**除 `proof` 之外的整个凭据**（规范化后）。这是要点：
如果签名只覆盖 `coreHash`，那么签发者、有效期、主体都可以被事后替换而不被发现。
**「签了什么」必须等于「凭据里除了签名本身之外的全部内容」。**
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

# 与 chain.py 共用同一种规范化，否则签名与哈希会对同一份内容给出不同字节。
#
# 这里先把本文件所在目录放进 sys.path，**为了让本模块能被 `python vc.py` 或
# `python tests/selftest.py` 直接跑**——不自包含的模块必须在特定的 cwd 或
# 特定的 sys.path 才能导入，那种脆弱性会在换一个入口时立刻暴露（本次就是）。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from chain import normalize  # noqa: E402

CONTEXT = ["https://www.w3.org/2018/credentials/v1"]
VC_TYPE = ["VerifiableCredential", "CIRCEcoreProfile"]
PROOF_TYPE = "Ed25519Signature2020"


class VCError(AssertionError):
    """凭据不可信。"""


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64u(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def key_id(pub: Ed25519PublicKey) -> str:
    """公钥指纹：把 raw 公钥哈希后取前 16 个十六进制字符。

    不用 PEM 全文做 id，因为 PEM 里含换行与头尾，作为标识太长且不易比对。
    """
    import hashlib
    raw = pub.public_bytes(encoding=serialization.Encoding.Raw,
                           format=serialization.PublicFormat.Raw)
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 签发
# --------------------------------------------------------------------------
def issue(sk: Ed25519PrivateKey,
          agent_id: str,
          core_hash: str,
          *,
          issuance: str | None = None,
          expiration: str | None = None,
          profile_seq: int | None = None,
          ledger_head: str | None = None) -> dict:
    """签发一份核剖面凭据。"""
    if not core_hash or len(core_hash) != 64:
        raise ValueError(f"core_hash 必须是 64 位十六进制，收到 {core_hash!r}")

    subject: dict[str, Any] = {"id": agent_id, "coreHash": core_hash}
    if profile_seq is not None:
        subject["profileSeq"] = profile_seq
    if ledger_head is not None:
        subject["ledgerHead"] = ledger_head

    cred: dict[str, Any] = {
        "@context": CONTEXT,
        "type": VC_TYPE,
        "issuer": key_id(sk.public_key()),
        "issuanceDate": issuance or now_iso(),
        "expirationDate": expiration,
        "credentialSubject": subject,
    }

    sig = sk.sign(normalize(cred))          # 覆盖「除 proof 外的全部」
    cred["proof"] = {
        "type": PROOF_TYPE,
        "created": cred["issuanceDate"],
        "proofValue": _b64u(sig),
    }
    return cred


# --------------------------------------------------------------------------
# 验证
# --------------------------------------------------------------------------
def verify(cred: dict, pub: Ed25519PublicKey,
           *, at: str | None = None,
           expect_agent: str | None = None,
           expect_core_hash: str | None = None) -> dict:
    """验证凭据。通过则返回凭据主体，否则抛 VCError。

    四道检查，缺一不可：
      ① 结构完整（必需字段在）
      ② issuer 与传入的公钥一致（否则是「拿别人的公钥验我的证」）
      ③ 签名有效且覆盖除 proof 外的全部内容
      ④ 未过期（若给了 at 或凭据有 expirationDate）
    """
    for k in ("@context", "type", "issuer", "issuanceDate", "credentialSubject", "proof"):
        if k not in cred:
            raise VCError(f"凭据缺字段 {k!r}")

    # ② 签发者必须是这把公钥
    kid = key_id(pub)
    if cred["issuer"] != kid:
        raise VCError(f"issuer={cred['issuer']!r} 与公钥指纹 {kid!r} 不一致")

    # ③ 签名
    proof = cred["proof"]
    if proof.get("type") != PROOF_TYPE:
        raise VCError(f"不认识的签名类型 {proof.get('type')!r}")
    try:
        sig = _unb64u(proof["proofValue"])
    except Exception as e:
        raise VCError(f"proofValue 不是合法 base64url: {e}") from e

    signed = {k: v for k, v in cred.items() if k != "proof"}
    try:
        pub.verify(sig, normalize(signed))
    except InvalidSignature as e:
        raise VCError("签名无效——凭据内容被改动过，或不是这把密钥签的") from e

    # ④ 有效期
    when = at or now_iso()
    exp = cred.get("expirationDate")
    if exp is not None:
        if _parse(exp) <= _parse(when):
            raise VCError(f"凭据已于 {exp} 过期（检查时刻 {when}）")
    if _parse(cred["issuanceDate"]) > _parse(when):
        raise VCError(f"签发时间 {cred['issuanceDate']} 晚于检查时刻 {when}")

    # 可选的期望值比对——调用方说「我要的是 agent X 的核」
    subj = cred["credentialSubject"]
    if expect_agent is not None and subj.get("id") != expect_agent:
        raise VCError(f"主体是 {subj.get('id')!r}，不是期望的 {expect_agent!r}")
    if expect_core_hash is not None and subj.get("coreHash") != expect_core_hash:
        raise VCError("核剖面哈希与期望值不符——这是另一份核剖面的凭据")

    return subj


def _parse(s: str) -> datetime:
    t = s.replace("Z", "+00:00")
    d = datetime.fromisoformat(t)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 密钥存取（PEM，便于本地演示；生产应放 KMS）
# --------------------------------------------------------------------------
def save_key(sk: Ed25519PrivateKey, path) -> None:
    path.write_bytes(sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))


def load_key(path) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def to_json(cred: dict) -> str:
    return json.dumps(cred, ensure_ascii=False, indent=1, sort_keys=True)
