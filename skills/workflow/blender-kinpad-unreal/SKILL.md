---
name: blender-kinpad-unreal
description: >-
  三端工作流总图：Blender 原型（m）→ KinPad 生产/插件（mm/rad）→ Unreal 落地（cm/左手）。
  改 FK/IK 或默认几何、新机型从原型进 PID、或问三段各自改哪里时使用。
---

# Blender → KinPad → Unreal

三段同一套运动学，单位只在边界换一次。仓库以 **PID/Unreal 为主**，KinPad 为辅。

| 段 | 放什么 | 单位 | 入口 |
|----|--------|------|------|
| **Blender** | `kin_pad/save/{model}.py` 原型、`blender_addon/` Zenoh 桥 | 场景 **m**；wire **mm** | `matrix_units.py`（`MM_PER_M = 1000`）；矩阵走 `bridge_queue` + timer，禁止非主线程写 bpy |
| **KinPad** | `src/kin_pad/` 生产；`save/{model}_kinematics_plugin.py` 插件；GUI 生成 `{type}_passives.lua` | **mm / rad / 右手** | `src/` 禁止 `import bpy`、禁止硬编码绝对路径。插件契约：`create_kinematics_plugin(config)` → `pk` / `ik` |
| **Unreal** | `PID/Content/kin/*.toml` + `Content/Script/kin/*.lua` + BP mesh + Solver | 显示 **cm / deg / 左手** | 换算只在 `AXtalActor::ParseMatrixLocal` 做一次 |

## 从动公式只有一份 Lua

`{type}_passives.lua` 是被动关节 SSOT：KinPad（lupa）与 UE（UnLua）加载同一文件。GUI 保存时逐字嵌入 + lupa 编译，TOML 只回填 `script_path`。线性从动勿手改；复杂从动（耦合、`matrices`）在生成物上手工扩写，重存时若会话内无表达式则不覆盖。

## 新机型原型目录

`kin_pad/save/`：`{model}.py` + `{model}_kinematics_plugin.py` + `{model}_kinematics.md` + `.toml`。默认几何在脚本、plugin、`LambdaSpool.cpp` 三处同名同值。合入 `src/` 前去掉 bpy、接插件加载器。

## 改几何时

先数学后代码；三端逐处同步并用 grep 补漏；FK→IK 闭环（同一组主动角出去再解回）。舀粉实验室常数走规则 `300`，不要抄进生产 NEODP。资产文本生成与机器闸走 `xtal-asset-gate`；脉冲进画面的 UE 链走 `kin-pipeline`。
