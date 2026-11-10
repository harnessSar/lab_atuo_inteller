"""sim_harness 自然语言 GUI 入口（Chainlit 薄面板，DESIGN.md §11 H6）。

启动（或双击 ui/启动GUI.cmd）：
    D:\\project\\kin_pad\\.venv\\Scripts\\python.exe -m chainlit run ui\\chat_ui.py --port 8501
浏览器打开 http://localhost:8501 （端口勿用 8000：UE MCP 占用）。

链路同 CLI（orchestrator/chat.py）：NL → route（自定义图优先，未命中再 ReAct）。
模型连接读 config/sim_harness.toml [llm]（Ollama/vLLM 只改配置）；UE 端点读 [unreal]；
Blender MCP 读 [blender]；人审合入目录读 [asset] merge_dir。

人审模式（H3/H6b）：自由环触发 run_asset_onboarding 后流水线挂起在 human_gate，
本面板发审批卡片——**三态**（dsh 式）：批准合入 / 驳回带意见重修（≤3 轮，意见进
llm_fix 最高优先级证据）/ 终止；按钮 + 文字双通道。挂起态经
orchestrator.graph.custom.asset._PENDING 常驻本进程（graph+thread_id 跨消息存活，
resume 三要素齐备）；审批意图由**正则硬路由**判定，不经模型（机器闸优先）。
"""

from __future__ import annotations

import base64
import re
import shlex
import sys
from pathlib import Path

import chainlit as cl
import toml

_SIM_ROOT = Path(__file__).resolve().parents[1]
if str(_SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SIM_ROOT))

from llm.provider import LlmConfig  # noqa: E402
from orchestrator.graph.custom.asset import pending_review, resolve_pending  # noqa: E402
from orchestrator.graph.custom.bp_assembly import (  # noqa: E402
    pending_bp_review, resolve_bp_pending)
from orchestrator.route import dispatch  # noqa: E402

# 审批硬路由（整词匹配，防"不批准"误判）：三态 = 批准 / 驳回带意见 / 终止
_APPROVE_RE = re.compile(r"^\s*(批准|同意|通过|合入|approve|yes|y)\s*[。！!]?\s*$", re.IGNORECASE)
_TERMINATE_RE = re.compile(r"^\s*(终止|拒绝|不批|放弃|作废|reject|no|n|abort|stop)\s*[。！!]?\s*$",
                           re.IGNORECASE)
_REVISE_RE = re.compile(r"^\s*(?:驳回|重修|打回|revise|redo)\s*[:：]?\s*(.*)$",
                        re.IGNORECASE | re.DOTALL)
# 点了"驳回重修"按钮后，下一条文本消息 = 驳回意见（模块级标志，单用户会话够用）
_AWAIT_FEEDBACK = False
# BP 装配审批的等意见标志（与资产图互相独立；BP 驳回 = 回滚虚拟槽，无重修轮次）
_AWAIT_BP_FEEDBACK = False


def _load_cfg() -> dict:
    """与 orchestrator/chat.py 同源的装配逻辑（toml → LlmConfig/endpoint/work_dir）。"""
    cfg = toml.load(str(_SIM_ROOT / "config" / "sim_harness.toml"))
    repo = _SIM_ROOT.parent
    work = Path(cfg.get("paths", {}).get("work_dir", "sim_harness/_work"))
    if not work.is_absolute():
        work = (repo / work).resolve()
    llm_t = cfg.get("llm", {})
    ue_t = cfg.get("unreal", {})
    # [nl] skills_dir：相对 lab_atuo_inteller/ 的 skill 注入目录（缺省 skills/）
    sd = cfg.get("nl", {}).get("skills_dir")
    skills_dir = (_SIM_ROOT / sd).resolve() if sd else None
    return {
        "llm_cfg": LlmConfig(
            base_url=llm_t.get("base_url", "http://localhost:11434/v1"),
            model=llm_t.get("model", "qwen2.5:7b"),
            api_key=llm_t.get("api_key", "ollama"),
            vision=bool(llm_t.get("vision", False)),  # VL 总闸（多模态模型才开）
        ),
        "ue_endpoint": ue_t.get("endpoint", "http://127.0.0.1:3000/mcp"),
        "ue_token_file": ue_t.get("token_file"),  # EngineWire capability-token（per-machine）
        "work_dir": work,
        "skills_dir": skills_dir,
        # [blender]：stdio MCP 启动命令（空 = 未配置，blender_* 工具不装配）
        "blender_command": shlex.split(cfg.get("blender", {}).get("command", "") or "") or None,
        # [asset]：人审批准后草稿合入目录（空 = 批准也不复制，草稿留 asset_run 人工处理）
        "asset_merge_dir": cfg.get("asset", {}).get("merge_dir", ""),
    }


async def _send_review_card(payload: dict) -> None:
    """人审卡片：证据摘要 + 三态按钮（批准/驳回带意见/终止；文字回复同样有效，见 _main 硬路由）。"""
    revisions, max_rev = payload.get("revisions", 0), payload.get("max_revisions", 3)
    lines = [f"⏸ **流水线挂起待人审**（设备 {payload.get('device_name')}）"]
    lines.append(f"- 关节 {payload.get('joint_count')} 个；机器闸 "
                 f"{'全过 ✅' if payload.get('gate_ok') else '未过 ❌'}"
                 f"；自纠 {payload.get('retries')} 次；已驳回 {revisions}/{max_rev} 轮")
    for ln in (payload.get("score_text") or "").splitlines()[:4]:
        lines.append("  " + ln)
    for n in payload.get("review_notes", []):
        lines.append(f"  ! {n}")
    lines.append(f"- 草稿：`{payload.get('draft_toml')}`"
                 + (f"、`{payload.get('draft_lua')}`" if payload.get("draft_lua") else ""))
    lines.append(f"- 证据包：`{payload.get('evidence_path')}`")
    revise_left = revisions < max_rev
    if not revise_left:
        lines.append("⚠️ 驳回重修轮次已用完，只剩 批准 / 终止")
    lines.append("审批：点按钮，或回复 `批准` / `驳回:你的意见` / `终止`")
    actions = [cl.Action(name="asset_approve", payload={}, label="✅ 批准合入")]
    if revise_left:
        actions.append(cl.Action(name="asset_revise", payload={}, label="✏️ 驳回重修"))
    actions.append(cl.Action(name="asset_terminate", payload={}, label="⛔ 终止"))
    await cl.Message(content="\n".join(lines), actions=actions).send()


async def _after_resolve(result_text: str) -> None:
    """审批续跑结果回显；若驳回重修后再次挂起，补发新审批卡片。"""
    await cl.Message(content=result_text).send()
    payload = pending_review()
    if payload is not None:
        await _send_review_card(payload)


@cl.action_callback("asset_approve")
async def _on_approve(_action: cl.Action) -> None:
    global _AWAIT_FEEDBACK
    _AWAIT_FEEDBACK = False
    await _after_resolve(resolve_pending(approved=True))


@cl.action_callback("asset_revise")
async def _on_revise(_action: cl.Action) -> None:
    global _AWAIT_FEEDBACK
    _AWAIT_FEEDBACK = True
    await cl.Message(content="✏️ 请直接回复**驳回意见**（将作为最高优先级证据交给重修）。").send()


@cl.action_callback("asset_terminate")
async def _on_terminate(_action: cl.Action) -> None:
    global _AWAIT_FEEDBACK
    _AWAIT_FEEDBACK = False
    await _after_resolve(resolve_pending(approved=False))


async def _send_bp_review_card(payload: dict) -> None:
    """BP 装配人审卡片：三态（批准=保留并请保存 BP / 驳回带意见=回滚虚拟槽 / 终止=保留现状）。"""
    lines = [f"⏸ **BP 装配挂起待人审**（{payload.get('device_label')} → "
             f"{payload.get('actor_name')}）"]
    lines.append(f"- 槽位 {payload.get('slot_count')}；创建={payload.get('brush_created')} "
                 f"跳过={payload.get('brush_skipped')} "
                 f"属性同步={'是' if payload.get('props_synced') else '否'}；"
                 f"机器闸 {'全过 ✅' if payload.get('gate_ok') else '未过 ❌'}")
    for i in payload.get("gate_issues", [])[:6]:
        lines.append(f"  ✗ [{i['gate']}] {i['message']}")
    for n in payload.get("review_notes", [])[:8]:
        lines.append(f"  ! {n}")
    lines.append(f"- 证据包：`{payload.get('evidence_path')}`")
    lines.append("审批：点按钮，或回复 `批准` / `驳回:你的意见`（回滚虚拟槽）/ `终止`（保留现状）")
    actions = [cl.Action(name="bp_approve", payload={}, label="✅ 批准（保留装配）"),
               cl.Action(name="bp_revise", payload={}, label="↩ 驳回（回滚虚拟槽）"),
               cl.Action(name="bp_terminate", payload={}, label="⛔ 终止（保留现状）")]
    await cl.Message(content="\n".join(lines), actions=actions).send()


@cl.action_callback("bp_approve")
async def _on_bp_approve(_action: cl.Action) -> None:
    global _AWAIT_BP_FEEDBACK
    _AWAIT_BP_FEEDBACK = False
    await cl.Message(content=resolve_bp_pending(approved=True)).send()


@cl.action_callback("bp_revise")
async def _on_bp_revise(_action: cl.Action) -> None:
    global _AWAIT_BP_FEEDBACK
    _AWAIT_BP_FEEDBACK = True
    await cl.Message(content="↩ 请直接回复**驳回意见**（将回滚虚拟槽，意见进证据）。").send()


@cl.action_callback("bp_terminate")
async def _on_bp_terminate(_action: cl.Action) -> None:
    global _AWAIT_BP_FEEDBACK
    _AWAIT_BP_FEEDBACK = False
    await cl.Message(content=resolve_bp_pending(approved=False)).send()


@cl.on_chat_start
async def _start() -> None:
    await cl.Message(
        content=(
            "**sim_harness** 自然语言 → 自定义图优先，未命中再 ReAct\n"
            "资产编排示例会直接进资产流水线；其余指令走 plan ⇄ execute。\n\n"
            "UE 示例：`在UE中新增1个立方体网格` / `查一下场景里有哪些Actor`\n"
            "资产编排示例：上传设备资料图 + `接入新设备 CAP8A，场景在 _work/cap_scene.json`\n"
            "BP 装配示例（需 UE 编辑器已开）：`把 BP_121_Capping_8a2 按 HTE 档案装配槽位，设备 121`\n"
            "流水线跑到人审闸门会挂起出审批卡片（三态）：点按钮或回复 `批准` / `驳回:意见` / `终止`"
        )
    ).send()


@cl.on_message
async def _main(message: cl.Message) -> None:
    text = (message.content or "").strip()

    # ── 人审硬路由：有待审/等意见时，审批词直接 resume，不进 ReAct 环（意图不给模型判）──
    global _AWAIT_FEEDBACK
    payload = pending_review()
    if payload is not None or _AWAIT_FEEDBACK:
        if _APPROVE_RE.match(text):
            _AWAIT_FEEDBACK = False
            await _after_resolve(resolve_pending(approved=True))
            return
        m = _REVISE_RE.match(text)
        if m and payload is not None:
            feedback = m.group(1).strip()
            if not feedback:
                _AWAIT_FEEDBACK = True
                await cl.Message(content="✏️ 驳回需要意见：请回复 `驳回:你的意见`，"
                                         "或直接输入意见内容。").send()
                return
            if payload.get("revisions", 0) >= payload.get("max_revisions", 3):
                await cl.Message(content="⚠️ 驳回重修轮次已用完，请回复 `批准` 或 `终止`。").send()
                return
            _AWAIT_FEEDBACK = False
            await _after_resolve(resolve_pending(approved=False, feedback=feedback))
            return
        if _TERMINATE_RE.match(text):
            _AWAIT_FEEDBACK = False
            await _after_resolve(resolve_pending(approved=False))
            return
        if _AWAIT_FEEDBACK and payload is not None:
            # 等意见态：本条文本即驳回意见（任何非审批词内容）
            _AWAIT_FEEDBACK = False
            await _after_resolve(resolve_pending(approved=False, feedback=text))
            return
        await cl.Message(
            content=(f"⏸ 有流水线待审（设备 {payload.get('device_name') if payload else '?'}）。"
                     "请先审批：`批准` / `驳回:意见` / `终止`，再聊别的。")
        ).send()
        return

    # ── BP 装配人审硬路由：三态同上；驳回 = 回滚虚拟槽（无重修轮次）──
    global _AWAIT_BP_FEEDBACK
    bp_payload = pending_bp_review()
    if bp_payload is not None or _AWAIT_BP_FEEDBACK:
        if _APPROVE_RE.match(text):
            _AWAIT_BP_FEEDBACK = False
            await cl.Message(content=resolve_bp_pending(approved=True)).send()
            return
        m = _REVISE_RE.match(text)
        if m and bp_payload is not None:
            feedback = m.group(1).strip()
            if not feedback:
                _AWAIT_BP_FEEDBACK = True
                await cl.Message(content="↩ 驳回需要意见：请回复 `驳回:你的意见`，"
                                         "或直接输入意见内容。").send()
                return
            _AWAIT_BP_FEEDBACK = False
            await cl.Message(content=resolve_bp_pending(approved=False, feedback=feedback)).send()
            return
        if _TERMINATE_RE.match(text):
            _AWAIT_BP_FEEDBACK = False
            await cl.Message(content=resolve_bp_pending(approved=False)).send()
            return
        if _AWAIT_BP_FEEDBACK and bp_payload is not None:
            # 等意见态：本条文本即驳回意见（任何非审批词内容）
            _AWAIT_BP_FEEDBACK = False
            await cl.Message(content=resolve_bp_pending(approved=False, feedback=text)).send()
            return
        await cl.Message(
            content=(f"⏸ 有 BP 装配待审（{bp_payload.get('actor_name') if bp_payload else '?'}）。"
                     "请先审批：`批准` / `驳回:意见` / `终止`，再聊别的。")
        ).send()
        return

    cfg = _load_cfg()
    reply = cl.Message(content="🔄 规划与执行中…")
    await reply.send()

    # 上传图片附件 → data URL（VL 首轮即见；纯文本模型由 vision 闸丢弃）
    user_images: list[str] = []
    for el in message.elements or []:
        mime = getattr(el, "mime", "") or ""
        path = getattr(el, "path", None)
        if mime.startswith("image/") and path:
            try:
                raw = Path(path).read_bytes()
            except OSError:
                continue
            user_images.append(f"data:{mime};base64,{base64.b64encode(raw).decode()}")

    final = await cl.make_async(dispatch)(
        message.content,
        llm_cfg=cfg["llm_cfg"],
        ue_endpoint=cfg["ue_endpoint"],
        ue_token_file=cfg["ue_token_file"],
        work_dir=cfg["work_dir"],
        skills_dir=cfg["skills_dir"],
        user_images=user_images,
        blender_command=cfg["blender_command"],
        asset_interactive=True,                    # GUI 长驻进程：挂起态可跨消息 resume
        asset_merge_dir=cfg["asset_merge_dir"],
    )

    elements = []
    shot = final.get("screenshot_path")
    if shot and Path(shot).exists():
        elements.append(cl.Image(path=shot, name="UE 证据截图", display="inline"))
    reply.content = final.get("report", "(无报告)")
    reply.elements = elements
    await reply.update()

    # 本轮若触发了流水线并挂起在人审 → 补发审批卡片（按钮 + 文字双通道）
    payload = pending_review()
    if payload is not None:
        await _send_review_card(payload)
    bp_payload = pending_bp_review()
    if bp_payload is not None:
        await _send_bp_review_card(bp_payload)
