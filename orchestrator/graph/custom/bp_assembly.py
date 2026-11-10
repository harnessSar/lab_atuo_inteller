"""确定性 BP 装配流水线：HTE 设备档案 → UE 蓝图（XtalActor）属性 + 虚拟槽位。

与 asset 图的分工：asset 图产 TOML/Lua **文本草稿**（人审后才落业务盘）；本图操作的
对象是**已在编辑器里的 BP**——变更在 brush 节点即生效（经 ``Hte.BrushSlots`` 唯一
通道，幂等）。因此人审设计是**先装后审、驳回回滚**：

- 批准 → 提示在 UE 保存蓝图资产（属性/槽位已写入 BP 默认值与关卡实例）；
- 驳回带意见 → ``Hte.ClearVirtualSlots`` 回滚本工具刷出的虚拟槽（属性默认值不在
  清理范围，skill 红线如实上报）；终止 → 保留现状。

链路（边确定性；LLM 只出现在 propose_layout 的 VL 顾问单发，不决定流程方向）::

    fetch_device(本地解析 HTE 档案) → pre_check(actor 在当前世界? 命名冲突? mesh↔joint diff)
    → brush(Hte.BrushSlots) → map_semantics(配置查表，不猜) → propose_layout(拓扑 + VL 顾问)
    → verify(日志计数/属性同步/SCS 集合/幂等重刷) → human_gate(interrupt 三态)
    → finalize(批准=保存提示 / 驳回=回滚) → report

姐妹篇：``asset.py``（TOML/Lua 资产图）。对话框经 ``match`` / ``run`` 整图进入；
dsh 经 ``bp_mcp.py`` 两个整图工具进入。节点共用 ``bp_tools`` 同一实现，两路不分叉。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

# kin_pad/src 进 sys.path（禁硬编码业务路径）。本文件在 graph/custom/，parents[4] = 仓库根
_KIN_PAD_SRC = Path(__file__).resolve().parents[4] / "kin_pad" / "src"
if _KIN_PAD_SRC.is_dir() and str(_KIN_PAD_SRC) not in sys.path:
    sys.path.insert(0, str(_KIN_PAD_SRC))

import toml  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from kin_pad.mcp.asset.parsing import ParseError, load_hte_devices, parse_hte_device  # noqa: E402

from llm.provider import ChatMessage, LlmConfig, LlmProvider  # noqa: E402
from orchestrator.client.ue_mcp_client import UeMcpClient  # noqa: E402
from orchestrator.tool import bp_tools as bpt  # noqa: E402

logger = logging.getLogger("lab_atuo_inteller.orchestrator.graph.custom.bp_assembly")

_FAIL = "执行失败："  # Observation 失败前缀（与 asset/nl_graph 同约定）
_SIM_ROOT = Path(__file__).resolve().parents[3]   # lab_atuo_inteller
_REPO_ROOT = Path(__file__).resolve().parents[4]  # 仓库根


# ---------------------------------------------------------------------------
# 状态（delta-only 回写：节点只 return 自己负责的键——DESIGN 附 2026-11-03 教训）
# ---------------------------------------------------------------------------
class BpState(TypedDict, total=False):
    """流水线状态。流转：入参 → fetch 写 slot_names/slots_detail/id_type_map →
    pre_check 写 precheck_notes/scs_names → brush → map_semantics 写 semantics →
    propose_layout 写 layout → verify 写 brush_created/gate_ok/gate_issues →
    human_gate 写 approved/feedback → finalize 写 rolled_back → report。"""

    # 入参（invoke 时写入）
    actor_name: str          # 场景中设备 actor 的 label（如 BP_121_Capping_8a2）
    device_key: str          # HTE 档案设备 key（results 下的键，如 "121"）
    hte_json_path: str       # 已解析的 HTE 档案绝对路径（入口经 bpt.resolve_hte_json）
    toml_path: str           # 可选：运动学 TOML，做 mesh↔joint 对齐检查
    images: list[str]        # 可选：VL 顾问单发用图（资料图/截图 data URL）
    work_dir: str            # 证据工作区（bp_run 在其下）
    interactive: bool        # True = human_gate interrupt 挂起等人；False = 只出证据
    rollback_on_reject: bool # 驳回时是否 Hte.ClearVirtualSlots 回滚（默认 True）
    verify_idempotent: bool  # verify 是否二次重刷验幂等（默认 True）
    # 过程量
    slot_names: list[str]
    slot_count: int
    slots_detail: list[dict]   # [{"name":..., "valid_model_ids":[int,...]}]
    id_type_map: dict[str, str]  # {str(model_id): model_type}（全档案，供语义提案）
    model_type: str
    device_label: str          # "model_id name（module_name）"
    precheck_notes: list[str]
    scs_names: list[str]       # pre_check 抓到的 SCS 组件名（刷前基线）
    brush_created: int
    brush_skipped: int
    props_synced: bool
    semantics: dict            # SocketType/AnchorType 提案（只进证据，不写 UE）
    layout: dict               # 拓扑提案 + VL 顾问文本
    review_notes: list[str]    # 全量人审提示（正式产物，必须全量上报）
    gate_ok: bool
    gate_issues: list[dict]
    approved: bool
    feedback: str
    rolled_back: bool
    error: str                 # 终态失败（fetch/pre_check/brush 硬失败）→ report 短路
    evidence: list[str]        # 每节点一行简述（证据包正文）
    report: str


def _run_dir(state: BpState) -> Path:
    rd = Path(state["work_dir"]) / "bp_run"
    rd.mkdir(parents=True, exist_ok=True)
    return rd


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def fetch_node(state: BpState) -> BpState:
    """fetch_device：本地解析 HTE 档案（不碰 UE）。设备 key 不存在 = 终态失败。"""
    path = Path(state["hte_json_path"])
    if not path.is_file():
        return {"error": f"HTE 档案不存在: {path}"
                         "（入口应先经 bp_tools.resolve_hte_json 解析；检查配置或显式路径）"}
    try:
        spec = parse_hte_device(path, state["device_key"])
        results = load_hte_devices(path)
    except ParseError as exc:
        return {"error": f"HTE 档案解析失败: {exc}"}
    if not spec.slots:
        return {"error": f"设备 {state['device_key']}（{spec.name}）没有 slots——"
                         "无槽设备（纯静止耗材？）不适用本流水线"}
    id_type_map = {str(e["model_id"]): str(e.get("model_type") or "")
                   for e in results.values()
                   if isinstance(e, dict) and e.get("model_id") is not None}
    label = f"{spec.model_id} {spec.name}（{spec.module_name or spec.model_type}）"
    logger.info("fetch_device: %s，槽位 %d 个", label, len(spec.slots))
    return {"slot_names": [s.name for s in spec.slots],
            "slot_count": len(spec.slots),
            "slots_detail": [{"name": s.name, "valid_model_ids": list(s.valid_model_ids)}
                             for s in spec.slots],
            "id_type_map": id_type_map,
            "model_type": spec.model_type,
            "device_label": label,
            "evidence": state.get("evidence", []) + [
                f"fetch_device: {label}，槽位 {len(spec.slots)} 个"]}


def make_precheck_node(client):
    """pre_check：actor 在当前世界？（硬闸）+ 命名冲突/mesh↔joint diff（软，人审提示）。

    软项失败不拦流程——证据全量上人审；actor 缺失是**唯一硬失败**
    （Hte.BrushSlots 只遍历当前编辑器世界，必败无疑，fail-fast 给指令级指引）。
    """

    def precheck_node(state: BpState) -> BpState:
        notes: list[str] = []
        ev: list[str] = []
        found, note = bpt.ue_actor_exists(client, state["actor_name"])
        if found is False:
            return {"error": f"关卡里找不到 Actor {state['actor_name']!r}"
                             "（Hte.BrushSlots 只遍历当前编辑器世界；control_actor.list 跨 "
                             "streaming level 看得到也刷不到）。正确动作：确认 actor label 逐字"
                             "一致，或先在编辑器切到其所在关卡后重试。"}
        if found is None:
            notes.append(f"actor 枚举不可用（{note}），前置核实跳过——由 brush 自身报错兜底")
        else:
            ev.append(f"pre_check: actor 命中（{note}）")
        # SCS 命名冲突 / 已存在基线（刷前）。scs_ok 单独跟踪：读取成功但为空 ≠ 读取失败——
        # 空 SCS 时 mesh 对齐检查仍须跑（全部关节缺 mesh 是真阳性，不能静默跳过）
        scs_names: list[str] = []
        scs_ok = False
        names, err = bpt.ue_scs_components(client, state["actor_name"])
        if err:
            notes.append(f"SCS 读取失败（{err}），命名冲突与 mesh 对齐检查跳过；verify 将重试")
        else:
            scs_names = names
            scs_ok = True
            missing, pre_existing, conflicts = bpt.diff_slot_names(state["slot_names"], scs_names)
            if pre_existing:
                notes.append(f"BP 已有同名槽 {len(pre_existing)} 个（首刷将幂等跳过）: "
                             f"{pre_existing[:10]}")
            for c in conflicts:
                notes.append(f"变体冲突：HTE 槽键与 BP 既有组件同义不同名（{c}，LP_1≠LP-1），"
                             "刷入后新旧并存——如实上报，需人工判断是否清理（本图不擅删旧件）")
            ev.append(f"pre_check: SCS {len(scs_names)} 组件，待创建 {len(missing)}，"
                      f"已存在 {len(pre_existing)}，变体冲突 {len(conflicts)}")
        # mesh ↔ TOML 关节 diff（可选输入；is_virtual 无 mesh 不参与，规则 001）
        toml_path = (state.get("toml_path") or "").strip()
        if toml_path:
            joints, virtuals, terr = bpt.toml_joint_names(toml_path)
            if terr:
                notes.append(f"TOML 读取失败（{terr}），mesh 对齐检查跳过")
            elif not scs_ok:
                notes.append("SCS 不可读，mesh 对齐检查跳过")
            else:
                missing_mesh = [j for j in joints if j not in scs_names]
                if missing_mesh:
                    notes.append(f"mesh 缺口 {len(missing_mesh)}（运行时 ApplyMotionByMatrix 将 "
                                 f"mesh not found）: {missing_mesh[:10]}")
                ev.append(f"pre_check: TOML 关节 {len(joints)}（虚拟 {len(virtuals)} 不对齐），"
                          f"mesh 缺口 {len(missing_mesh)}")
        return {"precheck_notes": notes, "scs_names": scs_names,
                "review_notes": state.get("review_notes", []) + notes,
                "evidence": state.get("evidence", []) + ev}

    return precheck_node


def make_brush_node(client):
    """brush：Hte.BrushSlots 控制台命令（唯一通道，skill 红线）。传输失败 = 终态。"""

    def brush_node(state: BpState) -> BpState:
        cmd, err = bpt.brush_slots_cmd(state["actor_name"], state["device_key"],
                                       state["hte_json_path"])
        if err:
            return {"error": err}
        text, err = bpt.ue_console(client, cmd)
        if err:
            return {"error": f"Hte.BrushSlots 调用失败：{err}"
                             "（检查 UE 编辑器在线 / capability token / EngineWire 能力名）"}
        first = text.splitlines()[0][:120] if text else "(无回执文本，结果以 verify 读日志为准)"
        logger.info("brush: %s → %s", cmd, first)
        return {"evidence": state.get("evidence", []) + [f"brush: {cmd} → {first}"]}

    return brush_node


def semantics_node(state: BpState) -> BpState:
    """map_semantics：SocketType/AnchorType 查表提案（配置，无模型；不猜，不写 UE）。"""
    proposals, notes = bpt.propose_semantics(
        state.get("slots_detail", []), state.get("id_type_map", {}),
        state.get("model_type", ""))
    logger.info("map_semantics: socket=%s，anchor %d 槽，提示 %d 条",
                proposals.get("socket_type") or "（留空）",
                len(proposals.get("anchor_by_slot", {})), len(notes))
    return {"semantics": proposals,
            "review_notes": state.get("review_notes", []) + notes,
            "evidence": state.get("evidence", []) + [
                f"map_semantics: SocketType 提案 {proposals.get('socket_type') or '（留空）'}，"
                f"AnchorType {len(proposals.get('anchor_by_slot', {}))} 槽，提示 {len(notes)} 条"]}


_LAYOUT_SYS = "你是实验室设备装配顾问，看图指认槽位区域（只给定性结论，不给坐标数值）。"


def make_layout_node(llm: LlmProvider | None = None):
    """propose_layout：槽键拓扑分组（纯机器）+ 可选 VL 顾问单发（资料图/截图 →
    定性区域指认）。**VL 输出只作顾问证据，坐标一律不采信**（VL 选型/分类可用，
    数值回归不可靠——ADR 决策）。"""

    def layout_node(state: BpState) -> BpState:
        topo = bpt.slot_topology(state["slot_names"])
        layout: dict[str, Any] = {"topology": topo, "vl_advice": ""}
        images = state.get("images") or []
        ev = f"propose_layout: 拓扑 {topo['kind']} {len(topo['groups'])} 组"
        if llm is not None and images and getattr(llm.cfg, "vision", False):
            prompt = (f"设备 {state['device_label']}，HTE 槽位 {state['slot_count']} 个："
                      f"{state['slot_names'][:40]}\n"
                      f"拓扑分组：{json.dumps(topo['groups'], ensure_ascii=False)}"
                      f"（未匹配名: {topo['unmatched']}）\n"
                      "任务：对照图片，指认各槽位组在设备上的物理区域（如 'A 组在转盘外圈'），"
                      "并指出与常识不符之处。只给定性结论，不要给坐标数值。")
            try:
                msg = llm.chat([ChatMessage("system", _LAYOUT_SYS),
                                ChatMessage("user", prompt, images=images)],
                               temperature=0.0, max_tokens=2048)
                layout["vl_advice"] = (msg.get("content") or "")[:1500]
                ev += "，VL 顾问已附图单发"
            except RuntimeError as exc:
                layout["vl_advice"] = f"（VL 顾问调用失败：{str(exc)[:200]}）"
                ev += f"，VL 顾问失败 {str(exc)[:80]}"
        else:
            ev += "，无 VL 顾问（未供图或模型纯文本）"
        return {"layout": layout, "evidence": state.get("evidence", []) + [ev]}

    return layout_node


def make_verify_node(client):
    """verify：机器闸（判据本身；VL 永不作判据）——
    g_log 日志行可读 / g_count 创建+跳过==槽数 / g_props 属性同步=是 /
    g_scs 刷后 SCS 集合（能力缺席降级为提示，不判死）/ g_idem 二次重刷创建=0（可选）。
    闸失败不自动修（BP 没有 LLM 可修的东西），证据上人审裁决。"""

    def verify_node(state: BpState) -> BpState:
        issues: list[dict] = []
        notes: list[str] = []
        log_text, err = bpt.ue_read_log(client, "创建", 8)
        parsed = bpt.parse_brush_log(log_text) if not err else None
        created = skipped = 0
        synced = False
        if parsed is None:
            issues.append({"gate": "brush_log",
                           "message": f"读不到刷入日志行（{err or '正则未命中'}）"})
        else:
            created, skipped, synced = parsed["created"], parsed["skipped"], parsed["props_synced"]
            if created + skipped != state["slot_count"]:
                issues.append({"gate": "slot_count",
                               "message": f"创建 {created} + 跳过 {skipped} ≠ "
                                          f"HTE 槽数 {state['slot_count']}"})
            if not synced:
                issues.append({"gate": "props_synced",
                               "message": "属性同步=否（HTE 字段未写入 BP 默认值）"})
        # SCS 集合复核（刷后重抓；能力不可用时降级为提示——能力缺席不应判死装配）
        names, serr = bpt.ue_scs_components(client, state["actor_name"])
        if serr:
            notes.append(f"SCS 复核不可用（{serr}），集合一致性未机器核对")
        else:
            missing, _, _ = bpt.diff_slot_names(state["slot_names"], names)
            if missing:
                issues.append({"gate": "scs_set",
                               "message": f"BP SCS 缺槽 {len(missing)}: {missing[:10]}"})
        # 幂等复核（可选）：重刷一次应 创建=0（Hte.BrushSlots 幂等设计）
        if state.get("verify_idempotent", True) and parsed is not None:
            cmd, cerr = bpt.brush_slots_cmd(state["actor_name"], state["device_key"],
                                            state["hte_json_path"])
            if cerr:
                notes.append(f"幂等复核命令拼接失败（{cerr}）")
            else:
                _, e2 = bpt.ue_console(client, cmd)
                log2, _ = bpt.ue_read_log(client, "创建", 8)
                p2 = bpt.parse_brush_log(log2)
                if e2 or p2 is None:
                    notes.append("幂等复核重刷失败（传输层），未计入闸")
                elif p2["created"] != 0:
                    issues.append({"gate": "idempotent",
                                   "message": f"二次刷入仍创建 {p2['created']}（应=0，幂等被破坏）"})
        ok = not issues
        logger.info("verify: 创建=%d 跳过=%d 同步=%s，%s", created, skipped,
                    "是" if synced else "否", "闸全过" if ok else f"{len(issues)} 问题")
        return {"brush_created": created, "brush_skipped": skipped, "props_synced": synced,
                "gate_ok": ok, "gate_issues": issues,
                "review_notes": state.get("review_notes", []) + notes,
                "evidence": state.get("evidence", []) + [
                    f"verify: 创建={created} 跳过={skipped} 属性同步={'是' if synced else '否'}，"
                    f"{'闸全过 ✅' if ok else f'{len(issues)} 问题 ❌'}"]}

    return verify_node


def human_gate_node(state: BpState) -> BpState:
    """human_gate：证据包落盘后 interrupt 挂起待人审（三态：批准/驳回带意见/终止）；
    非交互模式只出证据。注意此时 BP **已被 brush 修改**（先装后审）。"""
    rd = _run_dir(state)
    bundle = {
        "actor_name": state["actor_name"], "device_key": state["device_key"],
        "device_label": state.get("device_label", ""), "model_type": state.get("model_type", ""),
        "slot_count": state.get("slot_count"), "slot_names": state.get("slot_names", []),
        "brush_created": state.get("brush_created"), "brush_skipped": state.get("brush_skipped"),
        "props_synced": state.get("props_synced", False),
        "gate_ok": state.get("gate_ok", False), "gate_issues": state.get("gate_issues", []),
        "semantics": state.get("semantics", {}), "layout": state.get("layout", {}),
        "review_notes": state.get("review_notes", []),
        "rollback_on_reject": state.get("rollback_on_reject", True),
        "evidence": state.get("evidence", []),
    }
    ev_path = rd / f"{state['actor_name']}_{state['device_key']}_bp_evidence.json"
    ev_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    if not state.get("interactive", True):
        logger.info("human_gate: 非交互模式，证据包 %s", ev_path)
        return {"approved": False,
                "evidence": state.get("evidence", []) + [f"human_gate: 非交互，证据包 {ev_path}"]}
    decision = interrupt({"type": "bp_assembly_review",
                          "evidence_path": str(ev_path), **bundle})
    # 三态解析：approved=True 批准；否则 feedback 非空 = 驳回带意见（默认回滚）；皆空 = 终止
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        feedback = str(decision.get("feedback") or "").strip()
    else:
        approved, feedback = bool(decision), ""
    verdict = "批准" if approved else f"驳回: {feedback[:80]}" if feedback else "终止"
    logger.info("human_gate: 人审 %s", verdict)
    return {"approved": approved, "feedback": feedback,
            "evidence": state.get("evidence", []) + [f"human_gate: 人审 {verdict}"]}


def make_finalize_node(client):
    """finalize：批准=保存提示；驳回带意见+rollback_on_reject=ClearVirtualSlots 回滚；
    终止=保留现状。回滚只删虚拟槽，属性默认值不在清理范围（skill 红线如实上报）。"""

    def finalize_node(state: BpState) -> BpState:
        if state.get("approved"):
            return {"evidence": state.get("evidence", []) + [
                "finalize: 已批准——请在 UE 中保存蓝图资产（属性/槽位已写入 BP 与关卡实例）"]}
        if state.get("feedback") and state.get("rollback_on_reject", True):
            cmd, err = bpt.clear_slots_cmd(state["actor_name"])
            if err:
                return {"evidence": state.get("evidence", []) + [
                    f"finalize: 驳回——回滚命令拼接失败（{err}），虚拟槽未回滚"]}
            _, err = bpt.ue_console(client, cmd)
            rolled = not err
            return {"rolled_back": rolled,
                    "evidence": state.get("evidence", []) + [
                        "finalize: 驳回——" + (
                            "已回滚虚拟槽（Hte.ClearVirtualSlots）"
                            if rolled else f"回滚失败（{err}）")
                        + "；属性默认值不在清理范围（skill 红线，需人工核对）"]}
        return {"evidence": state.get("evidence", []) + [
            "finalize: 终止/未批准——槽位保留现状，未回滚"]}

    return finalize_node


def report_node(state: BpState) -> BpState:
    """report：终态文本汇总（成功/待审/失败三态）。"""
    if state.get("error"):
        return {"report": f"❌ {state['error']}"}
    lines = [f"BP 装配：{state.get('device_label', '?')} → Actor {state['actor_name']}"]
    lines.append(f"- 槽位 {state.get('slot_count')} 个；刷入 创建={state.get('brush_created')} "
                 f"跳过={state.get('brush_skipped')} "
                 f"属性同步={'是' if state.get('props_synced') else '否'}；"
                 f"机器闸 {'全过 ✅' if state.get('gate_ok') else '未过 ❌'}")
    for i in state.get("gate_issues", []):
        lines.append(f"  ✗ [{i['gate']}] {i['message']}")
    for n in state.get("review_notes", []):
        lines.append(f"  ! {n}")
    sem = state.get("semantics") or {}
    if sem.get("socket_type"):
        lines.append(f"- SocketType 提案：{sem['socket_type']}（人审参考，未写 UE）")
    advice = (state.get("layout") or {}).get("vl_advice") or ""
    if advice:
        lines.append("- VL 顾问：" + advice.splitlines()[0][:120])
    if state.get("approved"):
        lines.append("- ✅ 已批准：请在 UE 保存蓝图资产")
    elif state.get("rolled_back"):
        lines.append("- ↩ 已驳回并回滚虚拟槽；意见：" + state.get("feedback", "")[:80])
    else:
        lines.append(f"- ⏸ 未批准/非交互：证据包 {_run_dir(state)}，审批走 bp_mcp 或对话框")
    return {"report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# 图装配（边确定性；无 llm_fix——BP 没有模型可修的东西，闸失败上人审）
# ---------------------------------------------------------------------------

def build_bp_assembly(client, work_dir: Path, llm: LlmProvider | None = None,
                      checkpointer=None):
    """装配 BP 流水线图。``client`` = UeMcpClient 或同形假客户端（测试）；
    ``llm`` 可选（仅 propose_layout 的 VL 顾问用，None = 跳过顾问）。"""
    del work_dir  # 工作区路径由 state["work_dir"] 携带（与 asset 图同约定）
    g = StateGraph(BpState)
    g.add_node("fetch_device", fetch_node)
    g.add_node("pre_check", make_precheck_node(client))
    g.add_node("brush", make_brush_node(client))
    g.add_node("map_semantics", semantics_node)
    g.add_node("propose_layout", make_layout_node(llm))
    g.add_node("verify", make_verify_node(client))
    g.add_node("human_gate", human_gate_node)
    g.add_node("finalize", make_finalize_node(client))
    g.add_node("report", report_node)
    g.set_entry_point("fetch_device")

    g.add_conditional_edges("fetch_device",
                            lambda s: "report" if s.get("error") else "pre_check",
                            {"pre_check": "pre_check", "report": "report"})
    g.add_conditional_edges("pre_check",
                            lambda s: "report" if s.get("error") else "brush",
                            {"brush": "brush", "report": "report"})
    g.add_conditional_edges("brush",
                            lambda s: "report" if s.get("error") else "map_semantics",
                            {"map_semantics": "map_semantics", "report": "report"})
    g.add_edge("map_semantics", "propose_layout")
    g.add_edge("propose_layout", "verify")
    g.add_edge("verify", "human_gate")
    g.add_edge("human_gate", "finalize")  # 三态全进 finalize（批准=保存提示/驳回=回滚/终止=保留）
    g.add_edge("finalize", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


# ---------------------------------------------------------------------------
# 端到端入口
# ---------------------------------------------------------------------------

def _init_state(*, actor_name: str, device_key: str, hte_json_path: str,
                work_dir: str | Path, toml_path: str = "", images: list[str] | None = None,
                interactive: bool, rollback_on_reject: bool,
                verify_idempotent: bool) -> BpState:
    return {"actor_name": actor_name, "device_key": device_key,
            "hte_json_path": hte_json_path, "toml_path": toml_path,
            "images": images or [], "work_dir": str(work_dir),
            "interactive": interactive, "rollback_on_reject": rollback_on_reject,
            "verify_idempotent": verify_idempotent, "review_notes": [], "evidence": []}


def run_bp_assembly(
    *,
    actor_name: str,
    device_key: str,
    hte_json_path: str,
    ue_endpoint: str,
    ue_token_file: str | Path | None = None,
    work_dir: str | Path,
    toml_path: str = "",
    images: list[str] | None = None,
    llm: LlmProvider | None = None,
    interactive: bool = False,
    rollback_on_reject: bool = True,
    verify_idempotent: bool = True,
    thread_id: str = "bp-assembly",
) -> BpState:
    """一次装配的端到端入口（非交互默认：跑到 human_gate 出证据包即返）。

    交互模式（``interactive=True``）须用同一 graph + thread_id 续跑——GUI/dsh 请走
    ``start_interactive_assembly`` + ``resolve_bp_pending``（挂起态注册表）。
    """
    client = UeMcpClient(ue_endpoint, token_file=ue_token_file)
    graph = build_bp_assembly(client, Path(work_dir), llm=llm)
    init = _init_state(actor_name=actor_name, device_key=device_key,
                       hte_json_path=hte_json_path, work_dir=work_dir,
                       toml_path=toml_path, images=images, interactive=interactive,
                       rollback_on_reject=rollback_on_reject,
                       verify_idempotent=verify_idempotent)
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    return graph.invoke(init, cfg)


def resume_bp_assembly(graph, thread_id: str, approved: bool, feedback: str = "") -> BpState:
    """人审后续跑（同一 graph 实例 + 同一 thread_id）。三态：approved 批准；
    否则 feedback 非空 = 驳回带意见（默认回滚虚拟槽）；皆空 = 终止。"""
    return graph.invoke(Command(resume={"approved": approved, "feedback": feedback}),
                        {"configurable": {"thread_id": thread_id}, "recursion_limit": 100})


# ---------------------------------------------------------------------------
# 人审会话注册表（GUI/dsh 等长驻进程专用：graph+thread_id 跨消息存活，resume 才有载体）
# 为什么需要它：与 asset 图同一问题——InMemorySaver 是进程内存 dict，每条消息重建图
# 挂起态随局部变量被 GC（H3/H6b）。本表挂模块级，审批到达时按 key resume。
# ---------------------------------------------------------------------------
class PendingBpReview(TypedDict):
    graph: Any            # 编译后的流水线图（含 checkpointer，resume 载体）
    thread_id: str
    payload: dict         # interrupt 证据包（human_gate 传入的 bundle + evidence_path）
    actor_name: str


_PENDING_BP: dict[str, PendingBpReview] = {}
DEFAULT_BP_REVIEW_KEY = "bp"  # 单用户会话固定 key；与 asset 图的注册表互相独立


def pending_bp_review(key: str = DEFAULT_BP_REVIEW_KEY) -> dict | None:
    """当前待审的 BP 装配 interrupt 证据包；无待审返回 None。"""
    p = _PENDING_BP.get(key)
    return p.get("payload") if p else None


def start_interactive_assembly(
    *,
    actor_name: str,
    device_key: str,
    hte_json_path: str,
    ue_endpoint: str,
    ue_token_file: str | Path | None = None,
    work_dir: str | Path,
    toml_path: str = "",
    images: list[str] | None = None,
    llm: LlmProvider | None = None,
    rollback_on_reject: bool = True,
    verify_idempotent: bool = True,
    registry_key: str = DEFAULT_BP_REVIEW_KEY,
) -> str:
    """交互式触发装配：跑到 human_gate 挂起并注册挂起态，返回证据摘要 + 审批提示。

    未挂起（fetch/pre_check/brush 提前 error 直出 report）则直接返回终态文本。
    """
    client = UeMcpClient(ue_endpoint, token_file=ue_token_file)
    graph = build_bp_assembly(client, Path(work_dir), llm=llm)
    thread_id = f"bp-{actor_name}-{device_key}-{uuid4().hex[:8]}"
    init = _init_state(actor_name=actor_name, device_key=device_key,
                       hte_json_path=hte_json_path, work_dir=work_dir,
                       toml_path=toml_path, images=images, interactive=True,
                       rollback_on_reject=rollback_on_reject,
                       verify_idempotent=verify_idempotent)
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    final = graph.invoke(init, cfg)
    if "__interrupt__" not in final:
        return final.get("report") or f"{_FAIL}流水线无输出"
    payload = dict(final["__interrupt__"][0].value)
    _PENDING_BP[registry_key] = {"graph": graph, "thread_id": thread_id,
                                 "payload": payload, "actor_name": actor_name}
    logger.info("BP 人审挂起已注册 key=%s thread=%s", registry_key, thread_id)
    lines = [f"BP 装配已跑到人审闸门，**挂起待批**"
             f"（{payload.get('device_label')} → {actor_name}）："]
    lines.append(f"- 槽位 {payload.get('slot_count')}；创建={payload.get('brush_created')} "
                 f"跳过={payload.get('brush_skipped')} "
                 f"属性同步={'是' if payload.get('props_synced') else '否'}；"
                 f"机器闸 {'全过 ✅' if payload.get('gate_ok') else '未过 ❌'}")
    for i in payload.get("gate_issues", [])[:6]:
        lines.append(f"  ✗ [{i['gate']}] {i['message']}")
    for n in payload.get("review_notes", [])[:8]:
        lines.append(f"  ! {n}")
    lines.append(f"- 证据包：{payload.get('evidence_path')}")
    lines.append("**请审批：批准（保留并请保存 BP）/ 驳回:意见（回滚虚拟槽）/ 终止（保留现状）**")
    return "\n".join(lines)


def resolve_bp_pending(key: str = DEFAULT_BP_REVIEW_KEY, approved: bool = False,
                       feedback: str = "") -> str:
    """审批后续跑挂起的装配流水线，返回终态 report；无待审时如实说明。
    三态：approved=True 批准；否则 feedback 非空 = 驳回带意见（回滚）；皆空 = 终止。"""
    p = _PENDING_BP.pop(key, None)
    if p is None:
        return "（当前没有待审的 BP 装配流水线）"
    logger.info("BP 人审续跑 key=%s approved=%s feedback=%s", key, approved, feedback[:40])
    final = resume_bp_assembly(p["graph"], p["thread_id"], approved, feedback)
    return final.get("report") or "（无报告）"


# ---------------------------------------------------------------------------
# 配置加载（bp_mcp 与对话框 run 共用，单一来源）
# ---------------------------------------------------------------------------

def load_bp_config() -> dict:
    """读 ``config/sim_harness.toml``（相对仓库根解析；禁硬编码业务路径）。"""
    cfg = toml.load(str(_SIM_ROOT / "config" / "sim_harness.toml"))
    ue = cfg.get("unreal", {})
    paths = cfg.get("paths", {})
    work = Path(paths.get("work_dir", "lab_atuo_inteller/_work"))
    if not work.is_absolute():
        work = (_REPO_ROOT / work).resolve()
    llm_t = cfg.get("llm", {})
    return {
        "ue_endpoint": ue.get("endpoint", "http://127.0.0.1:3000/mcp"),
        "ue_token_file": ue.get("token_file") or None,
        "hte_json": paths.get("hte_json", ""),
        "work_dir": work,
        "llm_cfg": LlmConfig(
            base_url=llm_t.get("base_url", "http://localhost:11434/v1"),
            model=llm_t.get("model", "qwen2.5:7b"),
            api_key=llm_t.get("api_key", "ollama"),
            vision=bool(llm_t.get("vision", False)),
        ),
    }


# ---------------------------------------------------------------------------
# 对话框契约：route 按注册顺序问 match，命中则 run，都不命中才进 ReAct
# ---------------------------------------------------------------------------

NAME = "bp"

# BP 装配认领（与 asset 图的 TOML/Lua 认领互补，不重叠）
_MATCH_RE = re.compile(
    r"(刷槽位|槽位刷入|虚拟槽|配置槽位|装配槽位|按\s*HTE.{0,6}槽|装配\s*(蓝图|bp)|bp\s*装配|"
    r"brush.?slots?|装配\s*(actor|设备蓝图))",
    re.IGNORECASE,
)
_ACTOR_RE = re.compile(r"(BP_[A-Za-z0-9_]+)", re.IGNORECASE)
_ACTOR_KV_RE = re.compile(r"(?:actor|执行器)\s*[:=：]\s*([A-Za-z][A-Za-z0-9_]*)", re.IGNORECASE)
_KEY_KV_RE = re.compile(r"(?:device_key|设备\s*key|设备|key)\s*[:=：]?\s*(\d{1,6})", re.IGNORECASE)
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_])(\d{2,6})(?![A-Za-z0-9_])")
_JSON_RE = re.compile(r"([A-Za-z]:[\\/][^\s\"'，。,;；]+\.json)", re.IGNORECASE)
_TOML_RE = re.compile(r"([A-Za-z]:[\\/][^\s\"'，。,;；]+\.toml)", re.IGNORECASE)


def match(text: str, images: list[str] | None = None) -> bool:
    """BP 装配是否认领这条对话。"""
    del images  # 仅文字认领；附图不单独触发装配图
    return bool(_MATCH_RE.search(text or ""))


def _extract_actor(text: str) -> str:
    m = _ACTOR_KV_RE.search(text)
    if m:
        return m.group(1)
    m = _ACTOR_RE.search(text)
    return m.group(1) if m else ""


def _extract_key(text: str, actor: str) -> str:
    m = _KEY_KV_RE.search(text)
    if m:
        return m.group(1)
    # actor label 匹配跨度内的数字片段不算 key（BP_121_Capping_8a2 自带 121）——
    # 按区间排除而非按值排除（key 与 label 数字同值时按值会误杀，2026-11-10 实测）
    actor_span: tuple[int, int] | None = None
    if actor:
        am = re.search(re.escape(actor), text, re.IGNORECASE)
        if am:
            actor_span = am.span()
    for n in _NUM_RE.finditer(text):
        if actor_span and actor_span[0] <= n.start() < actor_span[1]:
            continue
        return n.group(1)
    return ""


def run(
    text: str,
    *,
    images: list[str] | None = None,
    llm_cfg: LlmConfig | None = None,
    work_dir: str | Path = "",
    blender_command: list[str] | None = None,
    interactive: bool = False,
    merge_dir: str = "",
) -> dict:
    """从对话抽出 actor label 与设备 key 并跑装配图。缺字段只回问，不进流水线。"""
    del blender_command, merge_dir  # BP 装配不用（对话契约统一形参）
    if pending_bp_review() is not None:
        return {"report": "已有 BP 装配流水线待审。请先审批（批准 / 驳回:意见 / 终止），再开新的。",
                "error": "", "screenshot_path": ""}
    actor = _extract_actor(text or "")
    device_key = _extract_key(text or "", actor)
    if not actor or not device_key:
        return {"report": "要装配 BP，请给出场景 actor 的 label（如 BP_121_Capping_8a2）"
                          "和 HTE 设备 key（如 121）。可选：HTE 档案 JSON / 运动学 TOML 路径。",
                "error": "", "screenshot_path": ""}
    cfgd = load_bp_config()
    json_m = _JSON_RE.search(text or "")
    hte_path, src_note, perr = bpt.resolve_hte_json(
        json_m.group(1) if json_m else "", cfgd["hte_json"], _REPO_ROOT)
    if perr:
        return {"report": f"❌ {perr}", "error": "", "screenshot_path": ""}
    toml_m = _TOML_RE.search(text or "")
    toml_path = toml_m.group(1) if toml_m else ""
    # VL 顾问只在「有图 + 多模态模型」时启用；否则 llm=None 走纯机器拓扑
    use_llm_cfg = llm_cfg or cfgd["llm_cfg"]
    llm = LlmProvider(use_llm_cfg) if (images and use_llm_cfg.vision) else None
    wd = Path(work_dir) if work_dir else cfgd["work_dir"]
    head = (src_note + "\n") if src_note else ""
    if interactive:
        report = start_interactive_assembly(
            actor_name=actor, device_key=device_key, hte_json_path=str(hte_path),
            ue_endpoint=cfgd["ue_endpoint"], ue_token_file=cfgd["ue_token_file"],
            work_dir=wd, toml_path=toml_path, images=images, llm=llm)
        return {"report": head + report, "error": "", "screenshot_path": ""}
    final = run_bp_assembly(
        actor_name=actor, device_key=device_key, hte_json_path=str(hte_path),
        ue_endpoint=cfgd["ue_endpoint"], ue_token_file=cfgd["ue_token_file"],
        work_dir=wd, toml_path=toml_path, images=images, llm=llm, interactive=False)
    return {"report": head + (final.get("report") or f"{_FAIL}流水线无输出"),
            "error": final.get("error") or "", "screenshot_path": ""}
