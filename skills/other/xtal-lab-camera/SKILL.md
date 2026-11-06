---
name: xtal-lab-camera
description: >-
  实验室观察相机：视口跟 Pawn、RMB 转向、LMB/MMB 平移、滚轮 dolly。
  改 Pawn 旋转画面不转、或转向轴拧时使用。
---

# 观察相机

GameMode：`PID/Config/DefaultEngine.ini` → `/XtalScene/GameMode/MyXtalGameMode`。源码在 `XtalScene/.../GameMode/XtalPawn.*`。

1. `CameraComponent->bUsePawnControlRotation` 必须为 **false**。为 true 时只跟 Controller：改 Location 有效、改 Actor Rotation 无效。
2. 转向用世界 Z 偏航 + 相机本地俯仰，再 `NormalizeAxis` 钳 Pitch。不要直接累加欧拉角。
3. 蓝图子类会盖掉 C++ 默认。`XtalPawn` 蓝图里取消 Use Pawn Control Rotation，Controller 的 Pitch/Yaw/Roll 全关。
4. 初始朝向用 `SetActorRotation`。

操作：RMB 转向；LMB/MMB 视平面平移；滚轮沿前向 dolly（默认约 24 cm/档）；WASD/QE 飞控，Shift 加速。左键留给点选时关掉 `bLeftMouseDragPans`。

只改位姿字符串走 `xtal-config-io`。
