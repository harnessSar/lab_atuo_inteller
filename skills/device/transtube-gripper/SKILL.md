---
name: transtube-gripper
description: >-
  TRANStube 机械臂夹爪逆耦合：转腕编码器与手指开合、gripper_roz_pulse_ratio、
  手指呼吸。转腕时夹爪跟着动、或新增同类耦合夹爪时使用。数字以 TRANStube.toml 为准。
---

# TRANStube（机械臂 / 业务 701）

业务身份是 `701`，运动学 type 是 `TRANStube`（不是 `TRANS`）。夹爪经验与 701 的 π/2 偏置是两件事：偏置走规则 `310`，本技能只管夹爪。

## 逆耦合

真机在耦合空间发编码器：转腕会带动手指编码器，逻辑开合应几乎不变。UE 关节树已解耦（转腕不带动丝杆），所以 Lua 里要减掉转腕分量再写成开合 mm：

```
logical_pulse = enc_pulse − roz_pulse * ratio
open_mm = (logical_pulse − finger_zero) / finger_ppu
手指连杆 = open；各手指杆 = ±open × 连杆比
```

## 经验

1. **禁止把手指编码器 1:1 当开合。** 只动转腕、开合指令不变时，若 `手指连杆` 还在走，就是逆耦合没加载或 params 没注入。
2. **`[kinematics.params]` 与关节 `pulse_*` 必须同改：** `roz_pulse_zero` / `roz_pulses_per_unit`、`finger_pulse_zero` / `finger_pulses_per_unit`、`gripper_roz_pulse_ratio`。
3. 下一帧 `J["手指连杆"]` 已是上回写的 open_mm。脚本用缓存区分「新编码器」和「上回写」。不要单独清缓存。
4. 平面臂从动与夹爪公式在同一 `TRANStube_passives.lua`。改臂时别删 `open_mm` 块。
5. 指杆杠杆比只在 Lua。改开合标定不要先改杠杆比。

文件：`PID/Content/Script/kin/TRANStube_passives.lua`、`PID/Content/kin/TRANStube.toml`。公式对照在 `.cursor/skills/xtal-device/transtube-gripper/reference.md`。
