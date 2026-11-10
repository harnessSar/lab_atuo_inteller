"""langgraph dev / LangGraph Studio 用图工厂（零参可调用，内部读 config/sim_harness.toml 装配）。

Studio 与 GUI 的分工：Studio 管**图内 trace/单步/时间旅行**（调试用）；
Chainlit GUI 管**日常对话与人审三态**（生产用）。两入口共用同一 build_* 装配函数，
行为无分叉。资产图在 Studio 里人审走 Studio 原生 interrupt UI。
ReAct 图不再挂整机触发器，整机编排由对话框 ``route`` 认领后进资产图。

启动（lab_atuo_inteller 目录）：
    D:\\project\\kin_pad\\.venv\\Scripts\\python.exe -m langgraph_cli dev --no-browser
浏览器打开 https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
（UI 壳来自云端、数据全走本机 2024；无外网时用 --tunnel 反代或只看 API）。
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import toml

_SIM_ROOT = Path(__file__).resolve().parents[1]
if str(_SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SIM_ROOT))

# ── 重型装配全部在模块导入期完成（import 时无 blockbuster 侦测），工厂只返回缓存 ──
# 为什么：langgraph dev 的 blockbuster 在**请求上下文**抓同步阻塞（toml 读、
# 模块 import 的 Path.resolve），工厂被 schemas/graph 端点调用时一旦做 I/O 即 500；
# --allow-blocking 只豁免 runtime worker，不豁免 API 的 schema 路径（2026-11-10 实测）。
from llm.provider import LlmConfig, LlmProvider  # noqa: E402
from orchestrator.client.mcp_stdio_client import McpStdioClient  # noqa: E402
from orchestrator.client.ue_mcp_client import UeMcpClient  # noqa: E402
from orchestrator.graph.custom.asset import build_asset_pipeline  # noqa: E402
from orchestrator.graph.custom.bp_assembly import build_bp_assembly  # noqa: E402
from orchestrator.graph.react import assemble_planner_prompt, build_nl_graph  # noqa: E402
from orchestrator.tool.asset_tools import make_asset_tools  # noqa: E402
from orchestrator.tool.blender_tools import make_blender_tools  # noqa: E402


def _load() -> tuple[dict, Path]:
    cfg = toml.load(str(_SIM_ROOT / "config" / "sim_harness.toml"))
    work = Path(cfg.get("paths", {}).get("work_dir", "lab_atuo_inteller/_work"))
    if not work.is_absolute():
        work = (_SIM_ROOT.parent / work).resolve()
    return cfg, work


def _llm(cfg: dict) -> LlmProvider:
    t = cfg.get("llm", {})
    return LlmProvider(LlmConfig(
        base_url=t.get("base_url", "http://localhost:11434/v1"),
        model=t.get("model", "qwen2.5:7b"),
        api_key=t.get("api_key", "ollama"),
        vision=bool(t.get("vision", False)),
    ))


def _build_asset():
    cfg, work = _load()
    cmd = shlex.split(cfg.get("blender", {}).get("command", "") or "") or None
    client = McpStdioClient(cmd) if cmd else None
    return build_asset_pipeline(_llm(cfg), work, blender_client=client)


def _build_nl():
    cfg, work = _load()
    llm = _llm(cfg)
    ue_t = cfg.get("unreal", {})
    client = UeMcpClient(ue_t.get("endpoint", "http://127.0.0.1:3000/mcp"),
                         token_file=ue_t.get("token_file"))
    cmd = shlex.split(cfg.get("blender", {}).get("command", "") or "") or None
    extra = make_asset_tools(work)
    if cmd:
        extra.extend(make_blender_tools(McpStdioClient(cmd), work))
    # Studio 直调没有 Python 包裹层播种 messages（2026-11-10 实测 KeyError 'messages'），
    # 系统提示词经 build_nl_graph(sys_prompt=...) 的 prep 入口节点注入，与 run_nl_command 同源。
    return build_nl_graph(llm, client, work, vision=llm.cfg.vision, extra_tools=extra,
                          sys_prompt=assemble_planner_prompt(llm.cfg.vision, bool(extra)))


def _build_bp():
    cfg, work = _load()
    ue_t = cfg.get("unreal", {})
    client = UeMcpClient(ue_t.get("endpoint", "http://127.0.0.1:3000/mcp"),
                         token_file=ue_t.get("token_file"))
    return build_bp_assembly(client, work, llm=_llm(cfg))


# 导入即构建（此刻无请求上下文、无 blockbuster）；CompiledStateGraph 并发安全，
# 与 langgraph.json 直接指对象实例同语义。watchfiles 热重载会重新 import 本模块重建。
_ASSET_GRAPH = _build_asset()
_NL_GRAPH = _build_nl()
_BP_GRAPH = _build_bp()


def asset_graph():
    """确定性资产流水线（10 节点 DAG；human_gate interrupt 在 Studio 里原生可见可续）。"""
    return _ASSET_GRAPH


def nl_graph():
    """自由 ReAct 环（plan⇄execute→report）+ UE 网关 + 资产单步工具（无整机触发器）。"""
    return _NL_GRAPH


def bp_graph():
    """BP 装配流水线（fetch→pre_check→brush→semantics→layout→verify→human_gate→finalize）。"""
    return _BP_GRAPH
