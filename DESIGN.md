# sim_harness — 资产编排 Agent Harness 设计文档

> 状态：草案 v3（**目标升级为构建自有 Agent**——SKILL / RAG / 流程执行 / HITL 四能力全自有，2026-10-30 晚定；UE 通道走 lab_simmer / UE 5.8 官方 MCP 新路；Phase 1 工具面已落地 `kin_pad/src/kin_pad/mcp/asset/`）
> 适用范围：仿真工程三段中的**资产编排段**——新设备接入自动化。
> 本文档是 sim_harness 的整体设计单一事实源；代码红线仍以 `.cursor/rules` 与磁盘 TOML 为准。

---

## 0. 一句话概览

目标 = **构建自有 Agent**（SKILL + RAG + 流程化任务执行 + 人机交互四能力，模型/知识/工具全自有，不依赖第三方 Agent 宿主）。业务上输入 **HTE 设备 JSON + 电机表**（`.blend`/Datasmith 暂不纳入，能力在建），经 **LangGraph 双模运行时**（自由 loop + 机器闸流水线）编排、**kin_pad** `mcp/asset` 提供纯 Python 机器闸，输出 **UE BP（编排好槽位）+ TOML + Lua**，UE 侧落地目标为 **lab_simmer（UE 5.8）**，出-UE 通道走**官方 MCP（Streamable HTTP）**。确定性步骤全机器闸，LLM 只在生成/修复/诊断点介入，产物过人审合入。**主入口 = 薄 GUI 直连 Agent 运行时**（HTTP+SSE）；MCP 封装仅作跨宿主任务级委托；dsh 降级为参考对照 + 可选宿主。

---

## 1. 定位与边界

| 项 | 内容 |
|---|---|
| 目标 | 新设备接入自动化：设备 JSON（slots/valid_model_id）+ 电机表 → UE BP（槽位已编排）+ TOML + Lua |
| UE 落地端 | **lab_simmer（UE 5.8 新工程）**，已启用 `ModelContextProtocol` + `MCPClientToolset` + `RemoteControl` 插件；**PID 工程转为旧路**，仅维护桌面工具链（GUI/TWIN → TCP 55557），不再作为 Agent 落地目标 |
| Phase 1 实际范围 | 设备 JSON + 电机表 → `DeviceSpec` → TOML/Lua 生成 → 机器闸 → 三端 diff → golden → UE 蓝图化 + 刷槽位 → 验证 |
| **暂不纳入** | `.blend` / Datasmith 导出导入（能力在建）；FK/IK 三端同步（`300` 契约，人主导）；脉冲标定（`xtal-pulse-calibrate`）；打包 |
| Agent 目标 | **自有 Agent**：SKILL / RAG / 流程化任务执行 / 人机交互四能力；不依赖第三方 Agent 宿主（dsh 仅参考对照 + 可选宿主） |
| 主语言 | **Python 全栈**（Agent 运行时 / 编排 / 机器闸 / GUI 面板）；TypeScript 仅在远期自研 dsh 插件时涉及（当前不需要） |
| 模式 | **人机协作**：机器闸优先，LLM 受限介入，human_gate 审批合入 |

## 2. 技术选型（已定）

| 角色          | 选型                                                                                                                            | 理由                                                                                     |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Agent 运行时   | **LangGraph 双模（自有 Python 进程）**                                                                                                | 自由 loop（探索/问答/调试）+ 受控流水线（机器闸生产）共享 state / checkpointer / interrupt；Agent 本体不依赖第三方宿主    |
| 主入口 GUI     | **薄 GUI 直连 Agent**（`langgraph dev`/Studio 起步 → Chainlit 面板）                                                                   | HTTP+SSE 流式、interrupt 原生；**kin_pad 保持现状，不作 Agent 入口**                                  |
| 第三方宿主       | **dsh 降级 = 参考对照 + 可选宿主**                                                                                                      | 三态审批 UX / skill 热加载 / MCP 网关设计可借鉴；接入仅经 MCP 封装做**任务级委托**（双 loop + 云模型中继，不适合做对话入口）       |
| 编排引擎        | **LangGraph**（不用 LangChain 链式抽象）                                                                                              | 资产管线是严格结构化状态机（生成→校验→装载→比对），LLM 不决定流程方向，边是确定性路由                                         |
| 机器闸工具面      | **kin_pad** `mcp/asset`（Python FastMCP）                                                                                       | 复用 `passive_lua_gen`/`Joint`/`JointFileRepository` SSOT；不连 UE/Blender/模型，可独立 `pytest`  |
| UE 通道       | **UE 5.8 官方 MCP**（`ModelContextProtocol` 插件，Streamable HTTP `127.0.0.1:8000/mcp`）+ **自定义 Toolset**（C++ 注册进 `ToolsetRegistry`） | 标准协议，免自研 TCP/命令 dispatcher；Toolset 是官方扩展点，与官方 `AgentSkillToolset` 平级。已实测握手/工具清单（§11 F） |
| 本地模型（开发）    | **Ollama**（5060 笔记本，Windows 原生）                                                                                               | 零许可费、Windows 原生一键、调图/调工具/调审批不挑模型聪明度                                                    |
| 本地模型（联调/生产） | **vLLM**（5090，Linux 原生）                                                                                                       | 零许可费、guided decoding + 稳定 tool calling 是硬需求；5090 Linux 独占 32G 显存，5060 跨机 HTTP 调用       |

**模型方案费用说明**：vLLM / Ollama / llama.cpp 三者**均为开源免费软件**，本地推理只有电费/硬件折旧；只有用**云端托管服务**才按 token 计费。本地推理的最大收益是**数据不出内网**（敏感数据合规）。vLLM **Windows 不友好**（无原生支持，只能 WSL2/Docker），故生产让其独占 5090 的 Linux 原生，彻底绕开。

## 3. 总体架构

```
┌─ 界面层 ─────────────────────────────────────────────────────────────┐
│ 主入口：薄 GUI 直连（langgraph dev/Studio 起步 → Chainlit 面板，ui/）   │
│ 调试：  CLI（orchestrator/chat.py）                                   │
│ 可选宿主：dsh / Cursor ── 经远期 MCP 封装做任务级委托（非对话入口）     │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ HTTP + SSE（流式 / interrupt 原生）
┌─ 运行时层（自有 Agent，Python + LangGraph 双模，agent/）───────────────┐
│  自由 loop 图：模型自主规划工具调用（探索/问答/调试）——默认模式          │
│  流水线图：  机器闸确定性流程（§4）——"接入新设备"类意图触发             │
│  共享：checkpointer（sqlite 会话持久化）/ interrupt（HITL 挂起·续跑）   │
├─ 能力层（工具）───────────────────────────────────────────────────────┤
│  MCP 工具：ue（lab_simmer 官方 toolset）/ kinpad_asset（机器闸 13 具）  │
│  内建工具：use_skill / search_knowledge / 流水线触发器（随批次落地）    │
├─ 知识层 ──────────────────────────────────────────────────────────────┤
│  skills/（SKILL.md + frontmatter；与 .cursor/skills 同格式同语料）     │
│  rag/（本地 embedding + 向量库；语料 = Docs / 规则 / TOML / 代码 chunk）│
├─ 模型层 ──────────────────────────────────────────────────────────────┤
│  开发 Ollama 7B（5060 Win）⇄ 生产 vLLM 32B-AWQ（5090 Linux）          │
│  OpenAI-compat 只改配置；tool calling + guided decoding 是硬门槛       │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ MCP (stdio)        MCP (Streamable HTTP)
              ┌────────────────────┴─────────┐  ┌─────────────┴──────────────┐
              ▼ asset-mcp (Python, 纯计算)    │  ▼ ue_mcp_client.py (已建)    │
   kin_pad/src/kin_pad/mcp/asset/             │  sim_harness/orchestrator/    │
   ├ parse_hte_device / attach_motor_table    │  标准 MCP client，POST JSON   │
   ├ generate_assets (TOML/Lua, 不落盘)       │  → 127.0.0.1:8000/mcp         │
   ├ validate_assets (机器闸)                 │  ├ list_toolsets / describe   │
   ├ diff_three_end_constants (只读)          │  └ call_tool(XtalSceneToolset)│
   └ run_golden_tests (pytest)                │  （替代手写 TCP unreal_bridge）│
              │  ★不 import connection.py     └─────────────┬────────────────┘
              │  ★不连 UE/Blender/模型                       │
              └──────────────┬───────────────────────────────┘
                             ▼ lab_simmer（UE 5.8）ToolsetRegistry
                 ┌ 官方：AgentSkillToolset（已注册，技能 CRUD）
                 └ 自建：XtalSceneToolset（C++ 待建，§5）
                     load_xtal_scene / clear_xtal_scene /
                     brush_hte_slots / clear_hte_slots /
                     set_active_joints / 截图证据 …

── 与 Agent 链路完全隔离的桌面工具链路（链路①，保留不动）──
  KinPad GUI (app.py) ─┐
                       ├─→ infra/transport/unreal_connection.py (TCP) ─→ UE 55557
  TWIN 服务 (twin_service) ─┘
  ※ connection.py 已从 mcp/unreal/ 挪到 infra/transport/，仅供 GUI/TWIN 使用，
    Agent 链路与 mcp/asset 均不 import 它。
```

**职责切分原则**：Agent 进程只做“编排 + LLM + 策略 + 交互”，一切领域操作（解析/生成/校验）下沉到 kin_pad `mcp/asset` 纯计算工具；**出-UE 动作经标准 MCP client 调 lab_simmer 官方 MCP 的自定义 Toolset**（不再自研 TCP 协议/命令 dispatcher）。

**kin_pad MCP 重新架构（本次重构）**：`kin_pad/mcp/` 下原 `mcp/unreal/` 的 **Cursor↔UE 纯转发壳已删除**（`server.py` + `tools/` editor/blueprint/node/project/umg/kinematics + `scripts/` 一次性脚本，及 `pyproject` 的 `kin-pad-unreal-mcp` 入口）。这些工具只是把 MCP 调用翻译成 TCP 命令转发 UE，Agent 时代由标准 MCP client 直连 UE 官方 MCP，转发壳冗余。删除后 `kin_pad/mcp/` 只剩 `mcp/asset/`**（纯计算机器闸）**。`connection.py`（GUI/TWIN 的命根子）**不是转发壳的一部分**，已上移到 `infra/transport/unreal_connection.py` 继续服务桌面工具链路（PID 旧工程）。

**UE 通道新旧路（2026-10-30 定）**：旧路 = PID 自研 `UnrealMCP` 插件（TCP JSON 55557 + `UMCPCommandDispatcher`），保留给桌面工具链（GUI/TWIN → PID），不再演进；新路 = lab_simmer（UE 5.8）**官方** `ModelContextProtocol` **插件**，标准 MCP Streamable HTTP（`127.0.0.1:8000/mcp`，协议版本 `2025-11-25`），扩展点 = **Toolset**（`ToolsetRegistry`，3 个元工具 `list_toolsets`/`describe_toolset`/`call_tool` 路由）。编排层的 `unreal_bridge.py`（手写 TCP 客户端）将整体替换为 `ue_mcp_client.py`（标准 MCP client），`sim_harness.toml [unreal]` 同步改端点。

## 4. LangGraph 状态机（核心）

```ts
type DeviceState = {
  deviceJsonPath: string; motorTablePath: string;
  spec?: DeviceSpec;            // parse 后的单一事实源
  tomlDraft?: string; luaDraft?: string;
  errors: GateError[];          // 结构化错误证据
  retries: number;              // 自纠上限
  evidence: Evidence[];         // diff/截图/日志
  approved?: boolean;
}
```

| 节点 | 类型 | 说明 |
|---|---|---|
| `parse_inputs` | 机器 | `parse_hte_device` + `attach_motor_table` → `DeviceSpec`（pydantic 校验） |
| `gen_toml_lua` | LLM | 按 `kinematics_plugin_template` + `passive_lua_gen` 契约起草 TOML/Lua |
| `gate_assets` | 机器 | `generate_assets`（lupa 闸）+ `validate_assets`（ASCII/linkage/lua_compile） |
| `gen_bp_spec` | LLM | 生成 BP 编排指令（组件层级、mesh 名白名单——生产 `j1`/`j0`/`spoon`，禁演示名） |
| `ue_apply_bp` | 机器 | `ue_mcp_client.call_tool("XtalSceneToolset.*")`：spawn / copy_to_blueprint / set_transform |
| `brush_slots` | 机器 | `call_tool("XtalSceneToolset.brush_hte_slots")` → lab_simmer 侧槽位刷入实现（§5，HteDeviceSlot 逻辑自 PID 移植） |
| `verify` | 机器 | `run_golden_tests` + `call_tool` 装载场景 + 查询 Actor 树 + 截图比对 |
| `llm_fix` | LLM | 收 `GateError` 证据 → 修复 draft（`retries < N`） |
| `human_gate` | 中断 | `interrupt()` 挂起，产出证据包待人审；批准→合入，拒绝→`llm_fix` 或终止 |

**关键设计**：边（edge）是确定性路由（闸过往下，未过走 `llm_fix`），**LLM 不决定流程方向**——这是 LangGraph 相对“自由 agent”的价值，也符合“机器闸优先”红线。

**双模运行时（v3 新增）**：本节流水线图是 Agent 的**受控模式**（生产任务，代码保证流程）；Agent 默认模式是**自由 loop 图**（模型自主规划工具调用，用于探索/问答/调试），两图共享 checkpointer / interrupt / 工具集。用户意图为“接入新设备”类时，自由 loop 经**流水线触发器**工具切入本节图（§11 H4）。

## 5. MCP 工具面

**Phase 1 已落地** `kin_pad/src/kin_pad/mcp/asset/`（FastMCP stdio 服务 `KinPadAssetMCP`，6 个工具）：

| 工具 | 说明 |
|---|---|
| `parse_hte_device(hte_json_path, device_key)` | HTE JSON → `DeviceSpec`（含槽位，不含关节/电机） |
| `attach_motor_table(spec, motor_table_path)` | 电机表（CSV/TSV/JSON，中英文列名）→ joints + motors |
| `generate_assets(spec)` | → TOML + 被动 Lua 文本（不落盘；未标定 `pulse_*` 不落盘，绝不写 0） |
| `validate_assets(spec, toml_text)` | 机器闸：TOML ASCII / linkage / lua_compile → `GateReport` |
| `diff_three_end_constants(model, save_dir, cpp)` | 三端几何常数**只读** diff（证据交人审） |
| `run_golden_tests(kin_pad_root, pytest_args?)` | 子进程 `uv run pytest` → 结构化 pass/fail |

模块：`spec.py`（pydantic 红线机器强制）/ `parsing.py` / `emit.py` / `validate.py` / `diffcheck.py` / `golden.py` / `server.py` / `semantic_land.py`（两步链第二步，见下）。
测试：`kin_pad/tests/test_asset_mcp.py`（13 项离线，全绿）。**注意：需** `uv run --extra mcp pytest`（pydantic 在 mcp extra）。

**红线（**`spec.py` **机器强制）**：单位 mm/rad；TOML 数字 ASCII `-`；`pulses_per_unit≠0`；生产关节禁 `*-demo`；被动关节必须有 `passive_expr` 且 `J["x"]` 引用存在；电机↔关节一致。

**两步编排链（CAP8A POC 定形，2026-11-06）**：第一步 = 模型**单发**看资料图+场景清单产出语义结论 `{part, motion, direction}`（专注单发，不进自由环）；第二步 = `semantic_land` 纯工具把结论落地成 JointSpec → `generate_assets` → `validate_assets` → golden 评分。**生成与落地分离**：模型只出语义，几何/命名/红线全机器强制。POC5b 基线（进产物 45/46、type 45/46、passive 45/46、lua 30/30）与失败根因 → `rag/corpus/kinematics/cap8a-two-step-poc.md`、`cap8a-pitfalls.md`；同 mesh 重复件去重已内建。

**UE 侧（Phase 2，新路）**：编辑器原语**官方工具集已全覆盖**（2026-10-30 实测 ~50 个 Toolset 注册，§11 F4），自定义 `XtalSceneToolset` **收缩为仅业务逻辑**。

**① 官方工具集直接** `call_tool`**（零自建）**：

| 编排需求 | 官方工具集.工具 |
|---|---|
| spawn Actor | `SceneTools.add_to_scene_from_class` / `add_to_scene_from_asset` |
| 设/读变换 | `ActorTools.set_actor_transform` / `get_actor_transform` |
| 组件增删查 | `ActorTools.add_component` / `get_components` / `get_root_component` |
| 查 Actor 树 | `SceneTools.find_actors` + `ActorTools.get_components` |
| 建/编译 BP | `BlueprintTools.create` / `compile_blueprint`（53 工具：节点/引脚/变量/函数全 CRUD + graph DSL） |
| 任意对象属性读写 | `ObjectTools.list_properties` / `get_properties` / `set_properties`（**须先 list 再 set，属性名不可猜**） |
| 视口截图证据 | `EditorAppToolset.CaptureViewport` / `CaptureEditorImage` / `CaptureAssetImage` |
| 读 Output Log | `LogsToolset.GetLogEntries` |
| PIE 控制 | `EditorAppToolset.StartPIE` / `StopPIE` / `IsPIERunning` |
| UE 侧自动化测试 | `AutomationTestToolset`（Discover→List→Run→GetResults，可作 golden 的 UE 段） |
| 多工具批组合 | `ProgrammaticToolset`（sandboxed Python 在 UE 内编排其他工具，减少 MCP 往返） |

**②** `XtalSceneToolset` **自建（C++，注册进** `ToolsetRegistry`**，仅业务逻辑）**：

| 规划工具                                   | 说明                     | 来源                                                                                |
| -------------------------------------- | ---------------------- | --------------------------------------------------------------------------------- |
| `load_xtal_scene` / `clear_xtal_scene` | 业务场景 JSON 生成/清空（收文件路径） | 语义对齐 PID `UMCPCommandDispatcher`，lab_simmer 重写                                    |
| `brush_hte_slots` / `clear_hte_slots`  | HTE 设备槽位刷入/清除          | **HteDeviceSlot 逻辑自 PID** `HteVirtualSlotTool` **移植**（核心纯函数 `BrushDeviceOnActor`） |
| `set_active_joints`                    | 驱动运动学 Solver 主动关节      | 新建（依赖 lab_simmer 运动学栈落地后再定）                                                       |

**红线**：单位换算/矩阵落地等规则 001 红线不变；自定义 Toolset 代码归 lab_simmer 工程（不放进 kin_pad）。

## 6. 本地模型接入（`llm/` 适配层）

| 项 | 方案 |
|---|---|
| 抽象单点 | `llm/provider.py` 统一 `chat(messages, tools, schema)`；本地/云端只改配置（`SIM_HARNESS_LLM_BASE_URL`/`SIM_HARNESS_LLM_MODEL`），业务节点不感知 |
| 开发（5060 Win） | **Ollama** + Qwen2.5-7B-Instruct（GGUF，`localhost:11434`）。8G 显存工具调用不稳属正常，只调图/工具/审批 |
| 联调/生产（5090 Linux） | **vLLM** + Qwen2.5-32B-Instruct (AWQ，~20G)，`vllm serve --enable-auto-tool-choice` + guided decoding；5060 跨机 HTTP |
| 硬门槛 | **Tool calling 稳定性** + **guided decoding 强制 JSON schema**（产出 TOML/BP 指令不靠模型自觉），失败走 `llm_fix` |
| 上下文 | 本地模型上下文有限，`DeviceSpec`/证据精简注入；长会话走 LangGraph checkpointer + 摘要压缩（自有 Agent 侧实测） |

**双机工作流**：开发期 5060 Win 原生 Ollama（不碰 Linux/WSL2）；联调/验收切 5090 Linux 原生 vLLM，UE 留 5060，跨机 HTTP。`sim_harness.toml` 切换 `base_url`/`model` 即可，图与工具不变。

## 7. 目录结构（规划）

```
sim_harness/
├─ DESIGN.md                  # 本文档
├─ orchestrator/              # 编排层（LangGraph，Python）
│  ├─ chat.py                 # 自然语言CLI 入口
│  ├─ nl_graph.py             # graph入口，采用ReAct模式（plan→execute→evidence→report）；build_nl_graph(extra_tools=) 可挂扩展工具
│  ├─ asset_tools.py          # 资产编排 4 @tool + 模块级纯函数（@tool 与流水线节点共用同一实现）
│  ├─ asset_pipeline.py       # 确定性资产流水线图（fetch→step1单发→land→emit→gate→score→human_gate[interrupt]→merge；含 run_asset_onboarding 触发器；_PENDING 人审注册表 GUI 专用）
│  ├─ run_asset.py            # 流水线 CLI：interrupt → 控制台审批 → resume → merge
│  ├─ mcp_stdio_client.py     # 通用 stdio MCP 客户端（NDJSON/握手/UTF-8/超时；Blender/kinpad stdio 对端通用）
│  ├─ blender_tools.py        # Blender 实况 @tool：blender_ping/blender_export_scene（写同一份 scene.json，下游零改动）
│  ├─ pipeline.py             # kin_pad编排资产管线（HTE JSON 路线，顺序实现）
│  └─ ue_mcp_client.py        # UE EngineWire MCP 客户端（Streamable HTTP 网关）
├─ skills/                    # Agent 技能包（14 张 SKILL.md，边界见 §11 H2；[nl] skills_dir 可指小样集）
├─ （rag/ 在工作区根，非本目录）  # 语料首批 4 卡 + kg_edges.yaml 已建于 <workspace>/rag/corpus（规则 120 合同位置，Cursor/Agent 共用一份）；embedding/向量库待 H5
├─ ui/                        # 薄 GUI 面板（Chainlit 2.12，chat_ui.py；含 H3/H6b 人审审批卡片）
├─ llm/
│  └─ provider.py             # OpenAI-compat 适配（Ollama/vLLM/云端只改配置）
├─ config/
│  └─ sim_harness.toml        # 端点/模型/路径（禁硬编码，规则 001）
└─ _work/                     # 证据/草稿工作区（非业务目录）

kin_pad/src/kin_pad/mcp/asset/   # Phase 1 已建（§5）

lab_simmer/                       # UE 5.8 工程（UE 落地端，新路）
└─ Source/lab_simmer/ 或 Plugins/ # XtalSceneToolset 自建 Toolset（§5②，F5 延后）
```

## 8. 依赖与红线对齐

- Python：新依赖进 `kin_pad/pyproject.toml` + `uv sync`（规则 `100`）；`mcp[cli]>=1.4.1,<2` 与 `pydantic>=2.6.1` 由主 `.venv` 的 `mcp` extra 提供（`.venv-mcp` 已废弃删除）。
- TS：暂不需要（Agent 全 Python；远期若自研 dsh 插件再引入）。LangGraph 用 **Python 版**（`langgraph`，已装主 `.venv`，清华镜像）。
- 红线：不硬编码绝对路径（`001`）；MCP 参数不散落（`110`）；不为 agent 改 PID 业务路径（`000`）；单位制/ASCII/`ppu≠0`/命名白名单做成 guard 机器校验而非文档约定；`publish_DT` 只做探针。

## 9. 验收（Phase 1 DoD）

1. 给定 HTE 设备 key + 电机表，headless 跑通：`parse → gen → 闸全过 → UE BP 生成且槽位刷入 → golden pass → 截图证据 → human_gate 挂起`。
2. 任一机器闸失败，`llm_fix` 收到结构化错误（哪一步/哪条断言/定位），`retries` 达上限后停等人工。
3. 切换本地 vLLM / Ollama / 云端只改 `sim_harness.toml`，图与工具不变。

## 10. 进度与下一步

| 阶段 | 内容 | 状态 |
|---|---|---|
| ① Python 机器闸工具面 | `kin_pad/src/kin_pad/mcp/asset/` + 13 项 pytest | ✅ 完成（13/13 全绿） |
| ② UE 刷槽位胶水（**旧路 PID**） | `Hte.BrushSlots` / `Hte.ClearVirtualSlots` 非 UI console command | ✅ 代码完成（PID 侧保留可用；**Agent 链路不再走此路**，见 ⑤） |
| ③ 编排层 | `sim_harness/orchestrator` 顺序管线 + `llm/provider` + `config` + `dsh/profile` 骨架 | ✅ 完成（端到端跑通，见 §11） |
| ④ 接模型 | Ollama（5060 开发）/ vLLM（5090 Linux 共享）联调 | ⬜ 待做（`llm/provider` 就绪，待真机） |
| ⑤ UE 通道迁移（**新路 lab_simmer**） | `ue_mcp_client.py`（标准 MCP client）替换 `unreal_bridge.py` ✅；lab_simmer 自建 `XtalSceneToolset` ⬜（**F5 延后**，优先 NL 链路） | 🔵 进行中（客户端已通真机 §11 F6） |
| ⑥ NL 全链路 | 自然语言 → LangGraph(plan→execute→evidence→report) → 官方 toolset → UE 落地；`orchestrator/nl_graph.py` + `chat.py` | 🔵 进行中（Stub-LLM 全链路已通 §11 G；**只欠真模型**——Ollama 下载中） |
| ⑦ 自有 Agent（**新主线**，2026-10-30 晚拍板） | 四能力：SKILL / RAG / 流程执行 / HITL；双模运行时 + 薄 GUI 主入口；dsh 降级参考对照 | ⬜ 待做（设计已定，验收见 §11 H） |
| ⑧ 资产两步链 POC（CAP8A） | 语义单发 + `semantic_land` 落地 → 机器闸 → golden 评分；`asset_tools.py` 4 @tool 可挂自由环；**确定性流水线已落地**（`asset_pipeline.py`） | 🔵 进行中（POC5b 基线 45/46、lua 30/30；step1 单发已固化为流水线节点，触发器 `run_asset_onboarding` 已挂自由环。待办：对称完整性闸、18 多出件专家确认、真模型端到端） |

**③ 说明**：orchestrator 当前为**顺序管线**（`pipeline.py` 每个节点对应 LangGraph 一个节点，边逻辑一致），LangGraph 引入后可平铺替换；LLM 节点（`gen_toml_lua`/`gen_bp_spec`/`llm_fix`）以 `# LLM-HOOK` 标注为确定性占位，④ 接模型后替换为 `llm/provider` 调用。已用 HTE 设备 `5031` + demo 电机表端到端跑通（无 UE/无 golden）：parse→gen→gate→three_end_diff→brush_slots(skipped)→human_gate，证据包 `sim_harness/_work/5031_evidence.json`。

---

## 11. 待验证清单（晚点一起验证）

> 以下均**代码已落盘但未在真机/编译环境验证**。验证时按勾选项逐个确认；失败项贴报错回填本文件。

### A. Python 机器闸（①，已验证 ✅）

- [x] `cd kin_pad && uv run --extra mcp pytest tests/test_asset_mcp.py` → 13/13 全绿
- [x] 端到端：`PYTHONPATH=sim_harness`，`.venv/Scripts/python -m orchestrator.run_device --device-key 5031 --motor-table _work/demo_motors.csv --no-golden` → `ok=true`，证据包生成

### B. UE 刷槽位胶水（②，**旧路 PID**，仅维护桌面链时验证；Agent 链路不走）

- [ ] **B1 编译**：UE 重新编译 `HteVirtualSlotTool` 模块（热重载/Live Coding）通过；`TActorIterator`/`FConsoleCommandWithArgsDelegate` 依赖 `UnrealEd`（Build.cs 已含）
- [ ] **B2 运行**：编辑器控制台 `Hte.BrushSlots <已放置XtalActor名> 5031` → Output Log 打 `创建=N 属性同步=是`，蓝图多出空 StaticMesh 槽位
- [ ] **B3 反向**：`Hte.BrushSlots 不存在的Actor 5031` / `Hte.BrushSlots <Actor> 99999` → 分别报「找不到 Actor」/「设备不在 JSON」
- [ ] **B4 清除**：`Hte.ClearVirtualSlots <Actor>` → 删除对应虚拟槽
- [ ] **B5 同名**：关卡放两个同 ActorLabel 的 Actor，`Hte.BrushSlots <该名> 5031` → 报「N 个同名，请用唯一对象名」

### C. 编排层 ↔ UE 联调（旧路 PID，**已废弃**，由 F 取代）

- [ ] ~~C1–C3（UnrealBridge TCP 55557 透传~~ `Hte.BrushSlots`~~）~~：`unreal_bridge.py` 将被 `ue_mcp_client.py` 替换，本节不再执行。

### F. UE 通道新路（⑤，lab_simmer / UE 5.8 官方 MCP）

- [x] **F1 连通性实测**（2026-10-30 通过）：`POST 127.0.0.1:8000/mcp` → initialize 握手 200，协议版本协商为 `2025-11-25`，`Mcp-Session-Id` 会话管理正常；`tools/list` 返回 3 个元工具（`list_toolsets`/`describe_toolset`/`call_tool`）
- [x] **F2 工具集盘点（初次，UE 未启用 toolset 插件时）**：`list_toolsets` → 仅 `ToolsetRegistry.AgentSkillToolset`（技能 CRUD）；当时结论"须自建"**已被 F4 推翻**——UE 侧启用 toolset 插件后原语层官方全覆盖
- [x] **F3 工程插件盘点**：`lab_simmer.uproject` 已启用 `ModelContextProtocol` + `MCPClientToolset` + `RemoteControl` + `ModelingToolsEditorMode`
- [x] **F4 官方工具集普查**（2026-10-30 通过，UE 侧启用后实测）：`list_toolsets` 返回 **~50 个 Toolset**；与资产编排直接相关的已 describe 核实——`SceneTools`(20：load_level/add_to_scene/find_actors…)、`ActorTools`(17：transform/组件/标签…)、`BlueprintTools`(53：create/compile/节点引脚全 CRUD/graph DSL)、`ObjectTools`(属性 list/get/set)、`EditorAppToolset`(21：CaptureViewport/PIE/相机…)、`LogsToolset`(4：GetLogEntries…)、另有 `AutomationTestToolset`/`ProgrammaticToolset`/`SlateInspectorToolset` 等。**结论：编辑器原语零自建，自定义 XtalSceneToolset 收缩为仅业务逻辑（§5②）**
- [ ] **F5** `XtalSceneToolset` **自建**：lab_simmer C++ 注册自定义 Toolset（§5 工具表）；槽位刷入逻辑自 PID `HteDeviceSlot::BrushDeviceOnActor` 移植
- [x] **F6** `ue_mcp_client.py`（2026-10-30 完成）：编排层标准 MCP client（initialize → notifications/initialized → call_tool，带 `Mcp-Session-Id`，会话失效自动重握手一次）；已替换并删除 `unreal_bridge.py`；`sim_harness.toml [unreal]` 改 `endpoint`。真机实测：ping/list_toolsets（67 个）/官方只读工具（`get_current_level` → `/Game/level/demolevel`）全通；业务工具未建时优雅报错（`Toolset 'XtalSceneToolset' not found`）；管线无-UE 回归 `ok=true`
- [ ] **F7 端到端带 UE（新路）**：`run_device.py --device-key 5031 --motor-table <电机表> --ue` → 经 `call_tool` 刷槽成功，证据包含 UE 侧确认

### G. NL 全链路（⑥，自然语言 → UE 场景落地）

- [x] **G1 官方原语手动验通**（2026-10-30）：`add_to_scene_from_asset(/Engine/BasicShapes/Cube.Cube)` 生成立方体 ✅；`find_actors` 查到 ✅；`CaptureViewport` 截图存证 ✅（`_work/cube_evidence.png`）
- [x] **G2 toolset 参数怪癖校准**：UE toolset 校验器要求**无默认值参数全显式传**（`find_actors` 须传 `tag`+`collision_channels`；`CaptureViewport` 须传 `captureTransform`+`annotations`）；schema 以 `describe_toolset` 实测为准，已固化进 `nl_graph.TOOL_DEFS` 的 required
- [x] **G3 LangGraph 版图**：`orchestrator/nl_graph.py`（plan[原生 tool calling→`_TOOL_ROUTE` 查 toolset+重试] → execute[call_tool] → evidence[截图] → report[模板]）；工具面 5 个（spawn/查/删/移/截图）
- [x] **G8 function calling 改 tool 模式**（2026-11-03）：plan 由「prompt 塞手写 schema + 文本 JSON 手抠（pydantic 校验）」改为**原生 OpenAI tool calling**（`TOOL_DEFS` 声明 + 读 `tool_calls`）；stub 链路 + 5090 32B + Ollama 7B 三端端到端全绿（圆锥/立方体均一次到位，证据 `nl_evidence.png`）
- [x] **G9 ReAct 环**（2026-11-03）：单步图升级为 plan⇄execute 环（回边 + Observation 回喂 + 收敛判据 + max_steps 兜底）；`ChatMessage` 扩 `tool_calls`/`tool_call_id`（协议硬要求），`_to_observation` 消毒（CaptureViewport base64 绝不进历史）；**执行失败二分**：传输失败（`raw` 空）短路、工具拒绝（`raw` 非空）回喂自纠。三端全绿；7B 实测「漏 `asset_path` → UE 拒绝 → 回喂 → 自纠补上 → 生成 → 截图」3 步收敛（H1 自由 loop 的预演）
- [x] **G10 工具面 @tool 化**（2026-11-03）：手写 `TOOL_DEFS`/`_TOOL_ROUTE` → LangChain `@tool(args_schema=pydantic)`（函数 = schema = 执行体单一事实源，`convert_to_openai_tool` 自动生成声明）；**invoke 先本地 pydantic 校验**（先于 UE 拦截坏参数，且不计步）。修存量 bug：`set_actor_transform` 真实 schema 是 `(actor, xform)` 嵌套（平铺参数从未真机跑通过）；新增第 6 工具 `get_actor_transform`（动作后自查，实测确认 z=250/200 移动生效）。加固：防跳步 prompt（"没调用工具的动作绝不允许声称已完成"）、LLM HTTP 异常 plan 内兜底不炸穿图、max_steps 默认 8。5090 四步复合指令（生成→移动→自查→截图）全绿。**review 加固**（同日）：多 tool_calls 裁剪回放（未配对 id 会 400）、校验失败不计步但计连续 fails（>=3 中止，成功即清零，防无限环 + 防误伤）、收敛结论落 messages、`recursion_limit=100`（8 步×3 super-step 超默认 25）、prompt 规则消歧（"已完成"须对应成功调用 vs 做不到如实说）、首轮空响应重试一次、校验失败 obs 附参数清单（`_args_hint`，自纠效率：拦 1 次即修正）
- [x] **W1-W3 UE 通道转 EngineWire + 网关模式**（2026-11-04 通过）：UE 通道再次切换（lab_simmer 官方插件 → **PID 编辑器内 EngineWire 0.6.0 插件**，`127.0.0.1:3000/mcp`，`X-MCP-Capability-Token` 鉴权，token 读 `<PID>/Saved/MCP/capability-token`，toml `[unreal] token_file` per-machine 配置）。**EngineWire 自身即网关**：tools/list 只暴露单个 `unreal` 工具（search/describe/execute/configure 四 operation，58 domains 数百能力），与我们预判的网关抽象一致——`nl_graph.py` 删 6 个硬编码 @tool 换**单个** `unreal` **网关 @tool**（薄层零映射纯横切：token/会话、失败二分、search/describe 回执压缩、截图消毒、`steps` 仅计 operation=execute、configure 不开放给模型）；evidence 节点摘除（截图变普通能力，execute 横切自动落盘）。`ue_mcp_client.py` 重写（标准 tools/call 直发；**写操作回 SSE 流需解析 data: 块**；**截图走 MCP image content 块**须单列落盘；回执为"状态行+空行+JSON"）。实测：spawn/delete(consentGrant nonce 制)/list/screenshot 全通；32B 端到端「生成→截图」13.5s；复合指令「delete+spawn+截图」spawn/截图成、**delete 被 consent 闸拦下模型如实上报未完成**（CONSENT_REQUIRED——destructive 能力 W4 接 H3 人审，当前安全默认）。`kin_pad/mcp/unreal` 转发壳同日物理删除（出-UE 全部经本链路，不经 kin_pad 中转）
- [x] **W3.5 业务 skill 机制 + 刷槽位 MCP 化**（2026-11-04 通过）：HTE 刷槽位经 `control_editor.console_command` → `Hte.BrushSlots <Actor> <DeviceKey> [JsonPath]` 零 C++ 接入（插件控制台命令本来就是为 MCP 预留的入口）；模型一度用原生蓝图能力自由发挥**误改 BP_5001_HTE_Reactor**（已 consent 流程清理还原）——根因 = 业务通道不在能力目录，对策 = **skill 层**：`sim_harness/skills/hte-brush-slots/SKILL.md`（与 `.cursor/skills` 同格式同语料）+ `nl_graph._load_skills` 全量注入 system prompt（最小实现，skill 增多后接 RAG 索引）。验证：同一指令从"误改他图"变为"走 Hte.BrushSlots + read_log/get_scs 复核 + 幂等（创建=0 跳过=2）如实收敛"。附赠修复：**client SSE 中文截断**（requests Latin-1 + `splitlines()` 把中文 UTF-8 字节里的 NEL 当行界 → 改 `resp.content.decode('utf-8')` + `split('\n')`）；EngineWire **限流退避**（-32000 rate limit 120/min，按 "Retry in N s" 解析等待重试）；**consent nonce 流程**（describe 顶层 `consentGrant` → execute 带 `consent` 兄弟字段）已验证可用于 destructive 能力
- [x] **G4 Stub-LLM 全链路集成**（2026-10-30 通过）：Stub 计划 + 真 UE + 真 LangGraph → 球体生成、截图、find_actors 复核全过（`_work/test_nl_stub.py`，`_work/nl_evidence.png`）
- [x] **G5 CLI 入口**：`python -m orchestrator.chat "在UE中新增1个立方体网格"`（读 `sim_harness.toml [llm]/[unreal]`）
- [x] **G6 真模型端到端**（2026-11-02 通过）：Ollama `qwen2.5:7b` 一把过——`chat.py "在UE中新增1个立方体网格"` 11.4s 完成：LLM 产出合法计划（`add_to_scene_from_asset`）→ UE 生成 `StaticMeshActor_UAID_…` → 证据截图 `_work/nl_evidence.png`；`find_actors`（`name=''` 枚举）复核场景在线。注意：`find_actors` 的 `name` 为精确匹配，模糊查找传空串枚举
- [x] **G7 安装 Ollama**（2026-11-02 完成）：安装包 1.6GB 经 gh-proxy 镜像下完（签名 Valid=Ollama Inc.）；本机深信服 EDR（`savsvc`/`edr_monitor`）静默杀自动安装进程（两次无日志死亡 + `sfavsvc` IOC blocker 窗口期崩溃 2 次），**最终用户手动安装成功**；serve 0.40.13 在线，`qwen2.5:7b`（4.7GB）已拉取；启动目录自续 `.cmd` 已清理

### D. 模型接入（④，待 5060/5090 就绪）

- [x] **D1 Ollama（5060 Win）**（2026-11-02 通过）：`qwen2.5:7b` 已拉取，`[llm]` 指 `localhost:11434/v1`；G6 端到端已证明 provider→模型→工具计划链路；G8 复测 7B 原生 tool calling 一次到位
- [x] **D2 vLLM（5090 Linux）**（2026-11-03 通过）：`http://10.249.21.47:8000/v1` 在线，`/v1/models` 见 `Qwen/Qwen2.5-32B-Instruct-AWQ`，无 API key；5060 跨机直连通。四层能力实测 4/4（`_work/test_5090.py`）：基础对话 0.9s / **tool calling**（`get_weather(苏州)` 正确）/ **guided JSON** / 中文领域推理，单次 0.4-1.0s
- [x] **D3 guided decoding**（2026-11-03 通过）：`response_format=json_schema` 请求返回严格 schema 的 JSON（计划四字段齐，T3 实测；G8 起 NL 链路改用 tool 模式，本能力留给 D4 结构化起草）
- [x] **D3b 32B 实战规划**（2026-11-03）：`run_nl_command("在UE中新增1个圆锥网格，放在 z=150", 5090)` → 计划满分（全名 toolset + Cone.Cone + z=150 + 自动命名 `MCP_Cone_1`）；execute 因 UE 编辑器未开报连接拒绝（非模型问题）。**修复**：节点返回值改 delta-only（`{**state,...}` 全量回写在条件边调度下触发 `InvalidUpdateError: At key 'user_input'`），evidence 透传改 `return {}`
- [x] **D5 VL 截图回喂（Qwen3-VL）**（2026-11-05 通过）：5090 模型换 `tclf90/Qwen3-VL-30B-A3B-Instruct-AWQ`（多模态，max ctx 16384）。冒烟确认：OpenAI multimodal parts（user 带图）vLLM 直收，一张视口截图 ~1100 prompt tokens；**带图+tools 同轮兼容**（首轮带图/ReAct 中段 assistant→tool→user 带图两形态均 200）。改造：`ChatMessage.images`（data URL 列表，仅 user 角色；`to_dict` 组装 vision parts）；`LlmConfig.vision` 总闸（toml `[llm] vision`，纯文本模型必须 false 防 400）；nl_graph 横切——截图 data URL 经网关 tool 闭包 `sink` 旁路 → `state.vision_image` → plan 注入带图 user 消息（消费即清零），**历史旧图同轮降级为纯文本**（单证据语义，防 token 累积）；`_VISION_SUFFIX` prompt 段教模型"截图后能真看到视口，动作后看图自查"。端到端实测 12-18s：空间问答（据图描述实验室布局：左墙设备+通风管/中央工作台/右侧玻璃隔断，与证据图吻合）；**动作自查闭环**——生成圆锥后截图，模型看图发现未出现，如实上报"未发现圆锥体"（不再凭"截图已保存"谎称完成，防跳步有视觉证据支撑）。Ollama qwen2.5 纯文本路径不受影响（vision=false 时零行为变化）。**实战两坑**（同日 GUI 复测暴露）：① Chainlit **浏览器刷新≠进程重启**——GUI 代码改动必须杀 python chainlit 进程重启（对时：进程 CreationDate vs 文件 mtime），否则跑旧逻辑；② 模型可能显式传 `returnBase64=false`（回执只剩 UE 侧路径文本，无图可喂）→ 网关 tool 对 `.screenshot` 后缀能力**强制覆盖 returnBase64=true**（证据落盘/VL 回喂都依赖 image 块，与消毒横切同级）。**用户上传图链路**（同日）：GUI 上传 500 = Chainlit `upload_file` 只 `mkdir(exist_ok=True)` 不带 parents，父目录 `.files` 需预建（chainlit Windows 版本缺陷）；附件闭环 = `chat_ui` 读 `message.elements` 图片 → data URL → `run_nl_command(user_images=)` → 初始 user 消息带图（**首轮即见**，实测 steps=0 直接据图作答 8.3s）；配套修正——plan 旧图降级改为**仅在新截图注入时剥**（原无条件剥会在首轮就误杀用户上传图；无新图时上传图全程保留可引用）。**图+数据双通道与多角度 QA**（同日，参照 unreal-agent-harness ue_qa.py）：① `.screenshot` 横切三连——强制 `returnBase64=true` + `includeMetadata=true`（回执 data 带相机位姿/尺寸，`_sanitize_execute` 压成 `[meta] WxH cam=(x,y,z) rot=(p,y,r)` 紧凑行进 obs 免 500 截断，VL 看图判相对关系+meta 给精确坐标）+ 缺省注 `resolution=1024x576`（实测 VL prompt 1363→557 tok 省 59%，模型可显式覆盖）；② **EngineWire 截图 location/rotation 是永久移动编辑器相机**（无官方 captureTransform 的 ON_SCOPE_EXIT scope 语义，实测紧随截图 cam 停留新机位）——多角度 QA 方案（机位编排/恢复时机/验收 checklist）**暂缓落地待思考**（同日用户定），skill 复核仍走文本通道（日志+SCS+命名核对）。端到端实测：模型精确报出相机位姿（数值来自 meta 非目测）+ 据图描述设备细节，8.1s。kin_pad 新增 pillow 依赖（后续 refdiff 参考图对比备用）
- [ ] **D4 替换 LLM-HOOK**：`pipeline.py` 三个 `# LLM-HOOK` 节点改调 `llm/provider`，端到端用模型跑通（7B 不稳属正常，换 32B 验证稳定性）

### E. dsh（已降级：参考对照 + 可选宿主——2026-10-30 晚拍板）

> 自有 Agent（H 线）立起来后，dsh 不再是主线/依赖。本节保留已验证事实作参考档案；E1b/E2/E3 **取消**（不再追 dsh 配本地模型，不做其审批/headless 接入）。远期若要 dsh 作宿主，只经 H7 的 MCP 封装做**任务级委托**。

- [x] **E0 物料确认**（2026-10-30）：`DeepSeek Harness.exe` 安装于 `%LocalAppData%\Programs\DeepSeek Harness`；`$DSH_HOME=C:\Users\lei.wang02\.dsh`，desktop profile 已初始化（`profiles/desktop/`）
- [x] **E1a MCP 注册**（2026-10-30）：直写 `profiles/desktop/cordis.patch.yml`（已备份 `.bak-20261030`），追加两条 `@deepseek-ai/dsh-mcp-client`：① `ue`（streamable-http `http://127.0.0.1:8000/mcp`）② `kinpad_asset`（stdio，主 `.venv` 的 `-m kin_pad.mcp.asset.server`）。dsh 桌面版可直连两个 MCP 服务当探索工具用

- ~~E1b 模型端点 / E2 三态策略 / E3 headless~~：取消（理由见上）

### H. 自有 Agent 线（⑦，**新主线**——2026-10-30 晚用户拍板）

> 目标：SKILL / RAG / 流程化任务执行 / 人机交互四能力全自有。主入口 = 薄 GUI 直连（HTTP+SSE），MCP 封装仅跨宿主任务级委托。迭代批次即验收顺序：

- [ ] **H1 MLP 自由 loop**：`agent/` 自由 loop 图（LangGraph）+ 工具装配（ue / kinpad_asset 经 MCP）+ CLI 对话框跑通——证明"模型能自主用我们的工具链干活"
- [ ] **H2 skill loader**：`skills/` 目录 + frontmatter 解析 + `use_skill` 工具 + 系统提示词清单注入；格式同 `.cursor/skills`，语料一套两端共用。**边界（2026-11-07 定）**：Agent 库只收**使用工程工具的经验**（产物契约/工具用法/标定经验），工程实现（C++/proto/路由内部）留 `.cursor/skills` 开发侧——纯实现 3 张已撤出（`_work/skills_archive/`），混合型已抽实现段。**ctx 实测（2026-11-07）**：17 张全量注入 = 16481 tok 爆 Qwen3-VL 16K 上限 → 过渡方案 `[nl] skills_dir` 指 `_work/skills_mini`（3 张相关卡）；根治靠 H5 按需加载
- [x] **H3 interrupt 审批**（2026-11-10 落地，CLI+GUI 双载体；**同日升级 dsh 式三态**）：流水线 human_gate = LangGraph `interrupt()` 挂起出证据包。核心矛盾 = interrupt/resume 三要素（同一 graph 实例 + checkpointer + thread_id），而 InMemorySaver 是进程内存 dict、GUI 每条消息重建图会丢挂起态 → `asset_pipeline._PENDING` **模块级人审注册表**（Chainlit 长驻进程，挂起态跨消息存活，GIL 原子无需锁；多会话远期换 session id 作 key）：`start_interactive_onboarding` / `pending_review` / `resolve_pending(key, approved, feedback)`。**三态**：批准合入 / **驳回带意见重修**（意见进 llm_fix 最高优先级证据、`revisions` 独立计数上限 3、驳回即重置机器自纠预算、重修后再挂起等下轮审批）/ 终止（草稿留 asset_run 可溯）。GUI 三按钮+文字（`批准`/`驳回:意见`/终止，驳回按钮后置等意见态收文本）双通道，审批意图**正则硬路由**（意图不经模型判，有待审时其他消息一律提示先审批）；CLI `run_asset.py` 同步三态 `[y/r/N]`。合入目录走 `[asset] merge_dir`（空 = 批准也不复制）。`_work/test_review_gate.py` 7 组全绿（①批准②拒绝③空待审④提前失败⑤驳回→重修→再挂起→批准⑥终止⑦轮次用尽收口）+ 流水线回归绿
- [x] **H4 流水线意图识别**（2026-11-09 落地）：**资产链预演（2026-11-07）**：4 资产 @tool 挂自由环链路全通，但**环内语义步质量确定性不足**（type 2/46，temp=0 复跑逐字节相同——形态问题非采样噪声）→ 确认第一步语义须**单发专注**。据此落地**确定性流水线** `orchestrator/asset_pipeline.py`：fetch_scene（文件/Blender live 双源）→ step1_semantics（POC5 形态单发，temperature=0）→ land/emit/gate（机器，与 asset_tools 同一实现）→ score → **human_gate（LangGraph interrupt）** → merge（批准+配 merge_dir 才出 asset_run）；land/emit/gate 失败 → llm_fix（retries≤3）→ 到顶停等人工。CLI `run_asset.py`（控制台审批续跑）；自由环触发器 `run_asset_onboarding`（非交互，证据包回报）已挂 nl_graph，`_ASSET_SUFFIX` 改以它为整机首选。离线 `_work/test_asset_pipeline.py` 全绿（批准/拒绝/fix 环/retries 到顶/live 源/优雅失败 6 组）。**真模型端到端（2026-11-09 通过）**：`run_asset` CLI + 5090 Qwen3-VL-30B + CAP8A 资料图，22s 全链——step1 首发第 51 条 `motion='平il'` 缺 direction（"径ial" 同类坏词），**解析回推重试实战生效**；60 关节、闸全过、自纠 0 次、lua 30/30；控制台批准 → merge 双文件落盘。注意：type 39/46 低于 POC5b 基线 45/46——通用 prompt 的设备描述（"这台CAP8A设备"）不如 POC5 具体描述（"这台 8 工位试管开关盖机"），motion 分类质量随设备提示具体度波动，待加 `device_hint` 参数。GUI interrupt 审批 UX 已落地（H3，2026-11-10）。**触发器前置闸（2026-11-10）**：实测"接入新设备四联移液"（未给场景来源）模型瞎猜 `use_live_blender=true` → 流水线 fetch 报错 → 模型转去 UE 连搜 4 次 "get config" 撞 probe 闸——教训 = **工具报错必须带指令级措辞**（含糊报错模型会自由发挥）。修复：`run_asset_onboarding` 加 fail-fast（`use_live_blender=true` 但 `[blender] command` 空 → 直接返回"禁止转向 UE、向用户要 scene_path 并结束"），`_ASSET_SUFFIX` 同步补"没给场景来源先问用户"。**二次加固（2026-11-10，同症状复现）**：30B 对"报错后问用户"软指令依从不足——用户明说"blender mcp正开着"模型仍不传 `use_live_blender`、被退件后去 UE 搜 "blender export scene" → 改**确定性硬拦截**：unreal 网关 search 命中 `blender/场景导出/资产编排/.toml/.lua` 等非 UE 词直接短路返回正确出口（不打 UE 但仍计空转，probe 闸继续兜底）；`_PLANNER_SYS` 加"报错要求问用户就立刻问完结束本轮"；触发器守卫文案改指令级；已配 `blender_command` 时缺省场景来源自动 = live。待办：device_hint、Blender 薄 server（live 源真接通）
- [x] **Blender MCP 接入**（2026-11-09 client 层；**2026-11-10 真接通**）：`mcp_stdio_client.py`（通用 stdio MCP 客户端，NDJSON/握手/UTF-8/超时，与 UeMcpClient 同构错误三件套）+ `blender_tools.py`（`blender_ping`/`blender_export_scene`，写**同一份** `asset_run/scene.json`，下游零改动；契约 `[{name,parent,matrix_local(16平铺,m)}]` 与文件流一致）。`sim_harness.toml [blender] command` 配置启动行（空=未配置，文件流兜底）。**对端 = vendor 版 mcp-for-blender 2.2.7**（`_work/vendor/blender-mcp`，uv 装入 kin_pad venv；`launch_blender_mcp.py` 启动器注入遥测关/更新检查关；Blender 侧 addon 点 Connect，桥走 socket:9876）：其无我们的直出契约工具，但有 `execute_blender_code`（活 Blender 内跑 bpy 片段、回执=stdout）→ `export_scene_items` **双契约适配**（有 `export_scene_objects` 直出工具直调——fake server/未来薄 server；否则注片段 print `__SCENE_JSON__`+JSON，按标记截取），fetch_node 与 blender_export_scene 共用。实测：握手列出 9 工具；**真 Blender 导出 42 物体**（首件"四联移液_2U底板"，中文名 UTF-8 链路无损）。safe_mode 默认关，即使用户开，导出片段（bpy+json+推导式）也在白名单内。kinpad asset MCP 保持进程内 import（自有 Agent 内部不付 MCP 税），stdio server 留作跨宿主门面
- [ ] **H5 RAG**（**优先级上调**：H2 实测全量注入爆 16K ctx）：corpus 首批 4 卡 + `kg_edges.yaml` 已建于 `<workspace>/rag/corpus/`（规则 120 合同位置，Cursor/Agent 共用）；待做 = 本地 embedding（Ollama/vLLM 均可）+ 向量库 + `search_knowledge` 工具 + skill 按需加载
- [x] **H6a GUI 最小版**（2026-11-02 提前落地）：`ui/chat_ui.py`（Chainlit 2.12，端口 **8501**——8000 被 UE MCP 占）单命令面板：消息 → `run_nl_command` → 报告 + 证据截图内联；启动器 `ui/启动GUI.cmd`（双击）或 `python -m chainlit run ui/chat_ui.py --port 8501`
- [ ] **H6b GUI 完整版**：~~interrupt 审批 UI~~（2026-11-10 随 H3 落地：审批卡片 = 证据摘要 + 三态按钮/文字双通道）+ 流式输出 + 多轮会话记忆（等 H1 自由 loop 接入后做）
- [x] **H8 trace 可视化（LangGraph Studio）**（2026-11-10）：`langgraph-cli[inmem]` 装入 venv；`langgraph.json` 注册两图（`asset`/`nl`，工厂 `orchestrator/studio_graphs.py` 零参装配读同一份 toml；nl 图在 Studio 里 interrupt 由线程自管故触发器 interactive=False）；`ui/启动Studio.cmd` 双进程（dev:**2026** + 垫片:2025→2026）。六个坑：①**PYTHONUTF8=1 中文区硬要求**（langgraph_api 裸 `open()`，GBK 区解码 UTF-8 即炸）；②**baseUrl 必须指垫片 :2025**——浏览器公网页→回环属 Private Network Access，langgraph dev 主动拒 PNA 预检（直连报 Failed to fetch），`orchestrator/studio_cors_shim.py` 本机垫片补 `Access-Control-Allow-Private-Network: true` 并剥 Origin 转发（`--tunnel` 走 Cloudflare 数据出机，合规不采用）；③**图工厂必须模块导入期 eager 构建**——blockbuster 阻塞侦测在 `/assistants/{id}/schemas` 请求上下文抓工厂内 I/O，故 `_ASSET_GRAPH`/`_NL_GRAPH` 模块级建好后工厂只返回缓存；④**dev 挪 2026**——2024 被幽灵 socket（netstat 有 LISTENING 但进程已死）占用；`--allow-blocking` 只豁免 runtime worker 不豁免 API schema 路径，watchfiles 刷屏且 reload 挂起故加 `--no-reload`；⑤**症状"schemas/graph 500 空正文"= 垫片双实例抢 :2025**（Windows SO_REUSEADDR 允许同端口两进程 LISTENING，旧垫片仍指死上游 2024，请求被随机分给坏的）——启动前必须 `Get-CimInstance Win32_Process` 清掉所有 `studio_cors_shim` 旧进程，且排查顺序：**先绕垫片直连 dev 端口**分清 500 来自 dev 还是垫片；⑥**nl 图须有 `prep` 入口节点**——Studio 直调只有 `user_input`，没有 Python 包裹层播种 messages（实测 KeyError），`build_nl_graph(sys_prompt=...)` 非空即挂 prep：messages 空则播种 [system, user]+计数清零、有历史则透传；系统提示词单一来源 = `assemble_planner_prompt`（run_nl_command / Studio 工厂共用）。Studio URL：`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2025`。冒烟：垫片链路 `/ok`、`/assistants/search`、`/assistants/{id}/schemas`、`/graph` 全 200。分工：Studio 管调试（trace/单步/时间旅行），Chainlit 管生产对话与三态审批；LangSmith 云 trace 因数据合规不启用
- [ ] **H7 MCP 封装（远期）**：Agent 整体包 MCP server 对外任务级委托（`run_device_onboarding` 等粗粒度工具）；dsh / Cursor 可作宿主

> 待办提醒：`sim_harness/` 尚无 Python 项目文件（`pyproject.toml`），编排层依赖复用 kin_pad venv（toml/pydantic）；若后续 sim_harness 独立，需补 `pyproject.toml` 并把 `orchestrator` 纳入依赖管理（规则 100）。

---

## 附：关键决策记录

- **工具下沉 Python**：复用 kin_pad SSOT，Agent 进程只编排（v3 起全 Python，无 TS 侧）。
- `pulse_*` **在 asset 层注入 TOML**，不走 `Joint.to_dict`（`Joint` 域模型不含脉冲字段，`patch_joint_matrices` 亦显式保留原值）。
- **三端 diff 只读**：三端同步是硬红线（规则 `100`/`300`），证据交人审，不自动改。
- `mcp/asset/__init__.py` **不 eager-import 子模块**，避免未装 mcp extra 的环境导入即崩。
- `mcp/unreal` **转发壳已删、**`connection.py` **上移** `infra/transport/`：kin_pad MCP 只留纯计算 `asset`；GUI/TWIN 用 `infra/transport/unreal_connection.py`。（2026-11-03 物理落实：`src/kin_pad/mcp/unreal/` 目录与残留 `kin_pad/mcp.json` 均已删除，`mcp/__init__.py` docstring 同步；此后对 UE 的操作全部由 Agent 直连，不经 kin_pad 中转。`tests/` 77 passed，13 个失败为 `save/shuangbihengyi.toml` 缺失的既有基线，与本次删除无关。）
- **UE 走新路（2026-10-30，用户拍板）**：后续 UE 架构与开发全部在 **lab_simmer（UE 5.8）** 进行，出-UE 通道 = 官方 `ModelContextProtocol`（Streamable HTTP 8000）+ 自定义 Toolset；PID 自研 UnrealMCP（TCP 55557）冻结为旧路，仅服务既有桌面工具链，不再演进、不再作为 Agent 落地目标。
- **vLLM 放 5090 Linux 原生**绕开其 Windows 不友好 + 避免与 UE 抢 32G 显存；开发期 5060 全程 Ollama Windows 原生。
- **目标 = 自有 Agent（2026-10-30 晚，用户拍板）**：构建 SKILL / RAG / 流程执行 / HITL 四能力全自有的 Agent；"把 LangGraph 包成 MCP 服务"只是对外接口形态，不是本体。
- **kin_pad 不作 Agent GUI 入口**：kin_pad 保持现状（桌面工具链 + `mcp/asset` 机器闸），Agent GUI 另写薄面板（`ui/`），互不侵入。
- **主入口直连，MCP 仅委托**：日常对话 / 审批走薄 GUI → Agent 运行时（HTTP+SSE，interrupt 原生）；MCP 封装只用于跨宿主**任务级**触发——dsh 类宿主有双 loop + 云模型中继问题，不适合做对话入口。
- **双模运行时**：自由 loop（默认，探索/问答）+ 机器闸流水线（生产，意图触发）共享 LangGraph state / checkpointer / interrupt。
- **知识语料一套两端用**：Agent `skills/` 与 `.cursor/skills` 同格式同语料；RAG corpus 建好后 Cursor（规则 `120`）与 Agent 共用——知识只有一份，宿主随便换。
- **dsh 降级**：代码 / 运行时不依赖；其三态审批 UX、skill 热加载、MCP 网关作设计参考。
- **LangGraph 节点返回 delta-only（2026-11-03）**：节点函数只 `return` 自己负责的键，不 `{**state, ...}` 全量回写（条件边/path_map 调度下会触发 LastValue 并发写冲突 `InvalidUpdateError`）；透传用 `return {}`。
- **function calling 用原生 tool 模式（2026-11-03）**：废弃「prompt 塞手写 schema + 文本 JSON 手抠 + pydantic 校验」变体。`TOOL_DEFS`（OpenAI function tools 声明）+ `_TOOL_ROUTE`（函数名→toolset 全名/UE 工具名映射，撞名再加 `scene__` 前缀 alias）；空 `tool_calls` = 越界显式出口；幻觉名/坏 arguments JSON 回喂重试一次。vLLM/Ollama 均实测支持；guided JSON（D3）转给 D4 结构化起草用。
- **ReAct 环三要素（2026-11-03，G9）**：① 回边 evidence→plan；② Observation 以 `role="tool"` 消息回喂（`tool_call_id` 与 assistant 的 `tool_calls` 配对——缺一 vLLM/Ollama 直接 400；截图 base64 消毒成文字，否则爆上下文）；③ 终止 = 模型不再调工具（完成/直接答/越界三合一，结论即 report）+ max_steps 兜底。**执行失败二分**：`resp.raw` 空 = 传输层失败（UE 未开）短路 report；`raw` 非空 = 工具拒绝（校验错误含 schema 文本）回喂走环——失败也是 Observation，7B 实测能自纠。校验失败重试留在 plan 内部（2 次），不占用环边、无自环。
- **@tool 模式约定（2026-11-03，G10）**：① langchain_core 1.7 **不解析 docstring Args 段**（整段塞工具 description），参数说明必须写 `Field(description=...)`、工具显式 `args_schema=`；② invoke 后嵌套参数以**模型实例**传入（`x.model_dump()` 还原 dict 喂 UE）；③ 本地 pydantic 校验失败**不计 steps**（未碰 UE，steps 只计真实执行），max_steps 默认 8；④ 无默认值字段 = 必传，镜像 UE 校验器怪癖；⑤ UE 工具真实 schema 以 `describe_toolset` 实测为准（`set_actor_transform` 是 `(actor, xform)` 嵌套，勿凭直觉平铺）；⑥ `get_actor_transform` 供模型动作后自查，配合防跳步 prompt（没调工具的动作不得声称完成、不输出"我将再次尝试"式中间意图）。
- **两步链：生成与落地分离（2026-11-06 定形）**：模型只做单发语义判断（`{part, motion, direction}`），JointSpec/TOML/Lua 落地全机器（`semantic_land` → `emit` → `validate`）；语义质量不收口于 prompt 工程，而收口于"单发专注 + 机器闸 + golden 评分"。
- **自由环不做语义步（2026-11-07 实测）**：资产 4 工具挂 ReAct 环链路全通，但模型在环内按件名字眼整批误判 motion（type 2/46），temp=0 复跑逐字节相同——非采样噪声而是形态问题；第一步单发（POC5）做成 `step1_semantics` 工具、由流水线编排是正路（H4 模式）。
- **skill 全量注入有 ctx 上限（2026-11-07）**：17 张 SKILL.md ≈ 16.5K tok > Qwen3-VL 16K ctx；过渡用 `[nl] skills_dir` 小样集，根治 = H5 RAG 按需加载（优先级因此上调）。
