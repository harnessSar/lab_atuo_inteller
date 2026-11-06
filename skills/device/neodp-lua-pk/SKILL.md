---
name: neodp-lua-pk
description: >-
  NEODP 舀粉：j0 伸出与 j5 自旋（均为 rad）、spoon 绝对矩阵、生产 PK 与 oring.py 对齐，
  以及实验室原型三端同步。改 NEODP.toml/Lua、spoon 位姿、或同类双摇臂时使用。
---

# NEODP（舀粉）

生产工位与实验室原型是两条线。改工位标定/overlay 走本节；改 FK/IK 默认几何走三端同步，SSOT 是规则 `300`，不要把实验室杆长抄进生产 TOML。

## 生产链

```
q = (pulse − pulse_zero) / pulses_per_unit
j0 = 伸出差动角 rad（原 j6，电机 id 必须是 j0）
j5 = 勺子自旋 rad
j0_pk = j0 + j5          # 解耦：伸长不跟 j5 走
pk → spoon 绝对 4×4（mm，RH）→ ParseMatrixLocal，不复合 TOML origin
```

PK 与 `NEODP_oring.py` 同公式。Unreal 专用修正只在 Lua overlay（`apply_spoon_for_unreal`）。`j0_l` 由伸出角、螺距和零位长算出。生产零位以 `NEODP_kinematics.lua` / `NEODP.toml` 为准，不要用规则 `300` 的实验室 `j0_l_base` 覆盖。

## 经验

1. j5 只进自旋，不改变伸出。大倾角 Details 欧拉不能当 j5 判据。
2. 虚拟轴 `is_virtual=true`：进脉冲与 Lua，不建 mesh。关旋转用 Lua `j5=0`，不要把 ppu 写成 0。
3. 万向节 delta 须 `wrap_pi`（瓶口/折叠位 atan2 会跨约 2π）。
4. 两点标定按 Motor.id 配对（曾把 j5 脉冲标到 j1）。
5. 位置差调 `spoon_ue_loc_off_*_cm`；约 180° 转差调 `spoon_ue_yaw_offset_deg`。先不要改 PK。
6. 编辑器对、打包歪：先走 `xtal-lua-host`（FName / UFS），不要先改 PK。

## 实验室三端（改公式/默认几何）

Blender `kin_pad/save/yaofenmokuai.py` → 插件 `yaofenmokuai_kinematics_plugin.py` → UE 演示 `LambdaSpool.cpp`（组件名 `j1-demo`，不是生产 `j1`）。清单可能漏文件，改完用 grep 补。更新 `yaofenmokuai_kinematics.md`。FK→IK 闭环后再宣称完成。

演示 Actor 禁止 `CreateDefaultSubobject("j1")` 或 `UPROPERTY J1`。

文件：`PID/Content/kin/NEODP.toml`、`NEODP_oring.py`，`PID/Content/Script/kin/NEODP_kinematics.lua`（与 `kin_pad/save/` 副本同步），探针 `publish_DT/publish_neodp_pulse.py`。
