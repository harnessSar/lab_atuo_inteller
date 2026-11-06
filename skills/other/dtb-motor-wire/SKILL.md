---
name: dtb-motor-wire
description: >-
  DtbMotor.pulse 线格式：proto double、UE shim、打包 libprotobuf.dll。
  发送端脉冲在涨但 UE 里 pulse 恒为 0，或打包找不到 protobuf DLL 时使用。
---

# pulse 线格式

schema：`double pulse = 2;`（`publish_DT/digital_twin_bus.proto` 与仓库 pb schema 一致）。double 的 wire type 是 fixed64；生成代码若是 int64，解码会丢字段，pulse 恒 0。

UE 三处同为 double：生成的 `DtbMotor.pulse()`、shim `motor_pulse`、`FDtbMotor::Pulse`。解码只在 `DigitalTwinBusShim`（独立 CRT）。含 `CoreMinimal` 的翻译单元里不要直接 new protobuf 消息。日志用 `%.3f`。

改 proto 后同时重生 Python pb2 与 UE `pb.h`/`pb.inc`，全量编 XtalZenoh，重启发布端。

打包：Windows 只从 exe 同目录加载 `libprotobuf.dll`、`libprotobuf-lite.dll`、`abseil_dll.dll`。源在 `XtalZenoh/Source/ThirdParty/Protobuf/bin/Win64/`。不改 ThirdParty 里的 protobuf 源码。

Handler 已打出非 0 但仍不动 → `xtal-kin-solver`。
