---
name: zenoh-dtb-handler
description: >-
  DtbStepState 分发：BlueprintNativeEvent 未 Call Parent 导致 C++ 不跑、no handler claimed。
  日志有 StepState 但 OnStepStateExecute 收不到时使用。
---

# StepState 认领

`TryHandlePayload` 是 BlueprintNativeEvent。`Execute_TryHandlePayload` 若找到蓝图函数就走 ProcessEvent：图表没实现、或没 **Call Parent**，返回值默认 false，C++ `_Implementation` 不跑，于是 `no handler claimed`，也没有 `DtbStepStateHandler: recv`。

现行分发直接调 C++：

```cpp
bHandled = Handler->TryHandlePayload_Implementation(KeyExpr, MessageBytes);
```

蓝图侧：用 Handler 的 **BP 子类**，覆盖 **On Step State Execute**，不要覆盖 Try Handle Payload。RoutingRules 的 HandlerClass 指向该子类，并与关卡组件类型一致。key 后缀是 `digital_twin_bus.DtbStepState`。

订了但没有任何 sample → `zenoh-bridge-route`。事件 bat → `dtb-module-event`。
