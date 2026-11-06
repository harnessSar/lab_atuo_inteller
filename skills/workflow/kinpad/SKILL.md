---
name: kinpad
description: >-
  kinpad主要任务是编排运动学资产，包括主动运动的toml描述文件，被动的lua描述文件。
---

# kinpad 运动学资产编排

一台设备的运动学资产 = 主动关节 TOML + 被动关节 Lua（五件套解剖见 `device-overview`）。生成走 `kin_pad/mcp/asset/emit.py`，机器闸在 `validate.py`，编排原则是**生成与校验分离、人审通过后才落盘**。

## 主动运动的toml

主动关节的全部事实在 `[[joints]]` 数组：

| 字段 | 取值 | 说明 |
|------|------|------|
| `name` | Blender mesh 名原样（中文名照用） | = UE 组件名 / Lua 键；禁 `*-demo`（演示 Actor 保留名，污染 Solver Lua 键） |
| `joint_type` | `Revolute` / `Prismatic` | 旋转→rad，平移→mm |
| `joint_axis` | `X/-X/Y/-Y/Z/-Z` | **相对父级的局部轴**，带符号 |
| `origin_matrix_local` | 4x4 嵌套数组，平移 mm | 相对父级零位矩阵，来自 Blender `matrix_local`×1000；零位时刻 `matrix_local` 与 origin 相同 |
| `sort_index` | int | 生成排序（同 index 按 name 字典序） |

红线（pydantic / 机器闸强制，不靠模型自觉）：

- 单位 **mm / rad**；TOML 数字必须 ASCII `-`（Unicode `−` 破坏 toml++ 解析）
- 电机字段按 `joint_name` 配对注入 `motor_id` / `pulse_zero` / `pulses_per_unit`；**未标定不写入，绝不写 0**（ppu=0 会让整帧脉冲失败）；min/max_position 未标定同样不落盘
- `passive_expr` **不落 TOML**（SSOT 在 Lua，见下节）；有被动关节时 TOML 只回填 `[kinematics].script_path = "./{stem}_passives.lua"`

轴字母不许凭空写：两步编排的第二步由 `semantic_land.resolve_axis` 定——方向词先映成世界向量（Blender Z-up：上下=Z、前后=Y、左右=X；**径向** = 件位置 − 直接父级中心的水平分量），再 `R_world^T · v` 取点积最大轴并带符号。方向词容错前缀匹配（`径` 覆盖"径ial"等模型坏词）；无父级、与父级同心等退化情形转**人审提示**，不静默兜底 Z 轴。

## 从动运动的Lua

- 被动关节：`is_passive=true` + `passive_expr`——**原生 Lua** 表达式，`J["name"]` 引用他关节位置，可用 `math.*`、`and/or`、比较等完整语法；引用目标必须存在于 joints（linkage 闸）
- 镜像分组：同 parent + 同名字前缀（去尾部编号）为一组，最小编号主动，其余 1:1 `follow` **同组第 1 个**（不是 parent）——纯机械规则，`auto_passives` 自动判定，不需要模型
- 产物契约：`M.resolve_passives(J) -> { positions = { name = value, ... } }`；`M.init(params)` 为可选空实现钩子；文件头有自动生成注释，线性从动勿手改
- 一份 Lua 两端加载：kin_pad（lupa）与 UE（UnLua `FXtalKinLuaHost`）读**同一个** `{stem}_passives.lua`（stem 默认取设备 name 小写，可显式指定），UTF-8 **BOM**
- 生成即闸：lupa 编译校验，语法错抛 `PassiveLuaGenError`（带关节名）阻止落盘；复杂从动（耦合、`matrices` 输出）在生成物上手工扩写，重存时若会话内无表达式则不覆盖已有脚本

## 生成与落地分离

`emit_toml` / `emit_passives_lua` 只产文本不落盘 → `validate_assets` 按固定顺序跑机器闸（`toml_ascii` → `linkage` → `lua_compile`）→ 失败产出结构化 `GateIssue`（闸名/原因/定位）喂 llm_fix 或 human_gate；**人审通过后才落盘**。pulse 标定（`xtal-pulse-calibrate`）与复杂从动扩写是下游人工环节，编排阶段不代做。
