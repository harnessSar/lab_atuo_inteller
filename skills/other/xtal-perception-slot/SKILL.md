---
name: xtal-perception-slot
description: >-
  感知盒 Slot/Anchor 吸附：s_/a_ 前缀、阈值、AABB 兜底、跨设备交接。
  器皿进盒不吸附、吸错槽、或松开后位置跳时使用。
---

# Slot / Anchor

- 组件名：Slot `s_`，Anchor `a_`。`PreferAnchorType=grip|place` 优先对应 `a_grip` / `a_place`。
- `AcceptableAnchorTypes` 空 = 不限制。默认只扫本 Actor 的 Slot。
- 平移阈值默认约 ±2 **cm**（UE 世界）。不要用 KinPad 的 mm 当阈值。
- overlap 对「Attach 进盒」不保证 `BeginOverlap`。`DetectInsideAnchors` 用 AABB 补登记；已有 overlap 登记保留。
- **不要恢复 `PruneDetectedAnchors`**：它会把 AABB 刚补上的锚点同帧删掉，接收方提前退出，槽位不 Attach。
- 跨设备：释放侧 `BroadcastDetachedAnchor` → 接收侧 `ReceiveDetachedAnchor` → `DetectInsideAnchors` → `Attach`。盒外拒因先加大 `BoxExtent`，不要先动平移阈值。owner 的 `SocketType` 须为 `Slot` 或 `SlotAnchor` 才会进广播列表。

刷虚拟槽走 `hte-brush-slots`，不要手改 SCS。
