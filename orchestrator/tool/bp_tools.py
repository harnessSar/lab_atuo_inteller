"""BP 装配工具面（EngineWire 调用 + 机器闸纯函数，@tool 与流水线节点共用实现）。

职责：按 HTE 设备档案给 UE 蓝图（XtalActor）刷属性与虚拟槽位的全部机器侧能力。

| 纯函数（不碰 UE）                        | UE 包装（注入 client，返回 (payload, err)） |
|-----------------------------------------|--------------------------------------------|
| ``resolve_hte_json`` 档案路径解析        | ``ue_actor_exists`` actor 在当前世界？      |
| ``brush_slots_cmd`` / ``clear_slots_cmd``| ``ue_scs_components`` 读 BP SCS 组件名     |
| ``parse_brush_log`` 刷入日志行解析       | ``ue_console`` 控制台命令（刷/清唯一通道）  |
| ``diff_slot_names`` 槽键三方 diff        | ``ue_read_log`` 读 Output Log              |
| ``toml_joint_names`` 非虚拟关节抽取      |                                            |
| ``slot_topology`` 槽键拓扑分组           |                                            |
| ``propose_semantics`` Socket/Anchor 提案 |                                            |

红线：
- 刷槽位**唯一通道** = 控制台命令 ``Hte.BrushSlots``（skill `hte-brush-slots`；
  禁止用 blueprint.edit_scs / add_component 等原生能力手拼槽位——绕过属性同步）；
- 属性/槽位提案（SocketType/AnchorType）只进证据，**本层不写 UE**（落地唯一通道是
  BrushSlots 的属性同步；语义映射查不到 → 人审提示，绝不瞎猜）；
- EngineWire 能力 id 唯一事实来源 = skill `hte-brush-slots`，改名只动本文件常量。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tomllib
from pathlib import Path

# kin_pad/src 进 sys.path（禁硬编码业务路径）。本文件在 orchestrator/tool/，parents[3] = 仓库根
_KIN_PAD_SRC = Path(__file__).resolve().parents[3] / "kin_pad" / "src"
if _KIN_PAD_SRC.is_dir() and str(_KIN_PAD_SRC) not in sys.path:
    sys.path.insert(0, str(_KIN_PAD_SRC))

from langchain_core.tools import tool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from kin_pad.mcp.asset.parsing import ParseError, load_hte_devices, parse_hte_device  # noqa: E402

logger = logging.getLogger("lab_atuo_inteller.orchestrator.tool.bp_tools")

_FAIL = "执行失败："  # Observation 失败前缀（与 nl_graph/asset_tools 同约定）

# ---------------------------------------------------------------------------
# EngineWire 网关与能力 id（唯一事实来源 = skills/other/hte-brush-slots/SKILL.md）
# ---------------------------------------------------------------------------
GATEWAY_TOOL = "unreal"  # EngineWire 对端网关工具名（tools/list 唯一暴露）

CAP_CONSOLE = "control_editor.console_command"  # 控制台命令（Hte.BrushSlots 唯一通道，consent: none）
CAP_READ_LOG = "system_control.read_log"        # 读 Output Log（刷入结果复核：filter="创建"）
CAP_GET_SCS = "blueprint.get_scs"               # 读蓝图 SCS 组件清单（参数 blueprintPath）
CAP_ASSET_LIST = "asset.list"                   # 内容浏览器资产检索（参数 filter，定位 BP 路径）
CAP_ACTOR_LIST = "control_actor.list"           # 关卡 Actor 枚举（跨 streaming level；刷只刷当前世界）

# ---------------------------------------------------------------------------
# 语义映射表（业务字典，人可调；查不到 → 人审提示，不猜——map_semantics 节点契约）
# ---------------------------------------------------------------------------
SOCKET_BY_MODEL_TYPE = {
    "tray": "SlotAnchor",      # 托盘：可承托试管，也可被机械臂搬运（Socket 语义见 XtalActor.h）
    "consumable": "Anchor",    # 耗材（试管/瓶）：被装载物
    # "device" 故意不映射：夹持方(Slot)还是工位(SlotAnchor)需人工判断，提案留空 + 人审提示
}
ANCHOR_BY_MODEL_TYPE = {
    "consumable": "vessel",    # valid_model_id 指向耗材 → 槽内容物是试管类
    "tray": "tray",
}


# ---------------------------------------------------------------------------
# 网关底层
# ---------------------------------------------------------------------------

def _execute(client, capability: str, params: dict) -> tuple[dict | None, str]:
    """网关 execute 一次（operation=execute 直发能力名，react.py 同一形态）。

    返回 ``(JSON 正文 dict, err)``；err 非空时 payload 为 None。绝不抛异常——
    传输/契约失败的原文就是自纠与证据的关键材料。
    """
    try:
        resp = client.execute(GATEWAY_TOOL, {"operation": "execute",
                                             "capability": capability, "params": params or {}})
    except Exception as exc:  # noqa: BLE001 —— 客户端自身异常也转成 err 进证据
        return None, f"{capability} 传输异常: {str(exc)[:300]}"
    if not getattr(resp, "ok", False):
        return None, f"{capability}: {getattr(resp, 'error', '')[:300]}"
    raw = getattr(resp, "raw", {}) or {}
    payload = raw.get("content_json")
    if isinstance(payload, dict):
        return payload, ""
    text = raw.get("content_text") or ""
    # EngineWire 回执形态："状态行 + 空行 + JSON 正文"——剥首行再解析
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    try:
        return json.loads(body), ""
    except (json.JSONDecodeError, ValueError):
        return {"text": body}, ""  # 非 JSON 回执（控制台命令常见），原文进证据


def _flatten_strings(obj) -> list[str]:
    """递归收集 JSON 里的全部字符串（actor/asset/SCS 清单的 schema 不做假设）。"""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_flatten_strings(v))
    return out


def _extract_component_names(scs_json) -> list[str]:
    """SCS 回执 → 组件名列表。优先收 dict 的 name 键；纯字符串数组兜底。"""
    names: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k in ("name", "componentName", "Name"):
                v = o.get(k)
                if isinstance(v, str):
                    names.append(v)
                    return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            names.append(o)

    walk(scs_json)
    return names


# ---------------------------------------------------------------------------
# UE 包装（注入 client；返回 (payload, err)，绝不抛异常）
# ---------------------------------------------------------------------------

def ue_actor_exists(client, actor_name: str) -> tuple[bool | None, str]:
    """actor 是否在枚举中。``None`` = 枚举能力不可用（交由 brush 自身报错兜底）。

    注意（skill 前置检查）：control_actor.list 跨 streaming level 枚举「看得到」，
    但 Hte.BrushSlots 只遍历**当前编辑器世界**——枚举命中 ≠ 一定刷得到。
    """
    payload, err = _execute(client, CAP_ACTOR_LIST, {})
    if err:
        return None, err
    names = [s.strip() for s in _flatten_strings(payload)]
    hit = any(s.lower() == actor_name.strip().lower() for s in names)
    return hit, ("枚举命中（仍需在当前世界）" if hit else f"枚举 {len(names)} 个 actor 无此名")


def ue_scs_components(client, bp_name: str) -> tuple[list[str] | None, str]:
    """BP 名 → SCS 组件名列表（asset.list 定位路径 → blueprint.get_scs）。"""
    alist, err = _execute(client, CAP_ASSET_LIST, {"filter": bp_name})
    if err:
        return None, f"asset.list: {err}"
    cands = [s for s in _flatten_strings(alist) if bp_name.lower() in s.lower()]
    if not cands:
        return None, f"asset.list 未找到 BP 资产 {bp_name!r}"
    scs, err = _execute(client, CAP_GET_SCS, {"blueprintPath": cands[0]})
    if err:
        return None, f"blueprint.get_scs: {err}"
    return _extract_component_names(scs), ""


def ue_console(client, command: str) -> tuple[str, str]:
    """执行控制台命令，返回回执文本。"""
    payload, err = _execute(client, CAP_CONSOLE, {"command": command})
    if err:
        return "", err
    return "\n".join(_flatten_strings(payload)), ""


def ue_read_log(client, filter_text: str = "创建", lines: int = 8) -> tuple[str, str]:
    """读 Output Log 末尾若干行（filter 过滤），返回拼接文本。"""
    payload, err = _execute(client, CAP_READ_LOG, {"filter": filter_text, "lines": lines})
    if err:
        return "", err
    return "\n".join(_flatten_strings(payload)), ""


# ---------------------------------------------------------------------------
# 纯函数（不碰 UE，可独立单测）
# ---------------------------------------------------------------------------

def resolve_hte_json(explicit: str, cfg_value: str, repo_root: Path) -> tuple[Path | None, str, str]:
    """HTE 档案路径解析：显式参数 → 配置 [paths] hte_json → ``PID/Docs/HTE.json`` 兜底。

    返回 ``(路径, 来源说明, err)``。兜底命中说明非空（配置值磁盘缺失，不静默）——
    2026-11-10 实测 toml 值 ``PID/HTE设备介绍.json`` 与 C++ 插件默认路径同名，
    磁盘均不存在，故刷入时必须显式携带解析后的路径。
    """
    cands: list[tuple[str, Path]] = []
    if explicit.strip():
        cands.append(("显式参数", Path(explicit.strip())))
    if cfg_value.strip():
        c = Path(cfg_value.strip())
        cands.append(("配置 [paths] hte_json", c if c.is_absolute() else repo_root / c))
    cands.append(("兜底磁盘实测 PID/Docs/HTE.json", repo_root / "PID" / "Docs" / "HTE.json"))
    for src, p in cands:
        if p.is_file():
            note = "" if src == "显式参数" else f"HTE 档案来源：{src}（{p}）"
            return p, note, ""
    tried = "、".join(str(p) for _, p in cands)
    return None, "", (f"HTE 档案 JSON 未找到（试过：{tried}）。"
                      "请显式传 hte_json_path 或修正 sim_harness.toml [paths] hte_json")


def brush_slots_cmd(actor: str, device_key: str, json_path: str = "") -> tuple[str, str]:
    """拼 ``Hte.BrushSlots <Actor> <DeviceKey> [JsonPath]``（正斜杠路径）。

    任何参数含空格即拒绝——控制台按空格分词，静默错解析比报错更糟。
    """
    for label, v in (("actor", actor), ("device_key", device_key)):
        if not v or any(ch.isspace() for ch in v):
            return "", f"{label} 为空或含空格（控制台命令按空格分词，拒绝拼接）: {v!r}"
    jp = (json_path or "").replace("\\", "/")
    if any(ch.isspace() for ch in jp):
        return "", f"json_path 含空格: {jp!r}"
    cmd = f"Hte.BrushSlots {actor} {device_key}"
    if jp:
        cmd += f" {jp}"
    return cmd, ""


def clear_slots_cmd(actor: str) -> tuple[str, str]:
    """拼 ``Hte.ClearVirtualSlots <Actor>``（只删本工具刷出的虚拟槽，不动业务组件）。"""
    if not actor or any(ch.isspace() for ch in actor):
        return "", f"actor 为空或含空格: {actor!r}"
    return f"Hte.ClearVirtualSlots {actor}", ""


_BRUSH_LOG_RE = re.compile(
    r"\[Hte\.BrushSlots\]\s+Actor=(\S+)\s+BP=(\S+)\s+创建=(\d+)\s+跳过=(\d+)\s+属性同步=(是|否)")


def parse_brush_log(text: str) -> dict | None:
    """从 Output Log 文本取**最后一条**刷入结果行（最新一次为准）。"""
    last = None
    for m in _BRUSH_LOG_RE.finditer(text or ""):
        last = m
    if last is None:
        return None
    return {"actor": last.group(1), "bp": last.group(2),
            "created": int(last.group(3)), "skipped": int(last.group(4)),
            "props_synced": last.group(5) == "是"}


def _norm(name: str) -> str:
    """命名归一化（去 -_/空格、小写）——变体冲突检测用，不作匹配依据。"""
    return re.sub(r"[-_\s]+", "", name).lower()


def diff_slot_names(expected: list[str], actual: list[str]) -> tuple[list[str], list[str], list[str]]:
    """槽键三方 diff。返回 ``(missing, pre_existing, conflicts)``。

    - ``missing``：HTE 有、BP 无（首刷将创建）；
    - ``pre_existing``：逐字已存在（将幂等跳过，不计创建）；
    - ``conflicts``：``"LP-1↔LP_1"`` 变体对——同义不同名将并存（skill 红线：
      如实上报人审判断，本层不擅删旧件）。
    """
    actual_set = set(actual)
    missing = [e for e in expected if e not in actual_set]
    pre_existing = [e for e in expected if e in actual_set]
    norm_actual: dict[str, str] = {}
    for a in actual:
        norm_actual.setdefault(_norm(a), a)
    conflicts = [f"{e}↔{norm_actual[_norm(e)]}" for e in missing if _norm(e) in norm_actual]
    return missing, pre_existing, conflicts


def toml_joint_names(toml_path: str) -> tuple[list[str], list[str], str]:
    """运动学 TOML → ``(非虚拟关节名, 虚拟关节名, err)``。

    ``is_virtual=true`` 无 mesh，不参与 mesh↔关节对齐（规则 001）；非虚拟关节名
    必须逐字命中 BP 的 StaticMesh 组件名，否则运行时 ``mesh not found``。
    """
    p = Path(toml_path)
    if not p.is_file():
        return [], [], f"TOML 不存在: {p}"
    try:
        doc = tomllib.loads(p.read_text(encoding="utf-8-sig"))  # utf-8-sig 容忍 BOM
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [], [], f"TOML 解析失败: {exc}"
    joints: list[str] = []
    virtuals: list[str] = []
    for j in doc.get("joints", []) or []:
        name = str(j.get("name", "")).strip()
        if not name:
            continue
        (virtuals if j.get("is_virtual") else joints).append(name)
    return joints, virtuals, ""


_SLOT_KEY_RE = re.compile(r"^([A-Za-z]+)[-_]?(\d+)$")


def slot_topology(slot_names: list[str]) -> dict:
    """槽键 → 拓扑分组提案（``A-1`` → 组 A 序号 1）。**只出拓扑，不出坐标**——
    pitch/origin/轴向不在 HTE 档案里，需标定或人工摆位（propose_layout 节点证据）。"""
    groups: dict[str, list[int]] = {}
    unmatched: list[str] = []
    for name in slot_names:
        m = _SLOT_KEY_RE.match(name)
        if not m:
            unmatched.append(name)
            continue
        groups.setdefault(m.group(1).upper(), []).append(int(m.group(2)))
    for idx in groups.values():
        idx.sort()
    kind = ("sequence" if len(groups) == 1 and not unmatched else
            "grid" if len(groups) > 1 else "mixed")
    return {"kind": kind, "groups": groups, "unmatched": unmatched,
            "note": "仅拓扑分组（序号/行列归属），不含坐标——pitch/origin 需标定或人工摆位"}


def propose_semantics(slots_detail: list[dict], id_type_map: dict[str, str],
                      model_type: str) -> tuple[dict, list[str]]:
    """SocketType / AnchorType 提案（配置查表；查不到 → 人审提示，不猜）。

    ``slots_detail``：``[{"name":..., "valid_model_ids":[int,...]}]``；
    ``id_type_map``：``{str(model_id): model_type}``（全档案解析，fetch 节点构建）。
    提案**只进证据不写 UE**——属性落地唯一通道 = Hte.BrushSlots 的属性同步。
    """
    notes: list[str] = []
    socket = SOCKET_BY_MODEL_TYPE.get(model_type)
    if socket is None:
        notes.append(f"model_type={model_type!r} 无 SocketType 映射（device 需人工确认："
                     "Slot=夹持方 / SlotAnchor=可承托可被夹），提案留空")
    anchor_by_slot: dict[str, str] = {}
    for slot in slots_detail:
        name = str(slot.get("name", ""))
        types: set[str] = set()
        unresolved: list[int] = []
        for mid in slot.get("valid_model_ids", []) or []:
            t = id_type_map.get(str(mid))
            (types.add(t) if t else unresolved.append(mid))
        if unresolved:
            notes.append(f"槽 {name}: valid_model_id {unresolved} 在档案里查不到条目，"
                         "AnchorType 提案跳过")
        if len(types) == 1:
            anchor = ANCHOR_BY_MODEL_TYPE.get(next(iter(types)))
            if anchor:
                anchor_by_slot[name] = anchor
            else:
                notes.append(f"槽 {name}: 内容 model_type={next(iter(types))!r} 无 "
                             "AnchorType 映射，提案留空")
        elif len(types) > 1:
            notes.append(f"槽 {name}: 内容混装 model_type={sorted(types)}，AnchorType 需人工定")
    proposals = {"socket_type": socket or "", "anchor_by_slot": anchor_by_slot,
                 "note": "提案仅供人审参考，本图不写 UE（属性落地唯一通道 = Hte.BrushSlots）"}
    return proposals, notes


# ---------------------------------------------------------------------------
# 调试级 @tool（与流水线节点共用上面的实现；整机装配走 bp_assembly 流水线）
# ---------------------------------------------------------------------------

class _DiffArgs(BaseModel):
    actor_name: str = Field(description="场景中设备 actor 的 label（如 BP_121_Capping_8a2）")
    device_key: str = Field(description="HTE 档案设备 key（results 下的键，如 121）")
    hte_json_path: str = Field(default="", description="HTE 档案 JSON 绝对路径；空 = 配置默认")


def make_bp_tools(client, hte_json_default: str = ""):
    """工厂：注入 UE 客户端，产出调试级 @tool（整机装配走流水线，勿逐件自由编排）。"""

    @tool(args_schema=_DiffArgs)
    def bp_slot_diff(actor_name: str, device_key: str, hte_json_path: str = "") -> str:
        """只读预检：HTE 槽键 vs 目标 BP 的 SCS 组件名 diff（待创建/已存在/LP_1 变体冲突）。
        整机装配（刷入+复核+人审）走 bp_assembly 流水线；本工具仅供单步调试。"""
        path_s = hte_json_path or hte_json_default
        try:
            spec = parse_hte_device(path_s, device_key)
        except ParseError as exc:
            return f"{_FAIL}{exc}"
        names, err = ue_scs_components(client, actor_name)
        if err:
            return f"{_FAIL}SCS 不可读：{err}"
        expected = [s.name for s in spec.slots]
        missing, pre_existing, conflicts = diff_slot_names(expected, names)
        lines = [f"设备 {spec.name}（key={device_key}）槽位 {len(expected)} 个；"
                 f"BP SCS 组件 {len(names)} 个",
                 f"- 待创建 {len(missing)}：{missing[:20]}",
                 f"- 已存在（将幂等跳过）{len(pre_existing)}：{pre_existing[:10]}",
                 f"- 变体冲突 {len(conflicts)}：{conflicts[:10]}"
                 + ("（同义不同名将并存，需人工判断）" if conflicts else "")]
        return "\n".join(lines)

    @tool(args_schema=_DiffArgs)
    def bp_brush(actor_name: str, device_key: str, hte_json_path: str = "") -> str:
        """经 Hte.BrushSlots 控制台命令刷槽位（唯一正确通道，幂等）并读日志复核。
        整机装配（含前置检查/语义提案/人审）走 bp_assembly 流水线；本工具仅供单步调试。"""
        path_s = (hte_json_path or hte_json_default).replace("\\", "/")
        cmd, err = brush_slots_cmd(actor_name, device_key, path_s)
        if err:
            return f"{_FAIL}{err}"
        _, err = ue_console(client, cmd)
        if err:
            return f"{_FAIL}{err}"
        log_text, err = ue_read_log(client, "创建", 8)
        if err:
            return f"{_FAIL}刷入命令已发，但日志复核失败：{err}"
        parsed = parse_brush_log(log_text)
        if parsed is None:
            return f"{_FAIL}刷入命令已发，但日志里找不到 [Hte.BrushSlots] 结果行"
        return (f"刷入完成：Actor={parsed['actor']} BP={parsed['bp']} "
                f"创建={parsed['created']} 跳过={parsed['skipped']} "
                f"属性同步={'是' if parsed['props_synced'] else '否'}")

    return [bp_slot_diff, bp_brush]
