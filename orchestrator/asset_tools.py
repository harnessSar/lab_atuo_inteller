"""运动学资产编排工具面（两步编排链接 ReAct 环，纯本地、无 UE、无模型调用）。

把 POC5 探针链（``_work/probe_poc5*.py``）固化成 4 个 ReAct @tool：

| 工具 | 对应探针段 | 职责 |
|---|---|---|
| ``asset_catalog`` | POC5 物体清单注入 | 读 Blender 场景导出 → ``asset_run/scene.json`` + 紧凑清单 |
| ``asset_land`` | ``land_semantics`` 段 | 语义结论 → JointSpec（轴判定/主从分组）→ ``asset_run/spec.json`` |
| ``asset_emit`` | emit + validate 段 | spec → TOML/Lua 草稿 + 机器闸 → ``asset_run/draft.*`` |
| ``asset_score`` | POC5b 评分段 | 草稿对 golden 评分（进产物/type/axis/passive/lua） |

跨工具状态全经 ``work_dir/asset_run/`` 磁盘句柄（scene.json → spec.json →
draft.toml / draft.lua / gate_report.json → score.json）。草稿**不落业务目录**——
生成与校验分离，人审通过后才合入（emit.py 契约）。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tomllib
from pathlib import Path

# kin_pad/src 进 sys.path（与 nl_graph.py 同约定，禁硬编码业务路径）
_KIN_PAD_SRC = Path(__file__).resolve().parents[2] / "kin_pad" / "src"
if _KIN_PAD_SRC.is_dir() and str(_KIN_PAD_SRC) not in sys.path:
    sys.path.insert(0, str(_KIN_PAD_SRC))

from langchain_core.tools import tool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from kin_pad.mcp.asset import emit, semantic_land, validate  # noqa: E402
from kin_pad.mcp.asset.spec import DeviceSpec  # noqa: E402

logger = logging.getLogger("lab_atuo_inteller.orchestrator.asset_tools")

_FAIL = "执行失败："  # Observation 失败前缀（与 nl_graph 同约定，模型据此前缀自纠）


# ---------------------------------------------------------------------------
# 参数 schema（Field description = 参数说明）
# ---------------------------------------------------------------------------


class _CatalogArgs(BaseModel):
    scene_path: str = Field(
        description="Blender 场景导出 JSON 绝对路径（物体数组，每元素含 name/parent/matrix_local）")


class _ConclusionItem(BaseModel):
    part: str = Field(description="部件名，用 asset_catalog 清单里的名字；每件只提交一次")
    motion: str = Field(description="该件自身的自由度：沿轴滑动=平移（丝杠/手指座/底座滑轨都是平移），"
                                    "绕轴转动=旋转（拧盖夹爪本体）。模组名带'旋转'≠其零件都旋转")
    direction: str = Field(description="方向词：上下（竖直升降）/ 前后 / 左右 / "
                                       "径向（沿半径向心或离心滑动）/ 绕竖直（绕竖直轴旋转）")
    note: str = Field(default="", description="可选补充（如 '16个同构，1主动带15镜像'）")


class _LandArgs(BaseModel):
    conclusions: list[_ConclusionItem] = Field(
        description="全部语义结论一次性提交（宁多勿漏：子级件也列；纯框架/瓶/工装等静止件不列）")
    device_key: str = Field(default="device", description="设备 key（ASCII），默认 device")
    device_name: str = Field(default="DEVICE", description="设备名（ASCII），被动 Lua 命名取它的小写")


class _EmitArgs(BaseModel):
    script_name: str = Field(default="", description="被动 Lua 脚本名，可选；"
                                                     "默认 {设备名小写}_passives.lua")


class _ScoreArgs(BaseModel):
    golden_toml: str = Field(description="golden TOML 绝对路径（人审定稿的关节描述）")
    golden_lua: str = Field(default="", description="golden 被动 Lua 绝对路径，可选；给了才评 lua 项")


# ---------------------------------------------------------------------------
# 评分辅助（与 _work/probe_poc5b_reland.py 同口径：golden 轴不带符号，只比字母）
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\.(\d+)$", r"_\1", s)


# ---------------------------------------------------------------------------
# 工具工厂
# ---------------------------------------------------------------------------


def make_asset_tools(work_dir: Path) -> list:
    """工厂：注入证据/草稿工作区，产出 4 个资产编排 @tool（闭包捕获，图编译后不可变）。"""

    def _run_dir() -> Path:
        rd = work_dir / "asset_run"
        rd.mkdir(parents=True, exist_ok=True)
        return rd

    @tool(args_schema=_CatalogArgs)
    def asset_catalog(scene_path: str) -> str:
        """读 Blender 场景导出 JSON，返回物体清单（name/parent 装配父子）。
        编排运动学资产的第一步：先看清场景里有哪些件、装配层级如何。"""
        p = Path(scene_path)
        if not p.is_file():
            return f"{_FAIL}场景文件不存在: {scene_path}"
        try:
            items = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return f"{_FAIL}场景文件解析失败: {exc}"
        if not isinstance(items, list) or not all(isinstance(o, dict) and "name" in o for o in items):
            return f"{_FAIL}场景文件应为物体数组（每元素含 name/parent/matrix_local）"
        rd = _run_dir()
        (rd / "scene.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        lines = [f"{o['name']}  ←  {o.get('parent') or '(根)'}" for o in items]
        out = (f"场景 {p.name}：{len(items)} 个物体（已存 {rd / 'scene.json'}）。"
               f"清单（件 ← 父级）：\n" + "\n".join(lines))
        if len(out) > 4000:  # 上下文保护：超长截断，完整件在 scene.json
            out = out[:4000] + f"\n…[清单截断，完整 {len(items)} 件见 scene.json]"
        return out

    @tool(args_schema=_LandArgs)
    def asset_land(conclusions: list[dict], device_key: str = "device",
                   device_name: str = "DEVICE") -> str:
        """语义结论一次性落地成关节（自动轴判定 + 主从镜像分组，全工具无模型）。
        返回关节名册与人审提示；人审提示（映射失败/轴退化）必须原样报告用户。"""
        scene_file = _run_dir() / "scene.json"
        if not scene_file.is_file():
            return f"{_FAIL}尚未 asset_catalog：先读场景再落地"
        items = json.loads(scene_file.read_text(encoding="utf-8"))
        concl = [c if isinstance(c, dict) else c.model_dump() for c in conclusions]
        try:
            joints, notes = semantic_land.land_semantics(concl, items)
            spec = DeviceSpec(key=device_key, model_id=0, name=device_name)
            data = spec.model_dump()
            data["joints"] = [j.model_dump() for j in joints]
            spec = DeviceSpec.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - spec/落地校验失败，回喂自纠
            return f"{_FAIL}落地校验失败: {str(exc)[:400]}"
        (_run_dir() / "spec.json").write_text(spec.model_dump_json(indent=1), encoding="utf-8")
        n_rev = sum(1 for j in joints if j.joint_type == "Revolute")
        n_pas = sum(1 for j in joints if j.is_passive)
        # obs 瘦身（16K ctx 红线）：名册不落 Observation——验证走 asset_emit/asset_score，
        # 完整关节数据在 spec.json；这里只留计数 + 人审提示（人审提示是正式产物，必须全量）
        out = (f"落地 {len(joints)} 关节（Revolute {n_rev} / Prismatic {len(joints) - n_rev}，"
               f"被动 {n_pas}）→ spec.json。"
               f"若计数与预期明显不符，说明结论分类有误，修正后重新 asset_land 覆盖。")
        if notes:
            out += f"\n⚠ 人审提示 {len(notes)} 条（必须报告用户）：\n" + "\n".join(f"  ! {n}" for n in notes)
        return out

    @tool(args_schema=_EmitArgs)
    def asset_emit(script_name: str = "") -> str:
        """落地关节 → TOML/Lua 草稿 + 机器闸（toml_ascii/linkage/lua_compile）。
        草稿写 asset_run/ 不落业务盘；闸失败返回结构化问题清单供修结论后重落地。"""
        spec_file = _run_dir() / "spec.json"
        if not spec_file.is_file():
            return f"{_FAIL}尚未 asset_land：先落地关节再生成"
        spec = DeviceSpec.model_validate(json.loads(spec_file.read_text(encoding="utf-8")))
        script = script_name or emit.default_script_name(spec)
        try:
            toml_text = emit.emit_toml(spec, script_name=script)
            lua_text = emit.emit_passives_lua(spec)
        except Exception as exc:  # noqa: BLE001 - lupa 编译闸等，回喂自纠
            return f"{_FAIL}生成失败: {str(exc)[:400]}"
        report = validate.run_all_gates(spec, toml_text)
        rd = _run_dir()
        (rd / "draft.toml").write_text(toml_text, encoding="utf-8")
        if lua_text:
            (rd / "draft.lua").write_text(lua_text, encoding="utf-8")
        (rd / "gate_report.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        out = (f"草稿已生成（不落业务盘，待人审）：{rd / 'draft.toml'}"
               + (f"、{rd / 'draft.lua'}" if lua_text else "")
               + f"\n机器闸：{'全过 ✅' if report.ok else f'{len(report.issues)} 个问题 ❌'}")
        for i in report.issues[:10]:
            out += f"\n  [{i.gate}] {i.location}: {i.message[:120]}"
        if len(report.issues) > 10:
            out += f"\n  …其余 {len(report.issues) - 10} 条见 gate_report.json"
        return out

    @tool(args_schema=_ScoreArgs)
    def asset_score(golden_toml: str, golden_lua: str = "") -> str:
        """当前 spec 对 golden 评分：进产物/type/axis/passive/lua + 失败明细 + 多出件。
        口径：关节名 .N→_N 归一；golden 轴不带符号，只比字母；多出件走人工定夺。"""
        spec_file = _run_dir() / "spec.json"
        if not spec_file.is_file():
            return f"{_FAIL}尚未 asset_land：先落地关节再评分"
        gt_path = Path(golden_toml)
        if not gt_path.is_file():
            return f"{_FAIL}golden TOML 不存在: {golden_toml}"
        spec = DeviceSpec.model_validate(json.loads(spec_file.read_text(encoding="utf-8")))
        out_joints = {j["name"]: j for j in
                      tomllib.loads(emit.emit_toml(spec, script_name=emit.default_script_name(spec)))["joints"]}
        with gt_path.open("rb") as fh:
            gt_joints = tomllib.load(fh)["joints"]

        n_in = n_type = n_axis = n_pass = 0
        fails: list[str] = []
        for j in gt_joints:
            o = out_joints.get(_norm(j["name"]))
            if o is None:
                fails.append(f"{j['name']}: 未进产物")
                continue
            n_in += 1
            if o["joint_type"] == j["joint_type"]:
                n_type += 1
            else:
                fails.append(f"{_norm(j['name'])}: type {o['joint_type']}!={j['joint_type']}")
            if o["joint_axis"].lstrip("-") == j["joint_axis"]:
                n_axis += 1
            else:
                fails.append(f"{_norm(j['name'])}: axis {o['joint_axis']}!={j['joint_axis']}")
            if o["is_passive"] == j["is_passive"]:
                n_pass += 1
            else:
                fails.append(f"{_norm(j['name'])}: passive {o['is_passive']}!={j['is_passive']}")

        lua_line = "lua 未评（未给 golden_lua）"
        if golden_lua:
            gl = Path(golden_lua)
            if not gl.is_file():
                return f"{_FAIL}golden Lua 不存在: {golden_lua}"
            lua_gt = {_norm(k): _norm(v) for k, v in re.findall(
                r'\["([^"]+)"\]\s*=\s*\(J\["([^"]+)"\]\)', gl.read_text(encoding="utf-8"))}
            lua_text = emit.emit_passives_lua(spec)
            lua_out = dict(re.findall(r'\["([^"]+)"\]\s*=\s*\(?J\["([^"]+)"\]\)?', lua_text))
            n_lua = sum(1 for k, v in lua_gt.items() if lua_out.get(k) == v)
            lua_line = f"lua {n_lua}/{len(lua_gt)}"

        n = len(gt_joints)
        extra = sorted(set(out_joints) - {_norm(j["name"]) for j in gt_joints})
        res = (f"评分（golden {n}）：进产物 {n_in}/{n}  type {n_type}/{n}  "
               f"axis {n_axis}/{n}  passive {n_pass}/{n}  {lua_line}\n"
               f"失败明细 {len(fails)} 条：")
        res += "\n" + "\n".join(f"  - {f}" for f in fails[:15]) if fails else " 无"
        if len(fails) > 15:
            res += f"\n  …其余 {len(fails) - 15} 条从略"
        res += f"\n多出件（人工定夺）{len(extra)} 个"
        if extra:
            res += "：" + "、".join(extra[:10]) + ("…" if len(extra) > 10 else "")
        (_run_dir() / "score.json").write_text(json.dumps({
            "in": n_in, "type": n_type, "axis": n_axis, "passive": n_pass,
            "total": n, "fails": fails, "extra": extra}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return res

    return [asset_catalog, asset_land, asset_emit, asset_score]
