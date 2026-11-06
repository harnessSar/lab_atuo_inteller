---
name: xtal-config-io
description: >-
  读写 PID/Config/XtalConfig：GetXtalConfig、位姿字符串、工站坐标、EndPoint。
  改相机/工站配置或解析 (X=,Y=,Z=) 时使用。单位是 UE 的 cm / deg。
---

# XtalConfig

文件：`PID/Config/XtalConfig`（打包后可改，不必重编）。API：`XtalComponentUtil` 的 `GetXtalConfig` / `StringToLocation` / `StringToRotation`。

位姿必须是 UE 属性文本，不要手写 CSV：

```
DefaultCameraPosition=(X=804.968,Y=-565.632,Z=177.11)
DefaultCameraRotation=(Pitch=-35.245,Yaw=89.661,Roll=0.0)
```

平移 **cm**，旋转 **deg**。`#` 与 `;` 是注释；同名 key 取最后一行；key 大小写不敏感。按第一个 `=` 切开。

常用 key：`EndPoint`、`get_all_resources_url`、`station_x/y/z`、`http_timeout_sec`、各机位相机、`UseJsonConfig`。工站坐标是 **cm**，不要写成 mm。

`AZenohBridge::Connect` 先从本文件读 `EndPoint`，再 `OpenSession`。要走文件须清空蓝图里的 `SessionConfig.Endpoint`。订阅 key 与 Endpoint 是两件事，见 `zenoh-bridge-route`。
