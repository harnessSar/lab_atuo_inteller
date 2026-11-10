"""通用 stdio MCP 客户端（JSON-RPC 2.0 over 子进程 stdin/stdout，NDJSON 分帧）。

适用对端：FastMCP / 官方 MCP SDK 写的 stdio server（如 kin_pad.mcp.asset.server、
blender-mcp 类 Blender 桥）。Streamable HTTP 对端（EngineWire）走 ``ue_mcp_client``，
本模块不覆盖。

与 ``UeMcpClient`` 同构的横切（踩坑沿用，不重新发明）：
- 握手：``initialize`` → ``notifications/initialized``（协议硬要求）；
- 编码：stdin/stdout 一律 UTF-8 bytes，中文工具回执不经 requests/Latin-1；
- 错误三件套：``McpResponse(ok/raw/error)``，raw 形态与 UeMcpClient 对齐
  （``content_text`` / ``content_json`` / ``content_images``），编排层两路同构处理；
- 失败是Observation：spawn 失败 / 超时 / 对端 isError 都进 error 字段，不炸穿调用方。

传输红线：MCP stdio = **换行分隔 JSON**（无 Content-Length 头）；server 的日志
全走 stderr（本客户端另起守护线程抽干，防管道写满死锁）。stdout 读取走守护线程
+ Queue（Windows 管道不支持 select）。
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("lab_atuo_inteller.orchestrator.client.mcp_stdio_client")

#: 客户端声明的协议版本；服务端按自身偏好协商（与 ue_mcp_client 同口径）
_PROTOCOL_VERSION = "2025-03-26"


@dataclass
class McpResponse:
    """统一响应（ok/raw/error 三件套；raw 键位与 UeMcpClient 对齐，两路同构处理）。"""

    ok: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error, "raw": self.raw}


class McpStdioClient:
    """stdio MCP 同步客户端：惰性拉起子进程，首次调用时完成握手。

    ``command``：启动命令行（list 形态，如 ``["uvx", "blender-mcp"]`` 或
    ``[sys.executable, "-m", "kin_pad.mcp.asset.server"]``）；空 list = 未配置，
    所有调用返回指引性失败（不抛异常炸图）。
    """

    def __init__(self, command: list[str] | None, *, timeout_s: float = 30.0) -> None:
        self.command = list(command or [])
        self.timeout_s = timeout_s
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._out_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._initialized = False

    # -- 子进程生命周期 ------------------------------------------------------

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.info("[mcp-stderr] %s", line[:300])

    def _drain_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in iter(self._proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                self._out_queue.put(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("MCP stdout 非 JSON 行（忽略）: %s", line[:200])
        self._out_queue.put(None)  # EOF 哨兵：对端进程已退出

    def _ensure_proc(self) -> None:
        if self._initialized and self._proc and self._proc.poll() is None:
            return
        self.close()
        if not self.command:
            raise RuntimeError(
                "MCP stdio 未配置启动命令（sim_harness.toml [blender] command 为空）；"
                "Blender 未开时请改用场景导出 JSON 文件流（asset_catalog）")
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=False)
        except FileNotFoundError as exc:
            raise RuntimeError(f"MCP stdio 启动失败（命令不存在）: {self.command[0]} ({exc})")
        except OSError as exc:
            raise RuntimeError(f"MCP stdio 启动失败: {self.command} ({exc})")
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._initialize()

    def _initialize(self) -> None:
        body = self._rpc("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "lab_atuo_inteller", "version": "0.1"},
        })
        if "error" in body:
            raise RuntimeError(f"initialize 被拒: {str(body['error'])[:200]}")
        negotiated = body.get("result", {}).get("protocolVersion", "?")
        # 协议要求的就绪通知（无 id，不等响应）
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        logger.info("MCP stdio 会话建立 protocol=%s cmd=%s", negotiated, self.command[:2])

    def close(self) -> None:
        self._initialized = False
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
        self._proc = None
        while not self._out_queue.empty():
            try:
                self._out_queue.get_nowait()
            except queue.Empty:
                break

    def __enter__(self) -> "McpStdioClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:  # 尽力而为；解释器退出期异常全吞
        try:
            self.close()
        except Exception:
            pass

    # -- 底层收发 -------------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise RuntimeError(f"MCP stdin 写入失败（对端已退出？）: {exc}")

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = next(self._ids)
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            try:
                msg = self._out_queue.get(timeout=self.timeout_s)
            except queue.Empty:
                raise RuntimeError(f"MCP {method} 等待响应超时（{self.timeout_s}s）")
            if msg is None:
                self._initialized = False
                raise RuntimeError(f"MCP {method} 失败：对端进程已退出（stderr 见日志）")
            if msg.get("id") == req_id:
                return msg
            # 跳过 server 通知/他请求（无配对 id）

    # -- 工具面 ----------------------------------------------------------------

    def tools_list(self) -> list[dict[str, Any]]:
        self._ensure_proc()
        body = self._rpc("tools/list")
        return body.get("result", {}).get("tools", [])

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> McpResponse:
        """标准 tools/call；任何失败都进 McpResponse（ok=False），不炸穿调用方。"""
        try:
            self._ensure_proc()
            body = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        except RuntimeError as exc:
            return McpResponse(ok=False, error=str(exc))
        if "error" in body:
            err = body["error"]
            return McpResponse(ok=False, raw=body,
                               error=f"JSON-RPC {err.get('code')}: {err.get('message')}")
        result = body.get("result", {})
        blocks = result.get("content", [])
        texts = [c.get("text", "") for c in blocks if c.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        raw: dict[str, Any] = {"content_text": text}
        images = [{"mimeType": c.get("mimeType", ""), "data": c.get("data", "")}
                  for c in blocks if c.get("type") == "image"]
        if images:
            raw["content_images"] = images
        try:
            raw["content_json"] = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        if result.get("isError"):
            return McpResponse(ok=False, raw=raw, error=text[:500])
        return McpResponse(ok=True, raw=raw)

    def ping(self) -> McpResponse:
        """连通性探测 = 拉起进程 + 握手 + tools/list 计数。"""
        try:
            self.close()
            tools = self.tools_list()
        except RuntimeError as exc:
            return McpResponse(ok=False, error=str(exc))
        return McpResponse(ok=True, raw={"tools": [t.get("name", "?") for t in tools]})
