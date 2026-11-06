---
name: zenoh-bridge-route
description: >-
  AZenohBridge：Endpoint 与 KeyExpr 分开、订阅 vs 路由、DTB 成功路径静音、GameThread。
  订了 topic 但没数据、或把 host:port 写进 key 时使用。
---

# Bridge 订阅与分发

三字符串不要粘在一起：

| 名 | 例 |
|----|-----|
| Endpoint（session 连谁） | `tcp/10.x.x.x:7447` |
| SubscriberKeyExprs（能否进进程） | `…/simulation/TRANStube/**` |
| live KeyExpr（分发给哪个 Handler） | `…/TRANStube/l1/state/position/digital_twin_bus.DtbPackage` |

`*` 只匹配一段，`**` 匹配多层。关卡只放一个 Bridge、一个 session、多条订阅。

现网 type：`TRANStube` / `CAP` / `NEODP`。历史 key 里的 `TRANS` 只在线上确实是 `TRANS` 时使用。KinPad 关节是另一路 topic（`kin_pad/joints`）。

## 静音与会话

`session state`：`1 Opening → 2 Open` 且 `subscriber declared` = 建连成功。`3 Error` 才是真失败。

`DtbPackage` 不打逐包日志，`UDtbMotorHandler` 成功路径也静音。证据是 watchdog `rx/hit` 或画面。非 DTB（joints、StepState、ModuleEvent）仍会打 `rx`。

分发在 GameThread。禁止为吞吐在 Handler 里同帧合并多轴。C++ 认领走 `TryHandlePayload_Implementation`，见 `zenoh-dtb-handler`。
