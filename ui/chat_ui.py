"""sim_harness 自然语言 GUI 入口（Chainlit 薄面板，DESIGN.md §11 H6 提前最小版）。

启动（或双击 ui/启动GUI.cmd）：
    D:\\project\\kin_pad\\.venv\\Scripts\\python.exe -m chainlit run ui\\chat_ui.py --port 8501
浏览器打开 http://localhost:8501 （端口勿用 8000：UE MCP 占用）。

链路同 CLI（orchestrator/chat.py）：NL → LangGraph(plan→execute→evidence→report) → UE。
模型连接读 config/sim_harness.toml [llm]（Ollama/vLLM 只改配置）；UE 端点读 [unreal]。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import chainlit as cl
import toml

_SIM_ROOT = Path(__file__).resolve().parents[1]
if str(_SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SIM_ROOT))

from llm.provider import LlmConfig  # noqa: E402
from orchestrator.nl_graph import run_nl_command  # noqa: E402


def _load_cfg() -> dict:
    """与 orchestrator/chat.py 同源的装配逻辑（toml → LlmConfig/endpoint/work_dir）。"""
    cfg = toml.load(str(_SIM_ROOT / "config" / "sim_harness.toml"))
    repo = _SIM_ROOT.parent
    work = Path(cfg.get("paths", {}).get("work_dir", "sim_harness/_work"))
    if not work.is_absolute():
        work = (repo / work).resolve()
    llm_t = cfg.get("llm", {})
    ue_t = cfg.get("unreal", {})
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
    }


@cl.on_chat_start
async def _start() -> None:
    await cl.Message(
        content=(
            "**sim_harness** 自然语言 → UE 落地\n"
            "链路：LangGraph plan → execute → evidence → report\n\n"
            "示例：`在UE中新增1个立方体网格` / `查一下场景里有哪些Actor` / `把 MCP_Cube_1 移到 z=200`"
        )
    ).send()


@cl.on_message
async def _main(message: cl.Message) -> None:
    cfg = _load_cfg()
    reply = cl.Message(content="🔄 规划与执行中…")
    await reply.send()

    # 上传图片附件 → data URL（VL 首轮即见；纯文本模型由 run_nl_command vision 闸丢弃）
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

    final = await cl.make_async(run_nl_command)(
        message.content,
        llm_cfg=cfg["llm_cfg"],
        ue_endpoint=cfg["ue_endpoint"],
        ue_token_file=cfg["ue_token_file"],
        work_dir=cfg["work_dir"],
        user_images=user_images,
    )

    elements = []
    shot = final.get("screenshot_path")
    if shot and Path(shot).exists():
        elements.append(cl.Image(path=shot, name="UE 证据截图", display="inline"))
    reply.content = final.get("report", "(无报告)")
    reply.elements = elements
    await reply.update()
