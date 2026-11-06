---
name: device-overview
description: >-
  设备共性：TOML + Lua + BP mesh + Zenoh type + Solver 五件套，以及新设备接入清单。
  问设备怎么拼起来、该进哪个设备技能、或接入新机型时使用。
---

# 设备共性

一台设备 = 五件套，断一环则包到了模型不动：

```
TOML（[[joints]] + script_path）
  ↔ BP（StaticMesh 名 = 关节名；DynamicType = Zenoh key 的 type）
  ↔ Lua resolve_passives
  ↔ Zenoh …/simulation/{type}/** → DtbMotorHandler
  ↔ FindSolverBySceneType
```


## 运动学解读原则（编排侧）

- **宁多勿漏**：资料/结构上能合理推出的运动都建 joint 存 TOML——例：A2 单旋转模组爆炸图有旋转电机、工艺动作表没列旋转，照样建 Revolute joint。实际用不用由人工在下游定，编排阶段不擅自删减。
- **不参编**：粉末瓶、对心工装/载具等是**被操作对象**（耗材/定位件），运动来自机构对它们的夹持搬运，自身无自由度——不建 joint、不进 TOML/Lua，编排结论中出现直接剔除。这是"宁多勿漏"的类别性例外（那条针对机构件）。CAP8A 实测：模型把 4 个粉末瓶 + 3 个对心工装报成"径向平移"，全部成了多出件进人审。
- **joint 不看装配层级**：运动学要动的件都建 joint，手指座是夹爪子级也建；`origin_matrix_local` 与 `joint_axis` 均为**相对父级**的局部量（不是世界轴）。
- **同名同构取第 1 主动**：同 parent 下同构一组（如 `手指座_A.1~.4`），第 1 个当主动，其余 1:1 镜像从动，Lua `follow` 指向**同组第 1 个**（不是 parent）。

