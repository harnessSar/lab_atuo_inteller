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
    ap.add_argument("--images", nargs="*", default=None,
                    help="附图路径（VL 模型首轮即见，如设备资料图）；纯文本模型自动丢弃")
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
    # [nl] skills_dir：相对 sim_root 的 skill 注入目录（缺省 = skills/ 全量）
    sd = cfg_toml.get("nl", {}).get("skills_dir")
    skills_dir = (sim_root / sd).resolve() if sd else None
    # --images → data URL（vision 闸在 run_nl_command：纯文本模型丢弃）
    user_images = None
    if args.images:
        import base64
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
        user_images = [
            f"data:{mime.get(Path(p).suffix.lower(), 'image/png')};base64,"
            + base64.b64encode(Path(p).read_bytes()).decode()
            for p in args.images]

    final = run_nl_command(args.instruction, llm_cfg=llm_cfg, ue_endpoint=endpoint,
                           ue_token_file=token_file, work_dir=work_dir,
                           skills_dir=skills_dir, user_images=user_images)
    print(final.get("report", "(无报告)"))
    return 0 if not final.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
