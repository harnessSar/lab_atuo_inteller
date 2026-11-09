---
name: neodp-lua-pk
description: >-
  NEODP 舀粉（使用侧）：j0 伸出与 j5 自旋（均为 rad）、spoon 绝对矩阵、
  产物语义与标定经验。编排/标定 NEODP 的 TOML/Lua、spoon 位姿、或同类双摇臂时使用；
  改公式/默认几何是工程修改，转开发侧。
---

# NEODP（舀粉）

生产工位与实验室原型是两条线。**产物真值以磁盘 `NEODP.toml` / `NEODP_kinematics.lua` 为准**，不要把实验室杆长（规则 `300` 的 `j0_l_base` 等）抄进生产。改公式/默认几何是工程修改，转开发侧技能，不在本技能范围。

## 生产链（编排时必须保的语义）

```
q = (pulse − pulse_zero) / pulses_per_unit
j0 = 伸出差动角 rad（原 j6，电机 id 必须是 j0）
j5 = 勺子自旋 rad
j0_pk = j0 + j5          # 解耦：伸长不跟 j5 走
pk → spoon 绝对 4×4（mm，RH）→ 工程侧落地，不复合 TOML origin
```

## 经验

1. j5 只进自旋，不改变伸出。大倾角 Details 欧拉不能当 j5 判据。
2. 虚拟轴 `is_virtual=true`：进脉冲与 Lua，不建 mesh。关旋转用 Lua `j5=0`，**不要把 ppu 写成 0**（整帧脉冲失败）。
3. 万向节 delta 须 `wrap_pi`（瓶口/折叠位 atan2 会跨约 2π）。
4. 两点标定按 Motor.id 配对（曾把 j5 脉冲标到 j1）。
5. 位置差调 `spoon_ue_loc_off_*_cm`；约 180° 转差调 `spoon_ue_yaw_offset_deg`。先不要改 PK。
6. 编辑器对、打包歪：是工程侧加载问题（FName/打包路径），转开发侧排查，不要先改 PK。

## 参考文件（只读）

`PID/Content/kin/NEODP.toml`、`PID/Content/Script/kin/NEODP_kinematics.lua`、`PID/Content/kin/NEODP_oring.py`（PK 同公式参照）；探针 `publish_DT/publish_neodp_pulse.py`。
