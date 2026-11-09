---
name: blender-kinpad-unreal
description: >-
  三端产物总图（使用侧）：Blender 原型（m）→ KinPad 生产（mm/rad）→ Unreal 落地（cm/左手）。
  读三端产物、编排新设备查单位与契约时使用；改 FK/IK、三端同步等工程修改走开发侧技能。
---

# Blender → KinPad → Unreal（使用侧）

三段同一套运动学，单位只在边界换一次。本技能是**解读/编排资产**的常识；工程修改（改 FK/IK、三端同步、插件接入）不在此范围，转开发侧技能。

| 段 | 产物 | 单位 |
|----|------|------|
| **Blender** | `kin_pad/save/` 原型脚本、场景导出 JSON | 场景 **m**；导出/wire **mm**（×1000） |
| **KinPad** | `save/{model}.toml`、`{type}_passives.lua` | **mm / rad / 右手** |
| **Unreal** | `PID/Content/kin/*.toml` + `Content/Script/kin/*.lua` + BP mesh | 显示 **cm / deg / 左手**（换算只在工程落地处做一次，编排产物不要自己换算） |

## 从动公式只有一份 Lua

`{type}_passives.lua` 是被动关节 SSOT：KinPad 与 UE 加载同一文件，所以表达式必须是宿主无关的原生 Lua；TOML 只回填 `script_path`，不落 `passive_expr`。线性从动（镜像 follow）是生成物勿手改；复杂从动（耦合、`matrices`）在生成物上手工扩写，重存时若会话内无表达式则不覆盖。

## 设备档案在哪读

`kin_pad/save/`：`{model}.py` + `{model}_kinematics_plugin.py` + `{model}_kinematics.md` + `.toml`——新机型编排时的参考资料包（原型 / 运动学文档 / golden 候选）。
