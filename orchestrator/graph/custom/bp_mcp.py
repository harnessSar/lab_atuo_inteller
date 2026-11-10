"""BP 装配图 MCP（stdio）。dsh 只看见整图启动和人审续跑，看不见单步 bp_*。

进程必须常驻：人审挂起态在本进程的 ``_PENDING_BP`` 里，下一次工具调用才能续跑。
UE 通道读 ``config/sim_harness.toml [unreal]``（EngineWire）；VL 顾问（可选）读
``[llm]``（本地 vLLM），截图/资料图不交给 dsh 的云端模型。
"""

from __future__ import annotations

import base64
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_SIM_ROOT = Path(__file__).resolve().parents[3]
if str(_SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SIM_ROOT))

from llm.provider import LlmProvider  # noqa: E402
from orchestrator.graph.custom.bp_assembly import (  # noqa: E402
    load_bp_config, pending_bp_review, resolve_bp_pending, start_interactive_assembly)
from orchestrator.tool import bp_tools as bpt  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("lab_atuo_inteller.bp_mcp")

mcp = FastMCP(
    "BpAssembly",
    instructions=(
        "BP 装配（按 HTE 档案给 UE 蓝图刷属性与虚拟槽位）只有两个工具："
        "run_bp_assembly 启动整条装配流水线，resolve_bp_review 在挂起后批准、驳回或终止。"
        "不要用别的 UE 工具逐件拼槽位（唯一通道是流水线内的 Hte.BrushSlots）。"
        "缺 actor label 或设备 key 时，按返回原文问用户并结束。"
        "返回「挂起待批」后，下一条审批必须调用 resolve_bp_review，不要再次 run_bp_assembly。"
    ),
)

_APPROVE = {"approve", "approved", "yes", "y", "批准", "同意", "通过", "保留"}
_REVISE = {"revise", "驳回", "打回", "回滚"}
_TERMINATE = {"terminate", "reject", "abort", "stop", "no", "n", "终止", "拒绝", "不批", "放弃", "作废"}
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _images(paths: list[str] | None) -> list[str]:
    """本机图片路径 → data URL（只进流水线内的本地 VL 顾问，不出内网）。"""
    out: list[str] = []
    for raw in paths or []:
        p = Path(raw)
        if not p.is_file():
            logger.warning("图片不存在，跳过: %s", raw)
            continue
        mime = _MIME.get(p.suffix.lower(), "image/png")
        out.append(f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}")
    return out


@mcp.tool()
def run_bp_assembly(
    instruction: str,
    actor_name: str = "",
    device_key: str = "",
    hte_json_path: str = "",
    toml_path: str = "",
    image_paths: list[str] | None = None,
) -> str:
    """启动整条 BP 装配流水线（解析 HTE 档案→前置检查→刷槽位→语义提案→拓扑/VL 顾问
    →机器闸复核→人审挂起）。

    用户要给场景中的设备蓝图刷 HTE 槽位/属性时只调这一个。instruction 用用户原话。
    actor_name 是场景中设备 actor 的 label（如 BP_121_Capping_8a2），device_key 是
    HTE 档案设备 key（如 121）；二者缺一时如实向用户要并结束。
    hte_json_path 一般留空（走配置+磁盘兜底）；toml_path 可选（运动学 TOML，做
    mesh↔关节对齐检查）；image_paths 是本机资料图路径，只进本地 VL 顾问。
    已有待审时不要调用。返回挂起摘要后，等待用户审批再调 resolve_bp_review。
    """
    if pending_bp_review() is not None:
        return ("已有 BP 装配流水线待审。请调用 resolve_bp_review（批准 / 驳回 / 终止），"
                "不要再次 run_bp_assembly。")
    if not actor_name.strip() or not device_key.strip():
        return ("缺参数：请向用户要场景 actor 的 label（如 BP_121_Capping_8a2）和 "
                "HTE 设备 key（如 121），拿到后重试；不要改去别的工具硬试。")
    cfgd = load_bp_config()
    hte_path, src_note, perr = bpt.resolve_hte_json(hte_json_path, cfgd["hte_json"],
                                                    Path(__file__).resolve().parents[4])
    if perr:
        return f"❌ {perr}"
    images = _images(image_paths)
    llm = LlmProvider(cfgd["llm_cfg"]) if (images and cfgd["llm_cfg"].vision) else None
    logger.info("run_bp_assembly: actor=%s key=%s hte=%s（%s）",
                actor_name, device_key, hte_path, src_note or "显式路径")
    report = start_interactive_assembly(
        actor_name=actor_name.strip(), device_key=device_key.strip(),
        hte_json_path=str(hte_path),
        ue_endpoint=cfgd["ue_endpoint"], ue_token_file=cfgd["ue_token_file"],
        work_dir=cfgd["work_dir"], toml_path=toml_path.strip(),
        images=images, llm=llm)
    return ((src_note + "\n") if src_note else "") + report


@mcp.tool()
def resolve_bp_review(decision: str, feedback: str = "") -> str:
    """续跑挂起的 BP 装配流水线。decision 只能是 批准、驳回、终止。

    批准 = 保留装配结果（提示用户在 UE 保存蓝图资产）；驳回 = 回滚虚拟槽，
    feedback 必填，意见进证据；终止 = 保留现状。没有待审时如实说明，不要改调 run_bp_assembly。
    """
    word = (decision or "").strip().lower()
    if word in _APPROVE or decision.strip() in _APPROVE:
        if pending_bp_review() is None:
            return "（当前没有待审的 BP 装配流水线）"
        return resolve_bp_pending(approved=True)
    if word in _REVISE or decision.strip() in _REVISE:
        if pending_bp_review() is None:
            return "（当前没有待审的 BP 装配流水线）"
        note = feedback.strip()
        if not note:
            return "驳回需要意见。请把意见放进 feedback 再调一次，不要空驳回。"
        return resolve_bp_pending(approved=False, feedback=note)
    if word in _TERMINATE or decision.strip() in _TERMINATE:
        if pending_bp_review() is None:
            return "（当前没有待审的 BP 装配流水线）"
        return resolve_bp_pending(approved=False)
    return "decision 只能是 批准、驳回、终止。"


def main() -> None:
    logger.info("BP 装配图 MCP（stdio）启动")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
