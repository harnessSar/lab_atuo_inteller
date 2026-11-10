"""对话框唯一入口：先认领自定义图，未命中再进 ReAct。

人审挂起优先于 match：有待审流水线时本函数不新开图。批准 / 驳回 / 终止仍由
GUI 在调用本函数之前用硬路由 ``resolve_pending``（意图不给模型判）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from orchestrator.graph.custom import CUSTOM_GRAPHS
from orchestrator.graph.custom.asset import pending_review
from orchestrator.graph.custom.bp_assembly import pending_bp_review
from orchestrator.graph.react import run_nl_command

logger = logging.getLogger("lab_atuo_inteller.orchestrator.route")


def dispatch(
    user_input: str,
    *,
    llm_cfg=None,
    ue_endpoint: str = "http://127.0.0.1:3000/mcp",
    ue_token_file: str | Path | None = None,
    work_dir: Path | None = None,
    skills_dir: str | Path | None = None,
    max_steps: int = 18,
    user_images: list[str] | None = None,
    enable_asset_tools: bool = True,
    blender_command: list[str] | None = None,
    asset_interactive: bool = False,
    asset_merge_dir: str = "",
) -> dict:
    """一条对话的分流结果。键与 ReAct 终态对齐，另加 ``route``。

    ``route`` = 自定义图名（当前 ``asset``）| ``react`` | ``review``（有待审，未新开图）。
    """
    pending = pending_review()
    if pending is not None:
        name = pending.get("device_name", "?")
        logger.info("有待审流水线（设备 %s），不重新分流", name)
        return {
            "route": "review",
            "report": (f"⏸ 有流水线待审（设备 {name}）。"
                       "请先审批：`批准` / `驳回:意见` / `终止`，再聊别的。"),
            "error": "",
            "screenshot_path": "",
        }
    bp_pending = pending_bp_review()
    if bp_pending is not None:
        actor = bp_pending.get("actor_name", "?")
        logger.info("有 BP 装配待审（%s），不重新分流", actor)
        return {
            "route": "review",
            "report": (f"⏸ 有 BP 装配待审（{actor}）。"
                       "请先审批：`批准` / `驳回:意见` / `终止`，再聊别的。"),
            "error": "",
            "screenshot_path": "",
        }

    text = user_input or ""
    for graph in CUSTOM_GRAPHS:
        if not graph.match(text, user_images):
            continue
        logger.info("对话框命中自定义图 %s", graph.name)
        out = graph.run(
            text,
            images=user_images,
            llm_cfg=llm_cfg,
            work_dir=work_dir or (Path(__file__).resolve().parents[1] / "_work"),
            blender_command=blender_command,
            interactive=asset_interactive,
            merge_dir=asset_merge_dir,
        )
        out["route"] = graph.name
        return out

    logger.info("自定义图未命中，进入 ReAct")
    final = dict(run_nl_command(
        text,
        llm_cfg=llm_cfg,
        ue_endpoint=ue_endpoint,
        ue_token_file=ue_token_file,
        work_dir=work_dir,
        skills_dir=skills_dir,
        max_steps=max_steps,
        user_images=user_images,
        enable_asset_tools=enable_asset_tools,
        blender_command=blender_command,
    ))
    final["route"] = "react"
    return final
