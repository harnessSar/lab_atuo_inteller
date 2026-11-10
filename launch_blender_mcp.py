"""mcp-for-blender stdio 启动器（vendor 版，_work/vendor/blender-mcp 装入 venv 后以此拉起）。

存在意义 = 注入本机默认环境（遥测关、更新检查关），并给 sim_harness.toml [blender] command
一个无空格、无引号的稳定路径（shlex 空格分词，免转义地狱）。
对端 Blender 侧仍需 addon 面板点 Connect（socket 9876），本进程只做 MCP↔socket 桥。
"""

import os

os.environ.setdefault("BLENDER_MCP_DISABLE_TELEMETRY", "1")  # 遥测 opt-out（telemetry.py 认此变量）
os.environ.setdefault("BLENDERMCP_NO_UPDATE_CHECK", "1")     # 禁更新检查（update_cli.py 认此变量）

from blender_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
