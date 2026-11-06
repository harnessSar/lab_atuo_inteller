---
name: publish-dtb-probe
description: >-
  publish_DT 收发探针：subscribe/publish bat、中文关节名预设、压测不改 UE 业务路径。
  验证 router 上有没有包、或 Windows bat 把关节名解成乱码时使用。
---

# publish_DT 探针

`--connect tcp/HOST:7447` 与 `--topic …/digital_twin_bus.DtbPackage` 分开写。禁止把 host:port 拼进 key。

一包一轴。「全轴同时」= 同一时间戳连发 N 次。UE 不为测评合并 Resolve。

Windows `.bat` 不要写中文关节名（cmd 代码页会把 UTF-8 解乱，UE 收到坏 id）。用 `--preset dabang`，映射写在 `publish_dtb.py` 的 UTF-8 源码里。解释器：`kin_pad/.venv` 的 python，脚本加 `-u`。

| 脚本 | 作用 |
|------|------|
| `subscribe_dtb.py` | 订阅；`--decode` 看 Motor id/pulse |
| `publish_dtb.py` | 发布；默认 toml 里全部主动轴 |
| `publish_cap_minmax.py` / `publish_neodp_pulse.py` | CAP / NEODP 扫程 |
| `publish_step_state.py` / `publish_module_event.py` | StepState / 事件（接收诊断不在本技能） |
| `run_bench.bat` | 只测发布端阶梯，不改 PID Handler |

无包时每秒 `still waiting`：核对 Endpoint、轴名段、上游是否在发。Python 能收、UE 不能收 → `zenoh-bridge-route`。pulse 恒 0 → `dtb-motor-wire`。
