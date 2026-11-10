"""确定性资产编排流水线 CLI（Blender 场景路线；HTE JSON 路线见 run_device.py）。

用法（仓库根或设 PYTHONPATH=lab_atuo_inteller）：
    python -m orchestrator.run_asset --device-key cap8a --device-name CAP8A \
        --scene _work/cap_scene.json --images cap_instruction1.jpg cap_instruction2.jpg \
        --golden-toml kin_pad/save/CAP8a/CAP8A.toml --golden-lua kin_pad/save/CAP8a/CAP8A_passives.lua \
        --merge-dir D:/path/to/business/kin
    python -m orchestrator.run_asset --device-key cap8a --device-name CAP8A --live-blender   # 实况源
    python -m orchestrator.run_asset ... --non-interactive   # 只出证据包，不挂起审批

链路：跑到 human_gate 打印证据包摘要并挂起 → 控制台输入 y/N → 批准且配置
--merge-dir 才复制草稿出 asset_run（业务落盘只此一处）。模型连接读
``config/sim_harness.toml [llm]``，Blender MCP 读 ``[blender]``。
"""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import sys
from pathlib import Path

import toml

from llm.provider import LlmConfig, LlmProvider
from orchestrator.client.mcp_stdio_client import McpStdioClient
from orchestrator.graph.custom.asset import (
    build_asset_pipeline, resume_asset_pipeline)

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _image_data_url(p: Path) -> str:
    return (f"data:{_MIME.get(p.suffix.lower(), 'image/png')};base64,"
            + base64.b64encode(p.read_bytes()).decode())


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性资产编排流水线（Blender → TOML/Lua → 人审合入）")
    ap.add_argument("--device-key", required=True, help="设备 key（ASCII，如 cap8a）")
    ap.add_argument("--device-name", required=True, help="设备名（ASCII，如 CAP8A）")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--scene", default="", help="Blender 场景导出 JSON 路径（文件流）")
    src.add_argument("--live-blender", action="store_true", help="从运行中的 Blender 经 MCP 实况导出")
    ap.add_argument("--images", nargs="*", default=None, help="设备资料图（爆炸图/机构动作表）；纯文本模型自动丢弃")
    ap.add_argument("--golden-toml", default="", help="golden TOML 路径（给了才评分）")
    ap.add_argument("--golden-lua", default="", help="golden Lua 路径")
    ap.add_argument("--script-name", default="", help="被动 Lua 脚本名（缺省 {设备名小写}_passives.lua）")
    ap.add_argument("--merge-dir", default="", help="批准后合入目录；缺省不合入（草稿留 asset_run）")
    ap.add_argument("--non-interactive", action="store_true", help="human_gate 不挂起，只出证据包")
    ap.add_argument("--config", default="config/sim_harness.toml", help="相对 lab_atuo_inteller/")
    args = ap.parse_args()

    sim_root = Path(__file__).resolve().parents[1]
    repo_root = sim_root.parent
    cfg_toml = toml.load(str((sim_root / args.config).resolve()))
    llm_t = cfg_toml.get("llm", {})
    blender_t = cfg_toml.get("blender", {})
    paths_t = cfg_toml.get("paths", {})

    def _rel(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else (repo_root / q).resolve()

    llm = LlmProvider(LlmConfig(
        base_url=llm_t.get("base_url", "http://localhost:11434/v1"),
        model=llm_t.get("model", "qwen2.5:7b"),
        api_key=llm_t.get("api_key", "ollama"),
        vision=bool(llm_t.get("vision", False)),
    ))
    blender_command = shlex.split(blender_t.get("command", "") or "")
    work_dir = _rel(paths_t.get("work_dir", "lab_atuo_inteller/_work"))
    images = [_image_data_url(Path(p)) for p in (args.images or [])]

    client = McpStdioClient(blender_command) if (args.live_blender and blender_command) else None
    graph = build_asset_pipeline(llm, work_dir, blender_client=client)
    init = {
        "device_key": args.device_key, "device_name": args.device_name,
        "work_dir": str(work_dir),
        "scene_path": str(Path(args.scene).resolve()) if args.scene else "",
        "live_blender": bool(args.live_blender), "images": images,
        "golden_toml": args.golden_toml, "golden_lua": args.golden_lua,
        "script_name": args.script_name, "merge_dir": args.merge_dir,
        "interactive": not args.non_interactive, "max_retries": 3,
        "retries": 0, "evidence": [],
    }
    thread_id = f"asset-{args.device_key}"
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    try:
        final = graph.invoke(init, cfg)
        while "__interrupt__" in final:
            payload = final["__interrupt__"][0].value
            print("\n===== 人审（human_gate）=====")
            print(f"证据包: {payload.get('evidence_path')}")
            print(f"关节 {payload.get('joint_count')} 个；机器闸 "
                  f"{'全过' if payload.get('gate_ok') else '未过'}；自纠 {payload.get('retries')} 次"
                  f"；已驳回 {payload.get('revisions', 0)}/{payload.get('max_revisions', 3)} 轮")
            for line in (payload.get("score_text") or "").splitlines()[:4]:
                print(" ", line)
            for n in payload.get("review_notes", []):
                print(f"  ! {n}")
            print(f"草稿: {payload.get('draft_toml')}"
                  + (f"、{payload.get('draft_lua')}" if payload.get("draft_lua") else ""))
            # 三态审批：y 批准 / r 驳回带意见重修 / 其他 终止（草稿留 asset_run）
            answer = input("审批 [y=批准 / r=驳回带意见 / N=终止]: ").strip()
            if answer.lower() == "y":
                approved, feedback = True, ""
            elif answer.lower().startswith("r"):
                approved = False
                feedback = answer[1:].lstrip(" :：") or input("驳回意见: ").strip()
            else:
                approved, feedback = False, ""
            final = resume_asset_pipeline(graph, thread_id, approved, feedback)
    finally:
        if client is not None:
            client.close()

    print("\n" + (final.get("report") or json.dumps(final, ensure_ascii=False, default=str)[:800]))
    return 0 if not final.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
