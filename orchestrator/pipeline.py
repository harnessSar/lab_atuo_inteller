"""资产编排管线（Phase 1 顺序实现，LangGraph 就绪后可平铺替换为图节点）。

每个步骤 = LangGraph 状态机里的一个节点；机器步骤直接调用，LLM 步骤
（gen_toml_lua / gen_bp_spec / llm_fix）当前以确定性规则 + 模板占位，
LLM 接入点用 ``# LLM-HOOK`` 标注——④ 接模型后替换为 ``llm/provider`` 调用
（模式已定：原生 tool calling，见 ``orchestrator/nl_graph.py``）。

红线：机器闸不过则不往下走；产物不落盘业务目录，全部进 evidence 工作区，
落盘/合入由 human_gate 人审后触发（规则 000：不为 agent 改 PID 业务路径）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# kin_pad/src 进 sys.path（调用方可用 PYTHONPATH 或 config 注入，禁硬编码绝对路径）
_KIN_PAD_SRC = Path(__file__).resolve().parents[2] / "kin_pad" / "src"
if _KIN_PAD_SRC.is_dir() and str(_KIN_PAD_SRC) not in sys.path:
    sys.path.insert(0, str(_KIN_PAD_SRC))

from kin_pad.mcp.asset import diffcheck, emit, golden, parsing, validate  # noqa: E402
from kin_pad.mcp.asset.spec import DeviceSpec  # noqa: E402
from orchestrator.ue_mcp_client import UeMcpClient  # noqa: E402


@dataclass
class GateError:
    gate: str
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"gate": self.gate, "message": self.message, "location": self.location}


@dataclass
class DeviceState:
    """编排状态（对应 LangGraph 的 DeviceState）。"""

    hte_json_path: str
    motor_table_path: str
    device_key: str
    work_dir: str                                   # evidence / 草稿工作区（非业务目录）
    spec: DeviceSpec | None = None                  # parse 后的单一事实源（关节/槽位/电机）
    toml_draft: str = ""                            # TOML 草稿（未过闸不落盘业务目录）
    lua_draft: str = ""                             # 被动 Lua 草稿（同上）
    script_name: str = ""                           # Lua 脚本名（TOML script_path 引用它）
    errors: list[GateError] = field(default_factory=list)  # 结构化错误证据（喂 llm_fix/人审）
    retries: int = 0
    max_retries: int = 3                            # 自纠上限，到头停等人工
    evidence: list[dict[str, Any]] = field(default_factory=list)  # 各节点留证（kind+payload）
    approved: bool | None = None                    # human_gate 审批结果（None=待审）

    def add_error(self, gate: str, message: str, location: str = "") -> None:
        self.errors.append(GateError(gate=gate, message=message, location=location))

    def record(self, kind: str, payload: Any) -> None:
        self.evidence.append({"kind": kind, "payload": payload})


@dataclass
class PipelineConfig:
    """路径/连接注入（禁硬编码绝对路径；来自 sim_harness.toml 或环境变量）。"""

    kin_pad_root: str
    save_dir: str
    lambda_spool_cpp: str
    ue_mcp_endpoint: str = "http://127.0.0.1:8000/mcp"   # lab_simmer UE 5.8 官方 MCP
    run_ue_steps: bool = False        # UE 步骤（刷槽位/装载）默认关，验证时显式开
    run_golden: bool = True


# ---------------------------------------------------------------------------
# 节点实现
# ---------------------------------------------------------------------------

def node_parse_inputs(state: DeviceState) -> DeviceState:
    try:
        spec = parsing.parse_hte_device(state.hte_json_path, state.device_key)
        spec = parsing.parse_motor_table(state.motor_table_path, spec)
        state.spec = spec
        state.record("parse", {"device_key": state.device_key, "joints": len(spec.joints), "slots": len(spec.slots)})
    except (parsing.ParseError, ValueError) as exc:
        state.add_error("parse_inputs", str(exc))
    return state


def node_gen_toml_lua(state: DeviceState) -> DeviceState:
    # LLM-HOOK: ④ 接模型后，此节点由 llm.provider 按模板 + DeviceSpec 起草
    # （模式已定：原生 tool calling，见 nl_graph）；当前用确定性 emit
    # （等价于“LLM 完美按契约生成”的上界），先打通机器链。
    assert state.spec is not None
    state.script_name = emit.default_script_name(state.spec)
    state.toml_draft = emit.emit_toml(state.spec, script_name=state.script_name)
    state.lua_draft = emit.emit_passives_lua(state.spec, validate=True)
    state.record("gen", {"script_name": state.script_name, "has_lua": bool(state.lua_draft)})
    return state


def node_gate_assets(state: DeviceState) -> DeviceState:
    assert state.spec is not None
    report = validate.run_all_gates(state.spec, state.toml_draft)
    state.record("gate", report.to_dict())
    if not report.ok:
        for issue in report.issues:
            state.add_error(issue.gate, issue.message, issue.location)
    return state


def node_diff_three_end(state: DeviceState, cfg: PipelineConfig) -> DeviceState:
    assert state.spec is not None
    report = diffcheck.diff_three_end_constants(
        state.spec.name.lower(), save_dir=cfg.save_dir, lambda_spool_cpp=cfg.lambda_spool_cpp
    )
    state.record("three_end_diff", report.to_dict())
    # 三端 diff 只产出证据，不作为硬闸（人审决策）；缺源/不一致仅记录
    return state


def node_golden(state: DeviceState, cfg: PipelineConfig) -> DeviceState:
    if not cfg.run_golden:
        return state
    result = golden.run_golden_tests(kin_pad_root=cfg.kin_pad_root)
    state.record("golden", result.to_dict())
    if not result.ok:
        state.add_error("golden", f"golden 测试失败 rc={result.returncode}: {'; '.join(result.failed[:5])}")
    return state


def node_brush_slots(state: DeviceState, cfg: PipelineConfig) -> DeviceState:
    if not cfg.run_ue_steps:
        state.record("brush_slots", {"skipped": True, "reason": "run_ue_steps=False"})
        return state
    assert state.spec is not None
    client = UeMcpClient(cfg.ue_mcp_endpoint)
    resp = client.brush_slots(actor_name=state.spec.name, device_key=state.device_key, json_path=state.hte_json_path)
    state.record("brush_slots", resp.to_dict())
    if not resp.ok:
        state.add_error("brush_slots", resp.error)
    return state


def node_llm_fix(state: DeviceState) -> DeviceState:
    # LLM-HOOK: ④ 接模型后，把 state.errors（哪一步/哪条断言/定位）喂给模型修复
    # draft（模式已定：原生 tool calling，见 nl_graph）；当前仅做机械重试计数，
    # 真实修复由人介入或后续模型接入。
    state.retries += 1
    return state


def node_human_gate(state: DeviceState) -> DeviceState:
    # 产出证据包到工作区；批准与否由人决定（LangGraph interrupt 的占位）。
    work = Path(state.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    bundle = {
        "device_key": state.device_key,
        "spec": state.spec.model_dump() if state.spec else None,
        "toml_draft": state.toml_draft,
        "lua_draft": state.lua_draft,
        "script_name": state.script_name,
        "errors": [e.to_dict() for e in state.errors],
        "evidence": state.evidence,
    }
    out = work / f"{state.device_key}_evidence.json"
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    state.record("human_gate", {"evidence_path": str(out), "pending": state.approved is None})
    return state


# ---------------------------------------------------------------------------
# 编排（LangGraph 就绪前用顺序驱动；边逻辑与图一致）
# ---------------------------------------------------------------------------

def run_pipeline(state: DeviceState, cfg: PipelineConfig) -> DeviceState:
    """顺序驱动各节点（边逻辑与 §4 LangGraph 设计一致；任何致命错误都短路到 human_gate）。"""
    state = node_parse_inputs(state)
    if state.errors:
        return node_human_gate(state)

    # 生成 → 机器闸，带有限自纠：闸不过 → llm_fix 修 draft → 重闸，到 max_retries 停等人工
    while True:
        state.errors.clear()
        state = node_gen_toml_lua(state)
        state = node_gate_assets(state)
        if not state.errors:
            break
        if state.retries >= state.max_retries:
            return node_human_gate(state)
        state = node_llm_fix(state)

    state = node_diff_three_end(state, cfg)   # 只读证据，不作硬闸
    state = node_golden(state, cfg)           # golden 失败计入 errors，human_gate 前可见
    state = node_brush_slots(state, cfg)      # run_ue_steps=False 时仅记录 skipped
    return node_human_gate(state)
