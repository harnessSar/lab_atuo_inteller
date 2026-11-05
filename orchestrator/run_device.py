"""sim_harness 编排入口：给定 HTE 设备 key + 电机表，跑 Phase 1 编排管线。

用法（仓库根，或设 PYTHONPATH=sim_harness）：
    python -m orchestrator.run_device --device-key 5031 \
        --motor-table path/to/motors.csv [--config config/sim_harness.toml] [--ue]

红线：路径从 TOML 配置注入（禁硬编码绝对路径）；UE 步骤默认关（--ue 显式开）；
产物进 work_dir 证据包，落盘业务目录由 human_gate 人审后另行触发。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import toml

from orchestrator.pipeline import DeviceState, PipelineConfig, run_pipeline


def _resolve(repo_root: Path, maybe_rel: str) -> str:
    p = Path(maybe_rel)
    return str(p if p.is_absolute() else (repo_root / p).resolve())


def main() -> int:
    ap = argparse.ArgumentParser(description="sim_harness 资产编排管线（Phase 1）")
    ap.add_argument("--device-key", required=True, help="HTE results 下的设备 key")
    ap.add_argument("--motor-table", required=True, help="电机表路径（.csv/.tsv/.json）")
    ap.add_argument("--config", default="config/sim_harness.toml", help="配置文件（相对 sim_harness/）")
    ap.add_argument("--ue", action="store_true", help="开启 UE 步骤（刷槽位/装载）；默认关")
    ap.add_argument("--no-golden", action="store_true", help="跳过 golden 测试")
    args = ap.parse_args()

    sim_root = Path(__file__).resolve().parents[1]
    repo_root = sim_root.parent
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (sim_root / cfg_path).resolve()
    cfg_toml = toml.load(str(cfg_path))

    paths = cfg_toml.get("paths", {})
    unreal = cfg_toml.get("unreal", {})
    golden_cfg = cfg_toml.get("golden", {})

    cfg = PipelineConfig(
        kin_pad_root=_resolve(repo_root, paths.get("kin_pad_root", "kin_pad")),
        save_dir=_resolve(repo_root, paths.get("save_dir", "kin_pad/save")),
        lambda_spool_cpp=_resolve(repo_root, paths.get("lambda_spool_cpp", "")),
        ue_mcp_endpoint=unreal.get("endpoint", "http://127.0.0.1:8000/mcp"),
        run_ue_steps=bool(args.ue or unreal.get("run_ue_steps", False)),
        run_golden=bool(golden_cfg.get("run_golden", True)) and not args.no_golden,
    )

    state = DeviceState(
        hte_json_path=_resolve(repo_root, paths.get("hte_json", "PID/HTE设备介绍.json")),
        motor_table_path=str(Path(args.motor_table).resolve()),
        device_key=args.device_key,
        work_dir=_resolve(repo_root, paths.get("work_dir", "sim_harness/_work")),
    )

    final = run_pipeline(state, cfg)
    summary = {
        "device_key": final.device_key,
        "ok": not final.errors,
        "retries": final.retries,
        "errors": [e.to_dict() for e in final.errors],
        "evidence_kinds": [e["kind"] for e in final.evidence],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not final.errors else 1


if __name__ == "__main__":
    sys.exit(main())
