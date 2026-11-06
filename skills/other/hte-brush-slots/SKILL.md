---
name: hte-brush-slots
description: >-
  为场景中的 XtalActor/设备蓝图刷 HTE 虚拟槽位（空 StaticMesh，供感知盒吸附）。
  当用户要求"刷槽位 / 增加虚拟槽 / 按 HTE json 配置槽位 / brush slots"时使用。
  唯一通道 = 控制台命令 Hte.BrushSlots（经 control_editor.console_command 执行），
  禁止用 blueprint.edit_scs / add_component 等原生蓝图能力手动拼槽位。
---

# 虚拟槽位刷入（HteVirtualSlotTool 插件）

## 唯一正确通道

经 `control_editor.console_command` 执行插件控制台命令（`consent: none`，直接可调）：

```
Hte.BrushSlots <ActorNameOrLabel> <DeviceKey|ModelId|Name> [JsonPath]
```

- `ActorNameOrLabel`：场景中设备 actor 的 label（如 `BP_121_Capping_8a2`）
- `DeviceKey|ModelId|Name`：HTE json 里设备条目的 key / model_id / name 任一（如 `121`）
- `JsonPath`：可选，HTE 设备 json 绝对路径（正斜杠，如 `D:/project/PID/Docs/HTE.json`）；省略用插件默认路径

效果：把该设备 `slots` 的每个键写成蓝图 SCS 里的**空 StaticMesh 组件**（键名 = 组件名），
并把 JSON 设备字段同步到 XtalActor 及蓝图默认值。重复刷**幂等**（已存在的槽跳过，不计创建）。

## 前置检查（失败主因）

1. **actor 必须在当前编辑器世界**：`control_actor.list` 跨 streaming level 枚举（看得到），
   但 `Hte.BrushSlots` 只遍历当前世界（刷不到）。报"关卡里找不到 Actor"时：
   用 `manage_level.load` 加载目标所在关卡，或如实告知用户切关卡后重试。
2. 设备 key 必须在 json 里存在（key/model_id/name 三匹配任一）。

## 结果复核（必须做）

1. 日志行（`system_control.read_log`，filter `创建`，lines 4）：
   `[Hte.BrushSlots] Actor=.. BP=.. 创建=X 跳过=Y 属性同步=是/否`——创建数应等于 slots 键数（首刷）。
2. SCS 复核：`blueprint.get_scs`（blueprintPath 经 `asset.list` filter 设备名获得），
   组件列表应出现 slots 的每个键名。
3. **命名核对**：slots 键名原样成为组件名（连字符 `LP-1` ≠ 下划线 `LP_1`）；
   若蓝图已有同义不同名的旧组件（如 `LP_1`），新刷 `LP-1` 会并存——如实报告用户判断，不擅自删旧件。

## 清理还原

```
Hte.ClearVirtualSlots <ActorNameOrLabel>
```

只删本工具刷出的虚拟槽（蓝图 SCS + 关卡实例），不动业务组件；属性同步的蓝图默认值不在清理范围。

## 红线

- ❌ 禁止用 `blueprint.edit_scs` / `blueprint.edit_component` / `control_actor.edit_component`
  等原生能力手动添加/拼槽位组件——目标蓝图易选错（2026-11-04 实测误改 `BP_5001_HTE_Reactor`），
  且绕过 HTE 属性同步（`bPropertiesSynced`），产物不完整。
- ❌ 不擅自删除蓝图既有组件（含疑似重复的旧槽位）——清理只走 `Hte.ClearVirtualSlots` 或用户明示。
