---
name: xtal-pack-runtime
description: >-
  打包后 XtalConfig、kin TOML、Script Lua、protobuf DLL 放哪。
  打包体缺 lua/dll、或不想为改配置重 cook 时使用。
---

# 打包后文件

cook 不会自动带走无扩展名 `XtalConfig`，toml/lua 靠 `DefaultGame.ini` Stage 或手拷：

```
+DirectoriesToAlwaysStageAsUFS=(Path="Script")
+DirectoriesToAlwaysStageAsUFS=(Path="kin")
+DirectoriesToAlwaysStageAsNonUFS=(Path="Config")
+DirectoriesToAlwaysStageAsNonUFS=(Path="kin")
```

冲突标记 `<<<<<<<` 会使 PackagingSettings 整段失效。查 lua 走 `IFileManager`（见 `xtal-lua-host`）。

输出根相对位置：`PID.exe` 旁放 `libprotobuf.dll` / `libprotobuf-lite.dll` / `abseil_dll.dll`；`PID/Config/XtalConfig`；`PID/Content/kin`；`PID/Content/Script`。改这三类后重启 exe 即可。`event_config.json` 同样放 `Config/`。

`TemplateObject->IsA(LoadClass)` 崩溃：蓝图短名不要撞引擎类（如 `SceneComponent`）；改 UClass 或重命名后必须全量 recook。编辑器插件不要打进游戏目标。
