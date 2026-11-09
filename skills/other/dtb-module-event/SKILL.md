---
name: dtb-module-event
description: >-
  DtbModuleEvent：topic 上的 type/module_id、字符串 event_id、add/drop、
  TRANStube/CAP/NEODP 一次性 bat。新增/发布事件时使用；收不到等工程问题转开发侧。
---

# 模块事件（使用侧：怎么发、怎么验）

key：`{domain}/{route}/{type}/{module_id}/event/action/digital_twin_bus.DtbModuleEvent`。type 在 key 上，不在 proto。`CAP_1` 拆成 DynamicType=`CAP`。后缀必须是 `DtbModuleEvent`，不要拿 Event 字节去解 `DtbPackage`。

`event_id` 是字符串 `{槽位}_{add|drop}`（如 `claw_U_o_add`）。设备由 topic type 决定。**新设备只改 `PID/Config/event_config.json` 的 `devices.<DynamicType>.slots`，不要加 C++ 分支**。`id` 对齐 CAD 组件名 / Tag / 网格名。同 type 多台时 tar 要写 uuid。

发布走 `publish_DT/dtb_event_wire.py`：线格式 field 2 是 length-delimited string，`event_id` 不要给 int。Windows bat 里不要放带 `&` 的 rsrc。事件默认发 1 次退出。成功后可观察到 `PID/scene_runtime.json` 更新（`scene.json` 不变）——拿它当验收证据。打包后 `event_config.json` 放到 `Config/`。

现网 type：`TRANStube`、`CAP`。NEODP 要先在 `event_config.json` 加槽位。发了收不到、分发异常是工程侧问题，转开发侧技能，不在本技能范围。
