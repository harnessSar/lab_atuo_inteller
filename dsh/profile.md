# dsh profile — sim_harness

> 状态：**参考对照档案（已降级，2026-10-30 晚拍板）**。自有 Agent（DESIGN.md §11 H）立起后，dsh 不再是主线/依赖；本文件仅留接线事实作设计参考。远期若 dsh 作宿主，只经 H7 的 MCP 封装做任务级委托。

## Profile 选择

| 场景 | profile | 用途 |
|---|---|---|
| 开发联调 | `sdk` | orchestrator 经 JSON-RPC stdio 调 dsh 的 LLM+工具执行 |
| CI / 无人值守 | `headless "接入新设备 <key>"` | 跑一次编排并打印结论退出 |
| 现场对话操作（**主线，2026-10-30 用户拍板**） | **`desktop`（Electron 客户端）** | 人用自然语言驱动编排；长任务 UI 稳定、原生文件交互。管理走桌面内置命令运行时（`dsh-desktop-host` cli，复用同一 `runCli`），配置直写 profile 的 `cordis.patch.yml`（HMR 热重载） |
| 现场对话操作（备选） | `web` | 浏览器对话框；受沙盒限（标签页休眠断 UI、文件选择器交互），不投入 |

## MCP servers（dsh 自动发现注册）

```yaml
mcpServers:
  kinPadAssetMCP:
    command: <repo>/kin_pad/.venv/Scripts/python.exe   # 主 venv（mcp extra 已验证）
    args: ["-m", "kin_pad.mcp.asset.server"]
    env: { PYTHONPATH: "<repo>/kin_pad/src" }
# 原 kinPadUnrealMCP（mcp/unreal 转发壳）已删除：出-UE 由 orchestrator/ue_mcp_client.py
# 直连 lab_simmer 官方 MCP（Streamable HTTP 8000），不经自研 MCP server。见规则 110。
```

## 工具策略（pre-execute 三态）

| 工具类 | 策略 | 例子 |
|---|---|---|
| 只读 | **放行** | parse_hte_device / validate_assets / diff_three_end_constants / run_golden_tests |
| 写盘/UE 变更 | **ask（人审）** | ue_mcp_client 的 load_scene / clear_scene / brush_slots（orchestrator 内经官方 MCP call_tool 调用，策略在编排层实施） |

## guard（defineTool + ctx.tools.guard，红线机器校验）

- 单位：角度 rad、长度 mm（算法/TOML/Lua）；UE 工站/机位 cm（仅 ParseMatrixLocal 换一次）
- TOML 数字 ASCII `-`；`pulses_per_unit ≠ 0`；生产关节禁 `*-demo`
- 路径：禁硬编码绝对路径，从 `config/sim_harness.toml` 注入

> 注：红线在 Python `mcp/asset/spec.py` 已机器强制；dsh guard 是第二道（TS 侧），二者一致。
