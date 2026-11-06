---
name: dtb-module-event
description: >-
  DtbModuleEvent：topic 上的 type/module_id、字符串 event_id、add/drop 资源树、
  TRANStube/CAP/NEODP 一次性 bat。新增事件或蓝图收不到时使用。
---

# 模块事件

key：`{domain}/{route}/{type}/{module_id}/event/action/digital_twin_bus.DtbModuleEvent`。type 在 key 上，不在 proto。`CAP_1` 拆成 DynamicType=`CAP`。后缀必须是 `DtbModuleEvent`，不要拿 Event 字节去解 `DtbPackage`。

`event_id` 是字符串 `{槽位}_{add|drop}`（如 `claw_U_o_add`）。设备由 topic type 决定。新设备只改 `PID/Config/event_config.json` 的 `devices.<DynamicType>.slots`，不要加 C++ 分支。`id` 对齐 CAD 组件名 / Tag / 网格名。

资源树在 C++：`add` 把 tar 挂到当前设备槽位再 `OnEventAdd`；`drop` 挂到 place 解析出的架位再 `OnEventDrop`。成功后写 `PID/scene_runtime.json`，不改 `scene.json`。同 type 多台时 tar 要写 uuid。蓝图实现 On Event Add/Drop；分发走 C++ `_Implementation`（同 `zenoh-dtb-handler`）。

线格式 field 2 是 length-delimited string。发布用 `publish_DT/dtb_event_wire.py`，不要 `event_id = int`。Windows bat 里不要放带 `&` 的 rsrc。事件默认发 1 次退出。打包后 `event_config.json` 放到 `Config/`。

现网 type：`TRANStube`、`CAP`。NEODP 要先在 `event_config.json` 加槽位。
