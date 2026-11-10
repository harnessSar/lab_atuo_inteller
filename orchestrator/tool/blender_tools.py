"""Blender MCP 工具面（编排只读：导出场景物体清单 + 探活；不做 Blender 侧写入）。

对端 = stdio MCP server（FastMCP / blender-mcp 类桥），经 ``McpStdioClient`` 调用。
**契约红线**：导出的场景数据形态与文件流完全一致——
``[{"name": str, "parent": str|None, "matrix_local": [16 平铺 | 4x4 嵌套], ...}]``（单位 m，
相对直接父级）。``blender_export_scene`` 与 ``asset_catalog`` 写**同一份**
``asset_run/scene.json``，下游 asset_land/asset_emit/asset_score 与流水线零改动；
单位换算（m→mm）只在 ``semantic_land`` 落地处做一次（规则 001），本层原样搬运。

server 侧能力名经 ``export_capability`` 参数注入（默认 ``export_scene_objects``，
自写薄 server 的约定名；接 blender-mcp 等第三方 server 时按其工具名配置）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_core.tools import tool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from orchestrator.client.mcp_stdio_client import McpStdioClient
from orchestrator.tool.asset_tools import check_scene_items, ingest_scene_items

logger = logging.getLogger("lab_atuo_inteller.orchestrator.tool.blender_tools")

_FAIL = "执行失败："  # Observation 失败前缀（与 nl_graph/asset_tools 同约定）

# ── blender-mcp（vendor 版）适配 ────────────────────────────────────────────
# 第三方 server 没有我们的 export_scene_objects 直出工具，但有 execute_blender_code
# （在活 Blender 里跑任意 bpy 片段，回执 = stdout）。注片段让它 print 出契约 JSON，
# 标记前缀把 JSON 与 "Code executed successfully:" 等噪声隔开。safe_mode 默认关；
# 即使用户开了，本片段只用 bpy + json + 推导式，仍在白名单内。
_SCENE_MARKER = "__SCENE_JSON__"
_SCENE_EXPORT_SNIPPET = (
    "import bpy, json\n"
    "_items = []\n"
    "for _ob in bpy.context.scene.objects:\n"
    "    _m = _ob.matrix_local\n"
    "    _items.append({'name': _ob.name,\n"
    "                   'parent': (None if _ob.parent is None else _ob.parent.name),\n"
    "                   'matrix_local': [_m[_i][_j] for _i in range(4) for _j in range(4)]})\n"
    "print('" + _SCENE_MARKER + "' + json.dumps(_items, ensure_ascii=False))\n"
)


def export_scene_items(client: McpStdioClient,
                       export_capability: str = "export_scene_objects"
                       ) -> tuple[list[dict] | None, str]:
    """双契约场景导出：对端有直出工具（自写薄 server）就直调；否则走 blender-mcp
    的 execute_blender_code 注片段。返回 ``(items, err)``，err 空 = 成功。
    items 即契约数组 ``[{name, parent, matrix_local(16 平铺, m)}]``。"""
    try:
        names = {t.get("name", "") for t in client.tools_list()}
    except RuntimeError as exc:
        return None, str(exc)
    if export_capability in names:
        resp = client.execute(export_capability, {})
        if not resp.ok:
            return None, resp.error
        items = resp.raw.get("content_json")
        if items is None:
            return None, (f"{export_capability} 回执不是 JSON："
                          f"{(resp.raw.get('content_text') or '')[:200]}")
        return items, ""
    if "execute_blender_code" in names:  # blender-mcp 系：注入导出片段
        resp = client.execute("execute_blender_code", {
            "code": _SCENE_EXPORT_SNIPPET,
            "user_prompt": "导出场景物体清单（name/parent/matrix_local）供运动学资产编排"})
        if not resp.ok:
            return None, resp.error
        text = resp.raw.get("content_text") or ""
        idx = text.rfind(_SCENE_MARKER)
        if idx < 0:
            return None, (f"execute_blender_code 回执无场景标记（Blender 侧执行报错？）："
                          f"{text[:300]}")
        try:
            return json.loads(text[idx + len(_SCENE_MARKER):].strip()), ""
        except json.JSONDecodeError as exc:
            return None, f"场景 JSON 解析失败: {exc}"
    return None, (f"对端既没有 {export_capability} 也没有 execute_blender_code，"
                  f"可用工具：{sorted(names)}")


class _ExportArgs(BaseModel):
    """空参数：导出当前 Blender 场景全部物体。"""


def make_blender_tools(client: McpStdioClient, work_dir: Path,
                       export_capability: str = "export_scene_objects") -> list:
    """工厂：注入 stdio 客户端 + 证据目录，产出 Blender @tool（闭包捕获，图编译后不可变）。

    ``client`` 共享同一实例（一次会话一个子进程）；``export_capability`` 为对端
    场景导出能力的工具名（自写 server 约定 ``export_scene_objects``）。
    """

    def _run_dir() -> Path:
        rd = work_dir / "asset_run"
        rd.mkdir(parents=True, exist_ok=True)
        return rd

    @tool()
    def blender_ping() -> str:
        """Blender MCP 连通性探测：拉起对端 server 并列出其工具名。
        用 blender_export_scene 前可先探活；Blender 未开/未配置时如实报指引。"""
        resp = client.ping()
        if not resp.ok:
            return f"{_FAIL}{resp.error}"
        return f"Blender MCP 在线，对端工具：{resp.raw.get('tools')}"

    @tool(args_schema=_ExportArgs)
    def blender_export_scene() -> str:
        """从正在运行的 Blender 实况导出场景物体清单（name/parent/matrix_local，单位 m），
        写 asset_run/scene.json 并返回紧凑清单（与 asset_catalog 文件流等价二选一）。
        编排运动学资产的第一步：Blender 正开着就用它；只有导出 JSON 文件时用 asset_catalog。"""
        items, fail = export_scene_items(client, export_capability)
        if fail:
            return f"{_FAIL}{fail}"
        err = check_scene_items(items)
        if err:
            return f"{_FAIL}对端返回不是合法场景数组（{err}）"
        logger.info("Blender 实况导出 %d 个物体 → asset_run/scene.json", len(items))
        return ingest_scene_items(items, _run_dir(), "Blender 实况")

    return [blender_ping, blender_export_scene]
