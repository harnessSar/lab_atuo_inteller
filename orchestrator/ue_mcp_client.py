"""EngineWire 原生 MCP 客户端（编排层 Agent 链路②用）。

对端：PID 编辑器内 EngineWire 插件（默认 ``http://127.0.0.1:3000/mcp``，
Streamable HTTP，JSON-RPC 2.0，服务端回 plain JSON）。
鉴权：请求头 ``X-MCP-Capability-Token``——插件首启自动生成 per-install token 于
      ``<ProjectRoot>/Saved/MCP/capability-token``（64 位小写 hex），客户端只读不写。

工具面（与旧官方插件 ToolsetRegistry 路由的本质区别）：
- 能力**平铺**在标准 ``tools/list``（58 domains，数百个能力），无 list_toolsets/call_tool 元工具；
- ``search_tools`` / ``describe_tool`` 由本客户端在 catalog 缓存上**本地**实现——
  这是 nl_graph 网关模式（search → describe → execute）的薄层基础，零服务端依赖；
- ``execute`` = 标准 ``tools/call`` 直发能力名。

会话：``initialize`` → 取响应头 ``Mcp-Session-Id`` → ``notifications/initialized``
      → 后续请求带会话头；失效（编辑器重启等）自动重握手一次。

单位红线：工站/机位坐标 **cm**（UE）；本层不做任何 mm→cm / Y 镜像换算（规则 001）。
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("sim_harness.ue_mcp_client")

#: 客户端声明的协议版本；服务端按自身偏好协商
_PROTOCOL_VERSION = "2025-03-26"

#: EngineWire 鉴权头（Private/MCP/AGENTS.md：capability auth 启用时方法分派前必查）
_TOKEN_HEADER = "X-MCP-Capability-Token"


@dataclass
class UnrealResponse:
    """统一响应（ok/raw/error 三件套，编排层与 nl_graph 共用）。"""

    ok: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error, "raw": self.raw}


class UeMcpClient:
    """EngineWire 同步客户端：短请求、服务端会话、token 鉴权、catalog 本地缓存。"""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:3000/mcp",
        *,
        token_file: str | Path | None = None,
        token: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self._token_inline = token          # 直传优先（测试/调用方自管）
        self._token_file = Path(token_file) if token_file else None
        self._token_cache: str | None = None
        self._session_id: str | None = None
        self._ids = itertools.count(1)
        self._catalog: dict[str, dict[str, Any]] | None = None  # name → 工具元数据

    # -- 鉴权 ---------------------------------------------------------------

    def _token(self) -> str | None:
        """惰性取 token：直传 > 文件缓存 > 读文件。文件只读一次（编辑器重开不换 token）。"""
        if self._token_inline:
            return self._token_inline
        if self._token_cache:
            return self._token_cache
        if self._token_file and self._token_file.is_file():
            tok = self._token_file.read_text(encoding="utf-8").strip()
            if tok:
                self._token_cache = tok
                return tok
        return None

    # -- 底层收发 -----------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any], str | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        tok = self._token()
        if tok:
            headers[_TOKEN_HEADER] = tok
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.timeout_s)
        ctype = resp.headers.get("Content-Type", "")
        # 编码红线：无 charset 时 requests 默认 ISO-8859-1，中文回执变 mojibake；
        # 且 str.splitlines() 把 NEL(0x85)/\u2028 等也当行界，中文 UTF-8 字节经 Latin-1
        # 解码后常含这些码位——SSE data 行被切断报 "Unterminated string"（2026-11-04 实测）。
        # 统一按 UTF-8 解码、只认 \n（SSE 规范即 LF）。
        text = resp.content.decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            # SSE 流（写操作/长任务实测触发）：取最后一个 data: 块为 JSON-RPC 响应
            data = [ln[5:].strip() for ln in text.split("\n") if ln.startswith("data:")]
            body = json.loads(data[-1]) if data else {}
        elif resp.content:
            try:
                body = json.loads(text)
            except ValueError:
                raise RuntimeError(
                    f"响应非 JSON（HTTP {resp.status_code}, Content-Type={ctype}）: {text[:200]}")
        else:
            body = {}
        return resp.status_code, body, resp.headers.get("Mcp-Session-Id")

    def _initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sim_harness", "version": "0.2"},
            },
        }
        status, body, sid = self._post(payload)
        if status == 401:
            raise RuntimeError(
                f"initialize 鉴权失败（401 Invalid capability token）："
                f"token_file={self._token_file} 不存在或已轮换；"
                f"请到 <PID工程>/Saved/MCP/capability-token 核对后改 sim_harness.toml [unreal] token_file")
        if status != 200 or "result" not in body:
            raise RuntimeError(f"initialize 失败 HTTP {status}: {str(body)[:200]}")
        self._session_id = sid
        # 协议要求的就绪通知
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        negotiated = body["result"].get("protocolVersion", "?")
        logger.info("UE MCP 会话建立 sid=%s protocol=%s", sid, negotiated)

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一次 JSON-RPC；会话失效自动重握手重试一次。"""
        last_exc: Exception | None = None
        for attempt in range(2):
            if not self._session_id:
                self._initialize()
            payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
            try:
                status, body, _ = self._post(payload)
            except (requests.RequestException, RuntimeError) as exc:
                last_exc = exc
                self._session_id = None
                continue
            if status == 401:
                # token 问题重握手无用（会话头救不了鉴权头），直接炸穿让上层报指引
                raise RuntimeError(f"401 Invalid capability token（token_file={self._token_file}）")
            # 会话失效典型表现：4xx 或 JSON-RPC error 提及 session
            err = body.get("error") or {}
            if status in (400, 404) or "session" in str(err.get("message", "")).lower():
                logger.warning("会话失效（HTTP %s），重握手", status)
                self._session_id = None
                continue
            return body
        raise RuntimeError(f"RPC {method} 失败（重握手后仍不行）: {last_exc or '会话失效'}")

    # -- catalog（tools/list；0.6.0 只暴露单个 `unreal` 网关工具，ping 探活用） ----

    def _ensure_catalog(self) -> dict[str, dict[str, Any]]:
        if self._catalog is None:
            body = self._rpc("tools/list")
            tools = body.get("result", {}).get("tools", [])
            self._catalog = {t.get("name", ""): t for t in tools if t.get("name")}
            logger.info("EngineWire tools/list：%s", list(self._catalog))
        return self._catalog

    def invalidate_catalog(self) -> None:
        """插件热更/编辑器重启后调，下次访问重新 tools/list。"""
        self._catalog = None

    # -- 执行（`unreal` 网关工具：search/describe/execute/configure 全走这里） -----

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> UnrealResponse:
        """标准 tools/call 直发能力名；命中对端速率限制（-32000 rate limit）按提示秒数退避重试。"""
        body: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                body = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
            except Exception as exc:
                return UnrealResponse(ok=False, error=str(exc))
            err = body.get("error") or {}
            msg = str(err.get("message", ""))
            if err.get("code") == -32000 and "rate limit" in msg.lower():
                # "Retry in N s." 提示退避时长；无提示按 5s/15s 递增
                m = re.search(r"retry in (\d+)\s*s", msg, re.IGNORECASE)
                wait = int(m.group(1)) + 1 if m else (5, 15)[min(attempt, 1)]
                logger.warning("对端限流，%ds 后重试（第 %d 次）", wait, attempt + 1)
                time.sleep(min(wait, 45))
                continue
            break
        else:
            return UnrealResponse(ok=False, raw=body or {},
                                  error="对端速率限制（120 calls/min），退避重试 3 次仍被限")
        if "error" in body:
            err = body["error"]
            return UnrealResponse(ok=False, raw=body, error=f"JSON-RPC {err.get('code')}: {err.get('message')}")
        result = body.get("result", {})
        # MCP 工具结果：content[] + 可选 isError；image 块单列（截图能力实测走 image 块）
        blocks = result.get("content", [])
        texts = [c.get("text", "") for c in blocks if c.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        raw: dict[str, Any] = {"content_text": text}
        images = [{"mimeType": c.get("mimeType", ""), "data": c.get("data", "")}
                  for c in blocks if c.get("type") == "image"]
        if images:
            raw["content_images"] = images
        # EngineWire 回执形态：状态行（ok / error …）+ 空行 + JSON 正文——剥首行再解析
        payload = text.split("\n\n", 1)[1] if "\n\n" in text else text
        try:
            raw["content_json"] = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            pass
        if result.get("isError"):
            return UnrealResponse(ok=False, raw=raw, error=text[:500])
        return UnrealResponse(ok=True, raw=raw)

    def ping(self) -> UnrealResponse:
        """连通性探测 = 全新握手 + catalog 计数。"""
        try:
            self._session_id = None  # 强制全新握手
            self._catalog = None
            catalog = self._ensure_catalog()
        except Exception as exc:
            return UnrealResponse(ok=False, error=str(exc))
        return UnrealResponse(ok=True, raw={"capabilities": len(catalog)})

    # -- 业务便捷封装（pipeline.py 机器闸流水线用；run_ue_steps 默认关） --------
    # EngineWire 能力名待 W5 对齐核实；未对齐前调用会如实返回"能力不存在"。

    def load_scene(self, scene_json_path: str) -> UnrealResponse:
        return self.execute("load_xtal_scene", {"path": scene_json_path})

    def clear_scene(self) -> UnrealResponse:
        return self.execute("clear_xtal_scene", {})

    def brush_slots(self, actor_name: str, device_key: str, json_path: str | None = None) -> UnrealResponse:
        params: dict[str, Any] = {"actor": actor_name, "device_key": device_key}
        if json_path:
            params["json_path"] = json_path
        return self.execute("brush_hte_slots", params)

    def clear_slots(self, actor_name: str) -> UnrealResponse:
        return self.execute("clear_hte_slots", {"actor": actor_name})
