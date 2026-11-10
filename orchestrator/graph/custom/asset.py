"""确定性资产编排流水线（LangGraph 图：机器闸优先，LLM 只在语义/修复两点介入）。

链路（边是确定性路由，**LLM 不决定流程方向**——DESIGN §4）::

    fetch_scene → step1_semantics(LLM单发) → land → emit → gate → score
                                                                  ↓
    merge → report ← human_gate(interrupt) ←──────────────────────┘
              ↑         ↑  reject
              └ approve ┘
    land/emit/gate 失败 → llm_fix(LLM, retries 上限) → 回 land；到顶 → human_gate 停等人工

与 ReAct 自由环的分工（实测依据 rag/corpus/kinematics/cap8a-pitfalls：环内语义步
type 2/46 vs 专注单发 45/46，形态问题非采样噪声）：**语义判断只在 step1_semantics
单发节点**（POC5 形态：资料图+清单+规则同一条 user 消息，temperature=0）。
对话框经 ``match`` / ``run`` 整图进入；``make_onboarding_tool`` 只留作单图调试，
不挂进 ReAct 工具列表。

节点均可独立单测（机器节点 = ``asset_tools`` 模块级纯函数 + 磁盘句柄，与 ReAct
工具同一实现，两路不分叉）。人审 = LangGraph ``interrupt()``：证据包落
``asset_run/{device_key}_pipeline_evidence.json`` 后挂起，``resume_asset_pipeline``
批准/拒绝续跑；非交互模式（自由环触发器）只出证据不合入。草稿**不落业务盘**——
merge 仅在批准且配置 merge_dir 时复制（规则：生成与校验分离、人审后才落盘）。

姊妹篇：``pipeline.py`` 是 HTE JSON+电机表路线（顺序实现），本图是 Blender 场景路线。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

# kin_pad/src 进 sys.path（禁硬编码业务路径）。本文件在 graph/custom/，parents[4] = 仓库根
_KIN_PAD_SRC = Path(__file__).resolve().parents[4] / "kin_pad" / "src"
if _KIN_PAD_SRC.is_dir() and str(_KIN_PAD_SRC) not in sys.path:
    sys.path.insert(0, str(_KIN_PAD_SRC))

from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from kin_pad.mcp.asset import validate  # noqa: E402
from kin_pad.mcp.asset.spec import DeviceSpec  # noqa: E402

from llm.provider import ChatMessage, LlmConfig, LlmProvider  # noqa: E402
from orchestrator.client.mcp_stdio_client import McpStdioClient  # noqa: E402
from orchestrator.tool.asset_tools import (  # noqa: E402
    check_scene_items, emit_drafts, ingest_scene_items, land_conclusions, score_spec)
from orchestrator.tool.blender_tools import export_scene_items  # noqa: E402

logger = logging.getLogger("lab_atuo_inteller.orchestrator.graph.custom.asset")

_FAIL = "执行失败："  # Observation 失败前缀（与 nl_graph/asset_tools 同约定）
DEFAULT_BLENDER_CAPABILITY = "export_scene_objects"  # 自写薄 server 的约定工具名


# ---------------------------------------------------------------------------
# 状态（delta-only 回写：节点只 return 自己负责的键——DESIGN 附 2026-11-03 教训）
# ---------------------------------------------------------------------------
class AssetState(TypedDict, total=False):
    """流水线状态。流转：入参 → fetch 写 scene_count → step1 写 conclusions →
    land 写 joint_count/review_notes → emit 写 script_name → gate 写 gate_ok/issues →
    score 写 score_text → human_gate 写 approved → merge 写 merged → report。"""

    # 入参（invoke 时写入）
    device_key: str           # 设备 key（ASCII，如 cap8a）
    device_name: str          # 设备名（ASCII，如 CAP8A；Lua 命名取其小写）
    work_dir: str             # 证据/草稿工作区（asset_run 在其下）
    scene_path: str           # 场景导出 JSON（文件流；与 live_blender 二选一）
    live_blender: bool        # True = 从运行中的 Blender 经 MCP 实况导出
    images: list[str]         # 设备资料图 data URL（step1 VL 单发；纯文本模型自动丢弃）
    golden_toml: str          # 可选 golden TOML（给了才评分）
    golden_lua: str           # 可选 golden Lua
    script_name: str          # 可选被动 Lua 脚本名（缺省 {设备名小写}_passives.lua）
    merge_dir: str            # 批准后合入目录；空 = 不合入（草稿留 asset_run）
    interactive: bool         # True = human_gate interrupt 挂起等人；False = 只出证据
    max_retries: int          # llm_fix 自纠上限（默认 3，同 pipeline.py）
    max_revisions: int        # 人审驳回重修上限（默认 3；独立于机器 retries）
    # 过程量
    scene_count: int
    conclusions: list[dict]   # step1/llm_fix 产出的语义结论（POC5 形态 {part,motion,direction,note}）
    review_notes: list[str]   # land 人审提示（正式产物，必须全量上报）
    joint_count: int
    stage_error: str          # 可修复失败（land/emit/gate）→ llm_fix 的证据；非终态
    gate_ok: bool
    gate_issues: list[dict]
    score_text: str           # golden 评分全文（无 golden / 评分失败亦有说明行）
    retries: int
    error: str                # 终态失败（fetch/LLM 不可用/解析失败）→ report 短路
    approved: bool
    feedback: str             # 人审驳回意见（最高优先级修复证据；llm_fix 消费后清空）
    revisions: int            # 已发生的人审驳回轮次（到 max_revisions 后驳回路由关闭）
    merged: list[str]         # 已合入的业务盘路径
    evidence: list[str]       # 每节点一行简述（证据包正文）
    report: str


# ---------------------------------------------------------------------------
# step1 语义单发（POC5 形态固化：probe_poc5_step1.py 的 prompt 结构参数化）
# ---------------------------------------------------------------------------
_STEP1_SYS = "你是运动学编排专家, 正在做设备运动学的第一步语义分析。"

# 与 POC5 实测 45/46 的 prompt 逐段对齐；设备描述与清单为参数，规则文本不动
_STEP1_PROMPT = (
    "{image_note}\n"
    "下面是 Blender 场景物体清单(name/parent 装配父子)。\n\n"
    "任务(第一步·语义判断): 找出所有会运动的部件, 用大白话描述每个部件怎么动。\n"
    "对每台运动件输出:\n"
    '- part: 部件名(用清单里的名字)\n'
    '- motion: "平移" 或 "旋转"\n'
    '- direction: 运动方向, 只能从这些词选: "上下"(竖直升降) / "前后" / "左右" / '
    '"径向"(沿转盘半径方向滑动, 向心或离心) / "绕竖直"(绕竖直轴旋转)\n'
    '- note: 可选补充(如 "16个同构, 1个主动带3个镜像")\n\n'
    "约定:\n"
    "1. 宁多勿漏: 资料/结构上能合理推出的运动都列出; 工艺表没明确列的只要合理也列。\n"
    "2. 与装配层级无关: 子级件(如手指座)也列。\n"
    "3. 运动类型以资料机构动作为准, 不以名称为准: 模组名里的'旋转/拧'是模组级功能, "
    "要逐件想清楚运动到底发生在哪个零件上。\n"
    "4. 纯框架/外壳/载具/瓶子/工装等静止件不要列。\n"
    "5. 不需要判断轴字母(X/Y/Z)和坐标数值——那是第二步工具的事。\n\n"
    '只输出 JSON 数组: [{"part":..., "motion":..., "direction":..., "note":...}]\n\n'
    "物体清单:\n{catalog}"
)

_FIX_PROMPT = (
    "你此前产出的运动件语义结论在工具落地/机器闸/golden 比对中暴露问题，"
    "请修正后重发**全部**结论（不是只发改动条目）。\n\n"
    "问题清单:\n{problems}\n\n"
    "原结论 JSON:\n{old}\n\n"
    "修正要求: part 必须是清单里的部件名；motion 只有 平移/旋转；"
    "direction 只能从 上下/前后/左右/径向/绕竖直 中选；"
    "宁多勿漏（子级件也列），纯框架/瓶/工装等静止件不列。\n"
    '只输出 JSON 数组: [{"part":..., "motion":..., "direction":..., "note":...}]'
)


def _parse_conclusions(raw_text: str) -> list[dict]:
    """模型响应 → 结论数组（POC5 同款容错：剥 ``` 围栏 + 首个 JSON 段正则提取）。

    只校验结构（每条含 part/motion/direction 字符串）；方向词坏词（"径ial" 等）
    不在此拦截——land 的容错/抛错与 llm_fix 回喂是设计好的下游（pitfalls 卡）。
    接受裸数组或 ``{"conclusions": [...]}`` 包装两种形态。
    """
    raw = re.sub(r"^```(json)?|```$", "", raw_text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{].*[\]}]", raw, re.S)
    if not m:
        raise ValueError(f"响应中找不到 JSON 数组: {raw[:120]}")
    data = json.loads(m.group(0))
    if isinstance(data, dict) and isinstance(data.get("conclusions"), list):
        data = data["conclusions"]
    if not isinstance(data, list):
        raise ValueError(f"响应不是 JSON 数组: {str(data)[:120]}")
    out: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) for k in ("part", "motion", "direction")):
            raise ValueError(f"第 {i + 1} 条缺 part/motion/direction 字符串字段: {str(item)[:120]}")
        note = item.get("note", "")
        out.append({"part": item["part"], "motion": item["motion"],
                    "direction": item["direction"], "note": note if isinstance(note, str) else ""})
    if not out:
        raise ValueError("结论数组为空")
    return out


def _run_dir(state: AssetState) -> Path:
    rd = Path(state["work_dir"]) / "asset_run"
    rd.mkdir(parents=True, exist_ok=True)
    return rd


def _spec_from_disk(state: AssetState) -> DeviceSpec:
    return DeviceSpec.model_validate(
        json.loads((_run_dir(state) / "spec.json").read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def make_fetch_node(blender_client: McpStdioClient | None,
                    blender_capability: str = DEFAULT_BLENDER_CAPABILITY):
    """fetch_scene：场景数据入 asset_run/scene.json——live（Blender MCP）或文件流二选一。"""

    def fetch_node(state: AssetState) -> AssetState:
        if state.get("live_blender"):
            if blender_client is None:
                return {"error": "live_blender=true 但未配置 Blender MCP"
                                 "（sim_harness.toml [blender] command 为空）"}
            # 双契约导出：直出工具（自写薄 server）或 blender-mcp execute_blender_code 注片段
            items, fail = export_scene_items(blender_client, blender_capability)
            if fail:
                return {"error": f"Blender 实况导出失败：{fail[:300]}"}
            label = "Blender 实况"
        else:
            p = Path(state.get("scene_path") or "")
            if not p.is_file():
                return {"error": f"场景文件不存在（且未开 live_blender）: {p}"}
            try:
                items = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                return {"error": f"场景文件解析失败: {exc}"}
            label = p.name
        err = check_scene_items(items)
        if err:
            return {"error": err}
        ingest_scene_items(items, _run_dir(state), label)
        logger.info("fetch_scene: %s，%d 个物体", label, len(items))
        return {"scene_count": len(items),
                "evidence": state.get("evidence", []) + [f"fetch_scene: {label} {len(items)} 物体"]}

    return fetch_node


def make_step1_node(llm: LlmProvider):
    """step1_semantics：LLM 唯一语义介入点（专注单发，temperature=0，解析失败重试一次）。"""

    def step1_node(state: AssetState) -> AssetState:
        items = json.loads((_run_dir(state) / "scene.json").read_text(encoding="utf-8"))
        catalog = [{"name": i["name"], "parent": i.get("parent")} for i in items]
        images = state.get("images") or []
        has_img = bool(images) and bool(getattr(llm.cfg, "vision", False))
        if images and not has_img:
            logger.info("step1: 纯文本模型，%d 张资料图丢弃", len(images))
        image_note = (f"附件是这台{state['device_name']}设备的资料(爆炸图 + 机构动作表)。"
                      if has_img else "（本轮无设备资料图，仅凭清单与机械常识判断。）")
        # 模板含 JSON 示例花括号，不能用 str.format——用 replace 防 KeyError
        prompt = (_STEP1_PROMPT
                  .replace("{image_note}", image_note)
                  .replace("{catalog}", json.dumps(catalog, ensure_ascii=False)))
        msgs = [ChatMessage("system", _STEP1_SYS),
                ChatMessage("user", prompt, images=(images if has_img else None))]
        last_err = ""
        for _ in range(2):  # 解析失败回推重发一次
            try:
                msg = llm.chat(msgs, temperature=0.0, max_tokens=8192)
                concl = _parse_conclusions(msg.get("content") or "")
            except (RuntimeError, ValueError) as exc:
                last_err = str(exc)[:300]
                logger.warning("step1 结论解析失败，回推重发: %s", last_err)
                msgs.append(ChatMessage(
                    "user", f"结论解析失败（{last_err}）。只输出 JSON 数组，重发全部结论。"))
                continue
            logger.info("step1: 语义结论 %d 条", len(concl))
            return {"conclusions": concl,
                    "evidence": state.get("evidence", []) + [f"step1: 语义结论 {len(concl)} 条"]}
        return {"error": f"step1 语义单发失败（重试后仍无法解析结论）：{last_err}"}

    return step1_node


def land_node(state: AssetState) -> AssetState:
    """land：语义结论 → JointSpec（轴判定/主从分组，全机器）。失败=可修复，转 llm_fix。"""
    spec, notes, err = land_conclusions(state.get("conclusions", []), _run_dir(state),
                                        state["device_key"], state["device_name"])
    if err:
        return {"stage_error": f"land: {err}"}
    logger.info("land: %d 关节，人审提示 %d 条", len(spec.joints), len(notes))
    return {"stage_error": "", "review_notes": notes, "joint_count": len(spec.joints),
            "evidence": state.get("evidence", []) + [
                f"land: {len(spec.joints)} 关节，人审提示 {len(notes)} 条"]}


def emit_node(state: AssetState) -> AssetState:
    """emit：spec → TOML/Lua 草稿（不落业务盘）。失败=可修复，转 llm_fix。"""
    spec = _spec_from_disk(state)
    toml_text, lua_text, script, err = emit_drafts(spec, _run_dir(state),
                                                   state.get("script_name", ""))
    if err:
        return {"stage_error": f"emit: {err}"}
    return {"stage_error": "", "script_name": script,
            "evidence": state.get("evidence", []) + [
                f"emit: draft.toml {'+ draft.lua' if lua_text else '(无被动 Lua)'}, script={script}"]}


def gate_node(state: AssetState) -> AssetState:
    """gate：机器闸（toml_ascii/linkage/lua_compile）。不过=可修复，转 llm_fix。"""
    rd = _run_dir(state)
    spec = _spec_from_disk(state)
    toml_text = (rd / "draft.toml").read_text(encoding="utf-8")
    report = validate.run_all_gates(spec, toml_text)
    (rd / "gate_report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    issues = [{"gate": i.gate, "location": i.location, "message": i.message}
              for i in report.issues]
    logger.info("gate: %s（%d 问题）", "全过" if report.ok else "未过", len(issues))
    return {"gate_ok": report.ok, "gate_issues": issues,
            "evidence": state.get("evidence", []) + [
                f"gate: {'全过' if report.ok else f'{len(issues)} 问题 ❌'}"]}


def score_node(state: AssetState) -> AssetState:
    """score：golden 比对（证据非硬闸——低分由 human_gate 人决定，不自动回修）。"""
    if not state.get("golden_toml"):
        return {"score_text": "（无 golden，未评分）",
                "evidence": state.get("evidence", []) + ["score: 无 golden，跳过"]}
    spec = _spec_from_disk(state)
    result, res = score_spec(spec, state["golden_toml"], state.get("golden_lua", ""))
    if result is None:
        return {"score_text": f"（评分失败：{res}）",
                "evidence": state.get("evidence", []) + [f"score: 评分失败 {res}"]}
    (_run_dir(state) / "score.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("score: %s", res.splitlines()[0])
    return {"score_text": res,
            "evidence": state.get("evidence", []) + [f"score: {res.splitlines()[0]}"]}


def make_fix_node(llm: LlmProvider):
    """llm_fix：收结构化证据（stage_error/gate_issues/score）→ 修结论（有界 retries）。"""

    def fix_node(state: AssetState) -> AssetState:
        retries = state.get("retries", 0) + 1
        problems: list[str] = []
        # 人审驳回意见 = 最高优先级证据（dsh 三态之"驳回带意见"），消费后清空
        if state.get("feedback"):
            problems.insert(0, f"【人审驳回意见·最高优先级】{state['feedback']}")
        if state.get("stage_error"):
            problems.append(f"落地/生成失败: {state['stage_error']}")
        for i in state.get("gate_issues", [])[:10]:
            problems.append(f"[{i['gate']}] {i.get('location', '')}: {i['message'][:120]}")
        if state.get("score_text") and "（" not in state["score_text"][:2]:
            problems.append("golden 比对:\n" + state["score_text"][:800])
        prompt = (_FIX_PROMPT
                  .replace("{problems}", "\n".join(problems) or "（无明细，请按约定全面自查）")
                  .replace("{old}", json.dumps(state.get("conclusions", []), ensure_ascii=False)))
        msgs = [ChatMessage("system", _STEP1_SYS), ChatMessage("user", prompt)]
        try:
            msg = llm.chat(msgs, temperature=0.0, max_tokens=8192)
            concl = _parse_conclusions(msg.get("content") or "")
        except (RuntimeError, ValueError) as exc:
            # 修复失败也算一次 retry（stage_error 保留原证据），由 retries 上限收口
            return {"retries": retries, "feedback": "",
                    "evidence": state.get("evidence", []) + [
                        f"llm_fix[{retries}]: 修复响应解析失败 {str(exc)[:120]}"]}
        logger.info("llm_fix[%d]: 重发结论 %d 条（人审意见 %s）", retries, len(concl),
                    "已采纳" if state.get("feedback") else "无")
        return {"retries": retries, "conclusions": concl, "feedback": "",
                "evidence": state.get("evidence", []) + [
                    f"llm_fix[{retries}]: 重发结论 {len(concl)} 条"
                    + (f"（采纳人审意见: {state['feedback'][:80]}）" if state.get("feedback") else "")]}

    return fix_node


def human_gate_node(state: AssetState) -> AssetState:
    """human_gate：证据包落盘后 interrupt 挂起待人审（三态：批准/驳回带意见/终止）；
    非交互模式只出证据不合入。"""
    rd = _run_dir(state)
    bundle = {
        "device_key": state["device_key"], "device_name": state["device_name"],
        "joint_count": state.get("joint_count"), "gate_ok": state.get("gate_ok", False),
        "gate_issues": state.get("gate_issues", []),
        "score_text": state.get("score_text", ""),
        "review_notes": state.get("review_notes", []),
        "retries": state.get("retries", 0),
        "revisions": state.get("revisions", 0),
        "max_revisions": state.get("max_revisions", 3),
        "draft_toml": str(rd / "draft.toml"),
        "draft_lua": str(rd / "draft.lua") if (rd / "draft.lua").is_file() else "",
        "script_name": state.get("script_name", ""),
        "merge_dir": state.get("merge_dir", ""),
        "evidence": state.get("evidence", []),
    }
    ev_path = rd / f"{state['device_key']}_pipeline_evidence.json"
    ev_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    if not state.get("interactive", True):
        logger.info("human_gate: 非交互模式，证据包 %s", ev_path)
        return {"approved": False,
                "evidence": state.get("evidence", []) + [f"human_gate: 非交互，证据包 {ev_path}"]}
    decision = interrupt({"type": "asset_pipeline_review",
                          "evidence_path": str(ev_path), **bundle})
    # 三态解析：approved=True 批准；feedback 非空 = 驳回带意见重修；其余 = 终止
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        feedback = str(decision.get("feedback") or "").strip()
    else:
        approved, feedback = bool(decision), ""
    revisions = state.get("revisions", 0) + (1 if (feedback and not approved) else 0)
    verdict = ("批准" if approved else
               f"驳回重修[{revisions}]: {feedback[:80]}" if feedback else "终止")
    logger.info("human_gate: 人审 %s", verdict)
    out: AssetState = {"approved": approved, "feedback": feedback, "revisions": revisions,
                       "evidence": state.get("evidence", []) + [f"human_gate: 人审 {verdict}"]}
    if feedback and not approved:
        # 人给了方向 = 新一轮：机器自纠预算重置（历史在 evidence 里可查）
        out["retries"] = 0
    return out


def merge_node(state: AssetState) -> AssetState:
    """merge：批准 + 配置 merge_dir 才复制草稿出 asset_run（业务落盘只此一处）。"""
    rd = _run_dir(state)
    tgt = (state.get("merge_dir") or "").strip()
    if not tgt:
        return {"merged": [],
                "evidence": state.get("evidence", []) + [
                    "merge: 未配置 merge_dir，草稿留 asset_run 待人工合入"]}
    md = Path(tgt)
    md.mkdir(parents=True, exist_ok=True)
    merged: list[str] = []
    src_toml = rd / "draft.toml"
    if src_toml.is_file():
        dst = md / f"{state['device_name']}.toml"
        shutil.copy2(src_toml, dst)
        merged.append(str(dst))
    src_lua = rd / "draft.lua"
    if src_lua.is_file():
        dst = md / (state.get("script_name") or f"{state['device_name'].lower()}_passives.lua")
        shutil.copy2(src_lua, dst)
        merged.append(str(dst))
    logger.info("merge: 合入 %s", merged)
    return {"merged": merged,
            "evidence": state.get("evidence", []) + [f"merge: 合入 {len(merged)} 文件 → {tgt}"]}


def report_node(state: AssetState) -> AssetState:
    """report：终态文本汇总（成功/待审/失败三态）。"""
    if state.get("error"):
        return {"report": f"❌ {state['error']}"}
    rd = _run_dir(state)
    lines = [f"设备 {state['device_name']}（{state['device_key']}）资产编排流水线："]
    lines.append(f"- 场景 {state.get('scene_count', '?')} 物体 → 语义结论 "
                 f"{len(state.get('conclusions') or [])} 条 → 关节 {state.get('joint_count', '?')} 个"
                 f"；机器闸 {'全过 ✅' if state.get('gate_ok') else '未过 ❌'}"
                 + (f"（自纠 {state.get('retries', 0)} 次）" if state.get("retries") else ""))
    if state.get("score_text"):
        lines.append("- " + state["score_text"].splitlines()[0])
    for n in state.get("review_notes", []):
        lines.append(f"  ! {n}")
    drafts = str(rd / "draft.toml") + (f"、{rd / 'draft.lua'}" if (rd / "draft.lua").is_file() else "")
    lines.append(f"- 草稿：{drafts}")
    if state.get("approved") and state.get("merged"):
        lines.append("- ✅ 已批准合入：" + "、".join(state["merged"]))
    elif state.get("approved"):
        lines.append("- ✅ 已批准；未配置 merge_dir，草稿在 asset_run 待人工合入")
    else:
        lines.append("- ⏸ 待审/未批准：草稿不落业务盘；证据包 "
                     f"{rd / (state['device_key'] + '_pipeline_evidence.json')}；"
                     "批准合入走 CLI：python -m orchestrator.run_asset")
    return {"report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# 边（确定性路由；LLM 不决定方向）
# ---------------------------------------------------------------------------

def _fix_or_human(s: AssetState) -> str:
    """可修复失败的统一出口：retries 到顶 → human_gate 停等人工，否则 llm_fix。"""
    if s.get("error"):
        return "report"
    return "human_gate" if s.get("retries", 0) >= s.get("max_retries", 3) else "llm_fix"


def build_asset_pipeline(llm: LlmProvider, work_dir: Path,
                         blender_client: McpStdioClient | None = None,
                         checkpointer=None,
                         blender_capability: str = DEFAULT_BLENDER_CAPABILITY):
    """装配流水线图。``checkpointer`` 缺省 InMemorySaver（interrupt 硬要求）。"""
    g = StateGraph(AssetState)
    g.add_node("fetch_scene", make_fetch_node(blender_client, blender_capability))
    g.add_node("step1_semantics", make_step1_node(llm))
    g.add_node("land", land_node)
    g.add_node("emit", emit_node)
    g.add_node("gate", gate_node)
    g.add_node("score", score_node)
    g.add_node("llm_fix", make_fix_node(llm))
    g.add_node("human_gate", human_gate_node)
    g.add_node("merge", merge_node)
    g.add_node("report", report_node)
    g.set_entry_point("fetch_scene")

    g.add_conditional_edges("fetch_scene",
                            lambda s: "report" if s.get("error") else "step1_semantics",
                            {"step1_semantics": "step1_semantics", "report": "report"})
    g.add_conditional_edges("step1_semantics",
                            lambda s: "report" if s.get("error") else "land",
                            {"land": "land", "report": "report"})
    g.add_conditional_edges("land",
                            lambda s: _fix_or_human(s) if s.get("stage_error") else "emit",
                            {"emit": "emit", "llm_fix": "llm_fix",
                             "human_gate": "human_gate", "report": "report"})
    g.add_conditional_edges("emit",
                            lambda s: _fix_or_human(s) if s.get("stage_error") else "gate",
                            {"gate": "gate", "llm_fix": "llm_fix",
                             "human_gate": "human_gate", "report": "report"})
    g.add_conditional_edges("gate",
                            lambda s: "score" if s.get("gate_ok") else _fix_or_human(s),
                            {"score": "score", "llm_fix": "llm_fix",
                             "human_gate": "human_gate", "report": "report"})
    g.add_edge("score", "human_gate")
    g.add_conditional_edges("llm_fix",
                            lambda s: "land",
                            {"land": "land"})
    # 三态路由：批准→merge；驳回带意见且轮次未用尽→llm_fix（重修后再回 human_gate）；
    # 终止 / 驳回轮次用尽→report（草稿留 asset_run，证据包可溯）
    g.add_conditional_edges(
        "human_gate",
        lambda s: ("merge" if s.get("approved")
                   else "llm_fix" if (s.get("feedback") and
                                      s.get("revisions", 0) <= s.get("max_revisions", 3))
                   else "report"),
        {"merge": "merge", "llm_fix": "llm_fix", "report": "report"})
    g.add_edge("merge", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


# ---------------------------------------------------------------------------
# 端到端入口
# ---------------------------------------------------------------------------

def run_asset_pipeline(
    *,
    device_key: str,
    device_name: str,
    work_dir: str | Path,
    llm: LlmProvider | None = None,
    llm_cfg: LlmConfig | None = None,
    scene_path: str = "",
    live_blender: bool = False,
    blender_command: list[str] | None = None,
    images: list[str] | None = None,
    golden_toml: str = "",
    golden_lua: str = "",
    script_name: str = "",
    merge_dir: str = "",
    interactive: bool = False,
    max_retries: int = 3,
    thread_id: str = "asset-pipeline",
) -> AssetState:
    """一次编排的端到端入口（非交互默认：跑到 human_gate 出证据包即返）。

    交互模式（``interactive=True``）：human_gate 挂起时返回的 state 含
    ``__interrupt__``，须用同一 graph + thread_id 经 ``resume_asset_pipeline`` 续跑
    （本函数每次新建 graph，故交互模式建议直接用 ``build_asset_pipeline``，见 run_asset CLI）。
    """
    llm = llm or LlmProvider(llm_cfg)
    client = McpStdioClient(blender_command) if (live_blender and blender_command) else None
    graph = build_asset_pipeline(llm, Path(work_dir), blender_client=client)
    init: AssetState = {
        "device_key": device_key, "device_name": device_name,
        "work_dir": str(work_dir), "scene_path": scene_path,
        "live_blender": live_blender, "images": images or [],
        "golden_toml": golden_toml, "golden_lua": golden_lua,
        "script_name": script_name, "merge_dir": merge_dir,
        "interactive": interactive, "max_retries": max_retries,
        "retries": 0, "revisions": 0, "evidence": [],
    }
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    try:
        return graph.invoke(init, cfg)
    finally:
        if client is not None:
            client.close()


def resume_asset_pipeline(graph, thread_id: str, approved: bool,
                          feedback: str = "") -> AssetState:
    """人审后续跑（同一 graph 实例 + 同一 thread_id）。三态：approved 批准；
    否则 feedback 非空 = 驳回带意见重修；皆空 = 终止。"""
    return graph.invoke(Command(resume={"approved": approved, "feedback": feedback}),
                        {"configurable": {"thread_id": thread_id}, "recursion_limit": 100})


# ---------------------------------------------------------------------------
# 人审会话注册表（GUI 等长驻进程专用：graph+thread_id 跨消息存活，resume 才有载体）
# ---------------------------------------------------------------------------
# 为什么需要它：interrupt/resume 三要素 = 同一 graph 实例 + 同一 checkpointer +
# 同一 thread_id，而 InMemorySaver 是进程内存 dict。GUI 每条消息重建图的话挂起态
# 随局部变量被 GC，下一条消息无东西可 resume（H3/H6b 的核心问题）。本表把挂起态
# 挂在模块级（Chainlit 进程常驻），审批到达时按 key resume。单用户单会话够用；
# dict 读写 GIL 原子，Chainlit async  handler 与 make_async 线程间无需加锁。
class PendingReview(TypedDict):
    graph: Any            # 编译后的流水线图（含 checkpointer，resume 载体）
    thread_id: str
    payload: dict         # interrupt 证据包（human_gate 传入的 bundle + evidence_path）
    device_name: str


_PENDING: dict[str, PendingReview] = {}
DEFAULT_REVIEW_KEY = "default"  # 单用户 GUI 会话固定 key；多会话远期换 session id


def pending_review(key: str = DEFAULT_REVIEW_KEY) -> dict | None:
    """当前待审的 interrupt 证据包；无待审返回 None。"""
    p = _PENDING.get(key)
    return p.get("payload") if p else None


def start_interactive_onboarding(
    *,
    device_key: str,
    device_name: str,
    work_dir: str | Path,
    llm: LlmProvider,
    scene_path: str = "",
    live_blender: bool = False,
    blender_command: list[str] | None = None,
    images: list[str] | None = None,
    golden_toml: str = "",
    golden_lua: str = "",
    merge_dir: str = "",
    max_retries: int = 3,
    registry_key: str = DEFAULT_REVIEW_KEY,
) -> str:
    """交互式触发流水线：跑到 human_gate 挂起并注册挂起态，返回证据摘要 + 审批提示。

    未挂起（fetch/step1 提前 error 直出 report）则直接返回终态文本。
    """
    client = McpStdioClient(blender_command) if (live_blender and blender_command) else None
    graph = build_asset_pipeline(llm, Path(work_dir), blender_client=client)
    thread_id = f"asset-{device_key}-{uuid4().hex[:8]}"
    init: AssetState = {
        "device_key": device_key, "device_name": device_name,
        "work_dir": str(work_dir), "scene_path": scene_path,
        "live_blender": live_blender, "images": images or [],
        "golden_toml": golden_toml, "golden_lua": golden_lua,
        "script_name": "", "merge_dir": merge_dir,
        "interactive": True, "max_retries": max_retries,
        "retries": 0, "revisions": 0, "evidence": [],
    }
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    try:
        final = graph.invoke(init, cfg)
    finally:
        if client is not None:
            client.close()  # fetch 已完成；resume 路径（merge/report）不碰 Blender
    if "__interrupt__" not in final:
        return final.get("report") or f"{_FAIL}流水线无输出"
    payload = dict(final["__interrupt__"][0].value)
    _PENDING[registry_key] = {"graph": graph, "thread_id": thread_id,
                              "payload": payload, "device_name": device_name}
    logger.info("人审挂起已注册 key=%s thread=%s", registry_key, thread_id)
    lines = [f"流水线已跑到人审闸门，**挂起待批**（设备 {device_name}）："]
    lines.append(f"- 关节 {payload.get('joint_count')} 个；机器闸 "
                 f"{'全过 ✅' if payload.get('gate_ok') else '未过 ❌'}"
                 f"；自纠 {payload.get('retries')} 次")
    for ln in (payload.get("score_text") or "").splitlines()[:4]:
        lines.append("  " + ln)
    for n in payload.get("review_notes", []):
        lines.append(f"  ! {n}")
    lines.append(f"- 草稿：{payload.get('draft_toml')}"
                 + (f"、{payload.get('draft_lua')}" if payload.get("draft_lua") else ""))
    lines.append(f"- 证据包：{payload.get('evidence_path')}")
    lines.append("**请审批：回复 批准（合入）或 拒绝（放弃草稿）**")
    return "\n".join(lines)


def resolve_pending(key: str = DEFAULT_REVIEW_KEY, approved: bool = False,
                    feedback: str = "") -> str:
    """审批后续跑挂起的流水线，返回终态 report；无待审时如实说明。
    三态：approved=True 批准；否则 feedback 非空 = 驳回带意见重修（会再挂起，
    挂起态重新注册进 _PENDING[key]）；皆空 = 终止。"""
    p = _PENDING.pop(key, None)
    if p is None:
        return "（当前没有待审的流水线）"
    logger.info("人审续跑 key=%s approved=%s feedback=%s", key, approved, feedback[:40])
    final = resume_asset_pipeline(p["graph"], p["thread_id"], approved, feedback)
    # 驳回重修 → 流水线再次挂起：重新注册挂起态，等下一轮审批
    intr = final.get("__interrupt__")
    if intr:
        payload = intr[0].value
        _PENDING[key] = {"graph": p["graph"], "thread_id": p["thread_id"],
                         "payload": payload, "device_name": payload.get("device_name", "?")}
        logger.info("人审续跑后再次挂起 key=%s revisions=%s", key, payload.get("revisions"))
        return (f"📝 已采纳驳回意见重修一轮，流水线再次挂起待审（第 "
                f"{payload.get('revisions')}/{payload.get('max_revisions', 3)} 轮驳回）：\n"
                f"- 关节 {payload.get('joint_count')} 个；机器闸 "
                f"{'全过 ✅' if payload.get('gate_ok') else '未过 ❌'}；自纠 {payload.get('retries')} 次\n"
                f"- 证据包：`{payload.get('evidence_path')}`\n"
                "请继续审批（批准 / 驳回:意见 / 终止）")
    return final.get("report") or "（无报告）"


# ---------------------------------------------------------------------------
# 自由环触发器工具（双模衔接：模型只调度，不做语义——pitfalls 卡结论）
# ---------------------------------------------------------------------------

class _OnboardingArgs(BaseModel):
    device_key: str = Field(description="设备 key（ASCII，如 cap8a）")
    device_name: str = Field(description="设备名（ASCII，如 CAP8A；被动 Lua 命名取其小写）")
    scene_path: str = Field(default="", description="Blender 场景导出 JSON 绝对路径；"
                                                    "与 use_live_blender 二选一")
    use_live_blender: bool = Field(default=False, description="true = 从正在运行的 Blender 实况"
                                                              "导出场景（需已配置 Blender MCP）")
    golden_toml: str = Field(default="", description="可选：golden TOML 绝对路径（给了才评分）")
    golden_lua: str = Field(default="", description="可选：golden Lua 绝对路径")


def make_onboarding_tool(llm: LlmProvider, work_dir: Path,
                         blender_command: list[str] | None = None,
                         images: list[str] | None = None,
                         *, interactive: bool = False,
                         merge_dir: str = "",
                         registry_key: str = DEFAULT_REVIEW_KEY):
    """工厂：注入 LLM + 工作区，产出 run_asset_onboarding @tool（单图调试，不挂 ReAct）。

    对话框整机编排走本模块 ``match`` / ``run``。``interactive=False``（一次性进程）：
    跑到 human_gate 出证据包即回报。``interactive=True``（GUI 长驻进程）：挂起态注册进
    ``_PENDING[registry_key]``，审批经 ``resolve_pending`` 续跑——resume 三要素
    （同 graph/同 checkpointer/同 thread_id）因此跨消息存活。
    """

    @tool(args_schema=_OnboardingArgs)
    def run_asset_onboarding(device_key: str, device_name: str, scene_path: str = "",
                             use_live_blender: bool = False, golden_toml: str = "",
                             golden_lua: str = "") -> str:
        """整机运动学资产编排（确定性流水线：读场景→语义单发→落地→生成→机器闸→golden评分
        →人审闸门）。对话框不挂本工具，走 match/run。单图调试可直接调，不要逐个
        asset_* 自由编排；asset_* 只用于单步调试或失败后的手动补救。草稿不落业务盘：
        到人审闸门会挂起等"批准/拒绝"，批准且配置了合入目录才落业务盘。"""
        if not scene_path and not use_live_blender and blender_command:
            use_live_blender = True  # 已配置实况源时的合理默认（用户："Blender 正开着"）
        if not scene_path and not use_live_blender:
            return (f"{_FAIL}未执行：需要场景来源。若用户说 Blender 正开着，重试本工具并"
                    "显式传 use_live_blender=true；否则用一句话向用户要 Blender 场景导出"
                    " JSON 的绝对路径（拿到后给 scene_path 重试），然后结束本轮。"
                    "禁止转向 unreal 网关搜 Blender/导出能力——那里只有 UE 编辑器能力。")
        # 前置闸：实况源不可用就 fail-fast，不进流水线空跑一轮；
        # 指令级措辞（问用户/禁止转 UE）——实测模型拿到含糊报错会转去 UE 乱探测撞 probe 闸
        if use_live_blender and not blender_command:
            return (f"{_FAIL}use_live_blender=true 但本机未配置 Blender MCP"
                    "（sim_harness.toml [blender] command 为空，实况导出不可用）。"
                    "禁止转向 UE search/describe（UE 里没有场景装配数据）；"
                    "正确动作：用一句话向用户要 Blender 场景导出 JSON 的绝对路径"
                    "（拿到后调 scene_path 重试），或请用户配置 [blender] command，然后结束本轮。")
        if interactive:
            return start_interactive_onboarding(
                device_key=device_key, device_name=device_name, work_dir=work_dir, llm=llm,
                scene_path=scene_path, live_blender=use_live_blender,
                blender_command=blender_command, images=images,
                golden_toml=golden_toml, golden_lua=golden_lua,
                merge_dir=merge_dir, registry_key=registry_key)
        final = run_asset_pipeline(
            device_key=device_key, device_name=device_name, work_dir=work_dir, llm=llm,
            scene_path=scene_path, live_blender=use_live_blender,
            blender_command=blender_command, images=images,
            golden_toml=golden_toml, golden_lua=golden_lua, interactive=False)
        return final.get("report") or f"{_FAIL}流水线无输出"

    return run_asset_onboarding


# ---------------------------------------------------------------------------
# 对话框契约：route 按注册顺序问 match，命中则 run，都不命中才进 ReAct
# ---------------------------------------------------------------------------

NAME = "asset"

# 整机编排认领。附图本身不认领，普通看图问答仍走 ReAct
_MATCH_RE = re.compile(
    r"(接入新设备|新设备接入|资产编排|运动学资产|onboarding|"
    r"(生成|建立|起草|出)\s*(toml|lua)|"
    r"(toml|lua)\s*(草稿|资产))",
    re.IGNORECASE,
)
_DEVICE_RE = re.compile(
    r"(?:接入新设备|新设备|设备名|device_name|设备|device)"
    r"\s*[:=：是为]?\s*([A-Za-z][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_KEY_RE = re.compile(
    r"(?:device_key|设备\s*key|设备键)\s*[:=：]?\s*([A-Za-z][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_SCENE_RE = re.compile(
    r"([A-Za-z]:[\\/][^\s\"'，。,;；]+\.json"
    r"|(?:\.{1,2}[\\/])?[^\s\"'，。,;；]+\.json)",
    re.IGNORECASE,
)
_LIVE_RE = re.compile(
    r"(live[_\s-]?blender|实况导出|blender\s*正(在)?开着?|正在运行的\s*blender)",
    re.IGNORECASE,
)


def match(text: str, images: list[str] | None = None) -> bool:
    """整机资产编排是否认领这条对话。"""
    del images  # 仅文字认领；附图不单独触发资产图
    return bool(_MATCH_RE.search(text or ""))


def _resolve_scene(raw: str, work_dir: Path) -> str:
    """相对路径按 cwd、work_dir、其上两级目录依次找；找不到则原样交流水线报错。"""
    p = Path(raw.strip().strip("\"'"))
    if p.is_file():
        return str(p.resolve())
    for base in (Path.cwd(), work_dir, work_dir.parent, work_dir.parent.parent):
        cand = base / p
        if cand.is_file():
            return str(cand.resolve())
    return str(p)


def run(
    text: str,
    *,
    images: list[str] | None = None,
    llm: LlmProvider | None = None,
    llm_cfg: LlmConfig | None = None,
    work_dir: str | Path,
    blender_command: list[str] | None = None,
    interactive: bool = False,
    merge_dir: str = "",
) -> dict:
    """从对话抽出设备名和场景来源并跑资产图。缺字段只回问，不进流水线。"""
    device_m = _DEVICE_RE.search(text or "")
    device_name = device_m.group(1) if device_m else ""
    if not device_name:
        return {
            "report": "要接入新设备，请给出设备名（ASCII，如 CAP8A）和场景来源"
                      "（导出 JSON 路径，或说明 Blender 正开着）。",
            "error": "",
            "screenshot_path": "",
        }
    key_m = _KEY_RE.search(text or "")
    device_key = key_m.group(1).lower() if key_m else device_name.lower()
    scene_m = _SCENE_RE.search(text or "")
    scene_path = _resolve_scene(scene_m.group(1), Path(work_dir)) if scene_m else ""
    live = bool(_LIVE_RE.search(text or "")) and not scene_path
    if not scene_path and not live:
        return {
            "report": f"设备 {device_name} 还缺场景来源。请给 Blender 导出 JSON 的路径，"
                      "或说明 Blender 正开着。",
            "error": "",
            "screenshot_path": "",
        }
    if live and not blender_command:
        return {
            "report": "Blender 实况导出需要 config/sim_harness.toml 的 [blender] command。"
                      "请改给场景导出 JSON 的路径，或先配好 Blender MCP。",
            "error": "",
            "screenshot_path": "",
        }
    model = llm or LlmProvider(llm_cfg)
    wd = Path(work_dir)
    if interactive:
        report = start_interactive_onboarding(
            device_key=device_key, device_name=device_name, work_dir=wd, llm=model,
            scene_path=scene_path, live_blender=live, blender_command=blender_command,
            images=images, merge_dir=merge_dir)
        return {"report": report, "error": "", "screenshot_path": ""}
    final = run_asset_pipeline(
        device_key=device_key, device_name=device_name, work_dir=wd, llm=model,
        scene_path=scene_path, live_blender=live, blender_command=blender_command,
        images=images, merge_dir=merge_dir, interactive=False)
    return {
        "report": final.get("report") or f"{_FAIL}流水线无输出",
        "error": final.get("error") or "",
        "screenshot_path": "",
    }
