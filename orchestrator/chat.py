"""sim_harness 自然语言入口（Phase NL-1 CLI）。

用法（仓库根或设 PYTHONPATH=sim_harness）：
    python -m orchestrator.chat "在UE中新增1个立方体网格"
    python -m orchestrator.chat "把 MCP_Cube_1 移到 z=200" --ue-endpoint http://127.0.0.1:8000/mcp

链路：自然语言 → LangGraph(plan[LLM] → execute[UE MCP] → evidence[截图] → report)。
模型连接读 ``config/sim_harness.toml [llm]``（Ollama/vLLM 只改配置）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import toml

from llm.provider import LlmConfig
from orchestrator.nl_graph import run_nl_command


def main() -> int:
    ap = argparse.ArgumentParser(description="sim_harness 自然语言 → UE 场景落地")
    ap.add_argument("instruction", help="自然语言指令，如：在UE中新增1个立方体网格")
    ap.add_argument("--config", default="config/sim_harness.toml", help="相对 sim_harness/")
    ap.add_argument("--ue-endpoint", default=None, help="覆盖 UE MCP 端点")
    args = ap.parse_args()

    sim_root = Path(__file__).resolve().parents[1]
    repo_root = sim_root.parent
    cfg_toml = toml.load(str((sim_root / args.config).resolve()))
    llm_t = cfg_toml.get("llm", {})
    ue_t = cfg_toml.get("unreal", {})

    def _rel(p: str) -> Path:
        """toml 里的相对路径一律相对仓库根解析（禁硬编码绝对路径，规则 001）。"""
        q = Path(p)
        return q if q.is_absolute() else (repo_root / q).resolve()

    llm_cfg = LlmConfig(
        base_url=llm_t.get("base_url", "http://localhost:11434/v1"),
        model=llm_t.get("model", "qwen2.5:7b"),
        api_key=llm_t.get("api_key", "ollama"),
        vision=bool(llm_t.get("vision", False)),  # VL 总闸（多模态模型才开）
    )
    endpoint = args.ue_endpoint or ue_t.get("endpoint", "http://127.0.0.1:3000/mcp")
    token_file = ue_t.get("token_file")  # EngineWire capability-token（per-machine，可空=无鉴权）
    work_dir = _rel(cfg_toml.get("paths", {}).get("work_dir", "sim_harness/_work"))

    final = run_nl_command(args.instruction, llm_cfg=llm_cfg, ue_endpoint=endpoint,
                           ue_token_file=token_file, work_dir=work_dir)
    print(final.get("report", "(无报告)"))
    return 0 if not final.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
