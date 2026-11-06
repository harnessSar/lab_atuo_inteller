---
name: xtal-scene-route
description: >-
  业务场景 load/clear 与路由：DynamicType/DynamicUuid、701 根与槽位、Route miss。
  场景树不对、子设备清不掉、或脉冲身份对不上 Actor 时使用。
---

# 场景路由

寻址按 `DynamicType` + `DynamicUuid` **精确**匹配（大小写不敏感）。发布的 type/id 必须与 spawn 时写入的字段一致。

- spawn：JSON 业务数字（如 `"701"`）经 `ActorTypeNameMap` 变成设备名，写入 `DynamicModuleName`；未命中则沿用原 type。`DynamicType` 保持 JSON 原始 type，`DynamicUuid = scene.id`。蓝图类走 `ActorTypeClassMap`。
- clear 必须递归：`load` 只登记根；子设备是 Attach。清空时 `GetAttachedActors(..., true)` 再 Destroy。
- 业务转换在内存：701 `role=rail` 升为根（工站坐标 cm）；其余按序挂 `u1/u2/…`；`with_cap=true` 追加盖子。工站 `station_x/y/z` 在 `XtalConfig`（cm）。
- 活动资源树从本次 spawn 拷贝。`add`/`drop` 搬节点并改 Actor 父子，快照写 `PID/scene_runtime.json`，不回写 `scene.json`。

Route miss：对比 payload 的 type/id 与 Actor 字段。701 不用运动学类名当路由键（规则 `310`）。DTB 的 `CAP_1` 拆成 DynamicType=`CAP`、DynamicUuid=`CAP_1`。

关节已命中但不动 → `xtal-kin-solver`。
