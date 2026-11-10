"""资产图 MCP（stdio）。dsh 只看见整图启动和人审续跑，看不见单步 asset_*。

进程必须常驻：人审挂起态在本进程的 ``_PENDING`` 里，下一次工具调用才能续跑。
语义单发仍读 ``config/sim_harness.toml [llm]``（本地 vLLM），不把场景和资料图交给 dsh 的云端模型。
"""

from __future__ import annotations

import base64
import logging
import shlex
import sys
from pathlib import Path

import toml
from mcp.server.fastmcp import FastMCP

_SIM_ROOT = Path(__file__).resolve().parents[3]
if str(_SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SIM_ROOT))

from llm.provider import LlmConfig  # noqa: E402
from orchestrator.graph.custom.asset import pending_review, resolve_pending, run  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("lab_atuo_inteller.asset_mcp")

mcp = FastMCP(
    "AssetGraph",
    instructions=(
        "整机运动学资产编排只有两个工具：run_asset 启动整张流水线，"
        "resolve_asset_review 在挂起后批准、驳回或终止。"
        "不要用别的资产工具逐件编排。缺设备名或场景来源时，按返回原文问用户并结束。"
        "返回「挂起待批」后，下一条审批必须调用 resolve_asset_review，不要再次 run_asset。"
    ),
)

_APPROVE = {"approve", "approved", "yes", "y", "批准", "同意", "通过", "合入"}
_REVISE = {"revise", "驳回", "重修", "打回"}
_TERMINATE = {"terminate", "reject", "abort", "stop", "no", "n", "终止", "拒绝", "不批", "放弃", "作废"}
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _cfg() -> dict:
    """读 sim_harness.toml。相对路径相对仓库根（lab_atuo_inteller 的上一级）。"""
    cfg = toml.load(str(_SIM_ROOT / "config" / "sim_harness.toml"))
    repo = _SIM_ROOT.parent
    work = Path(cfg.get("paths", {}).get("work_dir", "lab_atuo_inteller/_work"))
    if not work.is_absolute():
        work = (repo / work).resolve()
    llm_t = cfg.get("llm", {})
    return {
        "llm_cfg": LlmConfig(
            base_url=llm_t.get("base_url", "http://localhost:11434/v1"),
            model=llm_t.get("model", "qwen2.5:7b"),
            api_key=llm_t.get("api_key", "ollama"),
            vision=bool(llm_t.get("vision", False)),
        ),
        "work_dir": work,
        "blender_command": shlex.split(cfg.get("blender", {}).get("command", "") or "") or None,
        "merge_dir": cfg.get("asset", {}).get("merge_dir", "") or "",
    }


def _images(paths: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in paths or []:
        p = Path(raw)
        if not p.is_file():
            logger.warning("资料图不存在，跳过: %s", raw)
            continue
        mime = _MIME.get(p.suffix.lower(), "image/png")
        out.append(f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}")
    return out


def _instruction(instruction: str, device_name: str, scene_path: str, live_blender: bool) -> str:
    parts = [instruction.strip()]
    if device_name.strip():
        parts.append(f"接入新设备 {device_name.strip()}")
    if scene_path.strip():
        parts.append(f"场景在 {scene_path.strip()}")
    if live_blender:
        parts.append("Blender 正开着")
    return "\n".join(p for p in parts if p)


@mcp.tool()
def run_asset(
    instruction: str,
    device_name: str = "",
    scene_path: str = "",
    live_blender: bool = False,
    image_paths: list[str] | None = None,
) -> str:
    """启动整张资产流水线（读场景→语义→落地→机器闸→评分→人审挂起）。

    用户要给一台设备建 TOML/Lua 时只调这一个。instruction 用用户原话。
    已知设备名、场景 JSON 路径时填 device_name / scene_path；Blender 已打开且没有 JSON 时 live_blender=true。
    image_paths 是本机资料图路径，只进流水线内的本地模型。
    已有待审时不要调用。返回挂起摘要后，等待用户审批再调 resolve_asset_review。
    """
    if pending_review() is not None:
        return ("已有流水线待审。请调用 resolve_asset_review（批准 / 驳回 / 终止），"
                "不要再次 run_asset。")
    cfg = _cfg()
    text = _instruction(instruction, device_name, scene_path, live_blender)
    logger.info("run_asset: %s", text.replace("\n", " ")[:200])
    out = run(
        text,
        images=_images(image_paths),
        llm_cfg=cfg["llm_cfg"],
        work_dir=cfg["work_dir"],
        blender_command=cfg["blender_command"],
        interactive=True,
        merge_dir=cfg["merge_dir"],
    )
    return out.get("report") or "（资产图无输出）"


@mcp.tool()
def resolve_asset_review(decision: str, feedback: str = "") -> str:
    """续跑挂起的资产流水线。decision 只能是 批准、驳回、终止。

    驳回时 feedback 必填，意见会进入重修。没有待审时如实说明，不要改调 run_asset。
    """
    word = (decision or "").strip().lower()
    if word in _APPROVE or decision.strip() in _APPROVE:
        if pending_review() is None:
            return "（当前没有待审的流水线）"
        return resolve_pending(approved=True)
    if word in _REVISE or decision.strip() in _REVISE:
        if pending_review() is None:
            return "（当前没有待审的流水线）"
        note = feedback.strip()
        if not note:
            return "驳回需要意见。请把意见放进 feedback 再调一次，不要空驳回。"
        return resolve_pending(approved=False, feedback=note)
    if word in _TERMINATE or decision.strip() in _TERMINATE:
        if pending_review() is None:
            return "（当前没有待审的流水线）"
        return resolve_pending(approved=False)
    return "decision 只能是 批准、驳回、终止。"


def main() -> None:
    logger.info("资产图 MCP（stdio）启动")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
