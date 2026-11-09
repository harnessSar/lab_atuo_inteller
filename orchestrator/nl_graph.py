"""自然语言 → UE 场景落地，采用网关模式）。

链路（ReAct环）：用户自然语言 → [plan] LLM 原生 tool calling 出**一步**
      → [execute] 唯一 @tool `unreal` 透传 EngineWire 网关（search/describe/execute）
      → Observation 回喂消息历史 → 回 [plan]，max_steps 兜底防死循环。

# steps统计:仅execute 计步,search/describe不计步
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

# kin_pad/src 进 sys.path（禁硬编码业务路径）
_KIN_PAD_SRC = Path(__file__).resolve().parents[2] / "kin_pad" / "src"
if _KIN_PAD_SRC.is_dir() and str(_KIN_PAD_SRC) not in sys.path:
    sys.path.insert(0, str(_KIN_PAD_SRC))

from langchain_core.tools import tool  # noqa: E402
from langchain_core.utils.function_calling import convert_to_openai_tool  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from llm.provider import ChatMessage, LlmConfig, LlmProvider  # noqa: E402
from orchestrator.ue_mcp_client import UeMcpClient  # noqa: E402

logger = logging.getLogger("lab_atuo_inteller.orchestrator.nl_graph")

# ---------------------------------------------------------------------------
# 工具面：单个 unreal 网关 @tool
# ---------------------------------------------------------------------------

_FAIL_PREFIX = "执行失败："  # Observation 失败前缀（report 据此判定本步未真执行）
_GATEWAY_TOOL = "unreal"     # EngineWire 对端网关工具名（tools/list 唯一暴露）


class _UeTransportError(RuntimeError):
    """传输层失败（UE 未开/token 失效/会话断裂）——execute 捕获后短路 report，不回喂。"""


class _GatewayArgs(BaseModel):
    """网关四参数"""

    operation: Literal["search", "describe", "execute"] = Field(
        description="search=按英文关键词检索能力；describe=查能力的参数契约；execute=执行能力")
    query: str = Field(
        default="", description="operation=search 时必填：2-4 个英文实词（动词+宾语，"
                                "如 'spawn actor'、'set actor transform'、'capture viewport screenshot'）")
    capability: str = Field(
        default="", description="operation=describe/execute 时必填：能力 id，逐字抄 search 结果"
                                "里的 capability 字段（如 control_actor.spawn），不要猜")
    params: dict[str, Any] = Field(
        default_factory=dict, description="operation=execute 时必填：参数对象，键名严格按"
                                          " describe 返回的 inputSchema；只放它列过的键")


def _strip_status_line(text: str) -> tuple[str, str]:
    """EngineWire 回执"状态行+空行+JSON 正文"→ (状态行, 正文)。无空行时正文=全文。"""
    if "\n\n" in text:
        head, payload = text.split("\n\n", 1)
        return head.strip(), payload
    return "", text


def _compact_search(payload: str, limit: int = 1200) -> str:
    """search 回执压缩（上下文保护横切）：查询返回json进行压缩描述。

    不映射不改名——只裁字段（capability/effect/summary/nextCall 保留，其余元数据丢）。
    解析失败原样截断返回。
    """
    try:
        obj = json.loads(payload)
        rows = obj.get("results")
        if not isinstance(rows, list):
            return payload[:limit]
        lines = []
        for r in rows:
            cap = r.get("capability", "?")
            effect = r.get("effect", "")
            summary = (r.get("summary") or "").split("\n")[0][:110]
            lines.append(f"{cap} [{effect}] {summary}")
        total = obj.get("totalResults", len(rows))
        out = f"命中 {total} 个能力（示 {len(lines)}）：\n" + "\n".join(lines)
        if obj.get("nextCursor"):
            out += "\n（还有更多，别翻页——换更准的关键词重搜）"
        return out[:limit]
    except (json.JSONDecodeError, ValueError):
        return payload[:limit]


def _compact_describe(payload: str, limit: int = 1800) -> str:
    """describe 回执压缩：只留契约要素（capability/inputSchema/examples/behavior/policy）。

    模型构造 params 的依据全保留；availability/cost/aliases 等元数据裁掉。
    """
    try:
        obj = json.loads(payload)
        keep = {k: obj[k] for k in ("capability", "inputSchema", "examples", "behavior",
                                    "policy", "params") if k in obj}
        if not keep:
            return payload[:limit]
        return json.dumps(keep, ensure_ascii=False)[:limit]
    except (json.JSONDecodeError, ValueError):
        return payload[:limit]


def _drop_images(images: list[dict[str, str]], work_dir: Path) -> tuple[str, str]:
    """content_images 块落盘（截图横切）：base64 → PNG/JPG 写 work_dir存储到本地。

    返回 (落盘路径, data URL)，无图/解码失败返回 ("", "")。
    data URL 供 VL 回喂（vision 开启时经 state.vision_image 注入一条带图 user 消息）
    base64 本身绝不进消息历史文本。
    """
    if not images:
        return "", ""
    mime = images[0].get("mimeType", "") or "image/png"
    ext = ".png" if "png" in mime else (".jpg" if ("jpeg" in mime or "jpg" in mime) else ".img")
    data = images[0].get("data", "")
    try:
        blob = base64.b64decode(data)
    except (ValueError, TypeError):
        return "", ""
    if not blob:
        return "", ""
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"nl_evidence{ext}"
    out.write_bytes(blob)
    return str(out), f"data:{mime};base64,{data}"


def _sanitize_execute(payload: str, work_dir: Path) -> tuple[str, str, str]:
    """execute 回执消毒（截图横切）：JSON 里的大 base64 图 → PNG 落盘，字段换路径标记。
    返回 (消毒后文本, 截图路径或 "", data URL 或 "")。
    """
    shot_path = ""
    shot_url = ""
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return payload[:500], shot_path, shot_url

    # sidecar 提取（图+数据双通道）：截图 metadata 的相机位姿/尺寸压成紧凑行进 obs，
    # 免被 500 截断；同时删掉对模型无用的 UE 侧路径/明细（省 obs 字符）
    meta_line = ""
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict):
            cl = data.get("cameraLocation")
            cr = data.get("cameraRotation") or {}
            if isinstance(cl, dict):
                meta_line = (f"[meta] {data.get('width', '?')}x{data.get('height', '?')} "
                             f"cam=({cl.get('x', 0):.0f},{cl.get('y', 0):.0f},{cl.get('z', 0):.0f}) "
                             f"rot=(p{cr.get('pitch', 0):.1f},y{cr.get('yaw', 0):.1f},"
                             f"r{cr.get('roll', 0):.1f}) ")
            for k in ("screenshotPath", "details", "imageBase64", "mimeType"):
                data.pop(k, None)

    def _walk(node: Any) -> Any:
        nonlocal shot_path, shot_url
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str) and len(node) > 4096:
            try:
                blob = base64.b64decode(node, validate=True)
            except (ValueError, TypeError):
                return node
            if blob[:8] == b"\x89PNG\r\n\x1a\n" or blob[:3] == b"\xff\xd8\xff":
                work_dir.mkdir(parents=True, exist_ok=True)
                ext = ".png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
                mime = "image/png" if ext == ".png" else "image/jpeg"
                out = work_dir / f"nl_evidence{ext}"  # 固定文件名，每次覆盖（单证据语义）
                out.write_bytes(blob)
                shot_path = str(out)
                shot_url = f"data:{mime};base64,{node}"
                return f"[base64 图像已落盘: {shot_path}]"
            return node[:200] + f"…[超长文本已截断, 原长 {len(node)}]"
        return node

    cleaned = _walk(obj)
    return meta_line + json.dumps(cleaned, ensure_ascii=False)[:500], shot_path, shot_url


def make_gateway_tool(client: UeMcpClient, work_dir: Path, sink: dict[str, str] | None = None):
    """工厂：注入 UE 客户端 + 证据目录，产出唯一网关 @tool（闭包捕获，图编译后不可变）。

    ``sink``：可选可变容器，截图的 data URL 写入 ``sink["image"]``——@tool 返回值
    只能是给模型的文本，图像经此旁路递给 execute 节点 → state.vision_image（VL 回喂）。
    """

    @tool(args_schema=_GatewayArgs)
    def unreal(operation: Literal["search", "describe", "execute"],
               query: str = "", capability: str = "", params: dict[str, Any] | None = None) -> str:
        """
        UE 编辑器网关：search 找能力 → describe 看参数契约 → execute 执行。
        所有对Unreal Engine 的操作都经此工具。
        """
        wire: dict[str, Any] = {"operation": operation}
        if operation == "search":
            if not query.strip():
                return f"{_FAIL_PREFIX}search 需要 query（2-4 个英文实词，如 'spawn actor'）"
            wire["query"] = query
            wire["limit"] = 6  # 一页 6 条够选；上下文保护
        elif operation == "describe":
            if not capability.strip():
                return f"{_FAIL_PREFIX}describe 需要 capability（逐字抄 search 结果的 id）"
            wire["capability"] = capability
        else:  # execute
            if not capability.strip():
                return f"{_FAIL_PREFIX}execute 需要 capability（先 search/describe 确认）"
            wire["capability"] = capability
            wire["params"] = params or {}

            if capability.endswith(".screenshot"):
                wire["params"]["returnBase64"] = True
                wire["params"]["includeMetadata"] = True
                wire["params"].setdefault("resolution", "1024x576")
        #@向UE mcp发起1次工具调用（tools/call）,把wire作为参数传过去，等UE的返回结果，返回到resp
        resp = client.execute(_GATEWAY_TOOL, wire)
        if not resp.ok:
            if not resp.raw:
                raise _UeTransportError(resp.error[:300])
            return f"{_FAIL_PREFIX}{resp.error[:600]}"  # 网关校验错误含契约信息，是自纠关键

        head, payload = _strip_status_line(resp.raw.get("content_text") or "")
        if operation == "search":
            return _compact_search(payload)
        if operation == "describe":
            return _compact_describe(payload)
        text, shot, shot_url = _sanitize_execute(payload, work_dir)
        # image ：落盘 + 标记，图片不进历史对话框，否则压爆模型
        img_shot, img_url = _drop_images(resp.raw.get("content_images") or [], work_dir)
        if img_shot:
            shot, shot_url = img_shot, img_url
            text = f"[截图已落盘: {shot}] " + text
        if shot_url and sink is not None:
            sink["image"] = shot_url  # VL 回喂旁路（execute 节点 → state.vision_image）
        prefix = f"[screenshot] {shot}\n" if shot else ""
        return prefix + (f"{head} " if head and head != "ok" else "") + text

    return unreal


_PLANNER_SYS = """你是 UE 场景操作助手（ReAct 模式），通过唯一的 unreal 网关工具操作 Unreal Engine 编辑器。

工作流（严格遵守）：
1. search：（如 'spawn actor'、'set actor transform'、'capture viewport screenshot'）检索能力；
    结果每行给出 capability id（逐字抄）与说明。   
2. describe：执行任何能力前先看它的契约；params 的键名严格按返回的 inputSchema，
   有 examples 可照抄改值。
3. execute：一次执行一个能力；params 里只放 describe 列过的键。

"""

# 图像分析VL追加段（LlmConfig.vision=True 时拼进 system prompt；纯文本模型不加）
_VISION_SUFFIX = """
视觉能力：你是多模态模型，截图能力执行后，图像会以附图形式直接出现在后续消息里
（你能真正"看到"视口，而非仅收到路径文本）。主动利用：
- 生成/移动/摆放类动作后，截图并**亲眼看图确认**物体位置、朝向、遮挡关系是否符合预期；
- 回答"场景里有什么/布局如何/是否干涉"类空间问题时，先截图再据图作答；
- 图中选中高亮（橙色描边）= 编辑器当前选中的 Actor；
- 截图参数保持默认即可（网关已强制 returnBase64=true，勿显式传 false——否则只有路径文本看不到图）。
注意：你只能看到**最新一张**截图（历史图自动移除），需要旧画面时重新截图。
"""


# 资产编排追加段（enable_asset_tools=True 时拼进 system prompt）
_ASSET_SUFFIX = """
运动学资产编排工具（纯本地、无 UE；产物是 TOML/Lua 草稿，不落业务盘，人审后才合入）：
- asset_catalog：读 Blender 场景导出 JSON → 物体清单（name/parent 装配父子）。编排必先调它。
- asset_land：把全部语义结论**一次性**提交落地（自动轴判定 + 主从镜像分组；每件只提交一次）。
  逐件判断运动：宁多勿漏、子级件也列、纯框架/瓶/工装等静止件不列；
  motion 指**该件自身**的自由度——丝杠/手指座/底座滑轨沿轴滑动永远是"平移"，
  拧盖夹爪本体绕轴转才是"旋转"；模组名带"旋转/拧"是模组级功能，≠其零件都旋转。
  返回的人审提示必须原样报告用户，不得隐瞒。
- asset_emit：由落地关节生成 TOML/Lua 草稿并跑机器闸；闸失败按问题清单修结论后重新 asset_land。
- asset_score：草稿对 golden TOML/Lua 评分（进产物/type/axis/passive/lua + 失败明细）。
编排流程：asset_catalog 看清单 → （有资料图就结合看）逐件判运动 → asset_land →
asset_emit 看闸 → 有 golden 时 asset_score 评分并报告成绩与多出件。
"""

#skills中3个目录的排序优先级
_SKILL_ORDER = {"workflow": 0, "device": 1, "other": 2}


def _load_skills(skills_dir: Path | None) -> str:
    """扫描 ``skills_dir/**/SKILL.md`` 拼 prompt 段（最小实现：全量注入 system prompt）。

    目录三分：``workflow/`` Blender–KinPad–Unreal，``device/`` 设备类型，``other/`` 其余。
    格式同 ``.cursor/skills``（YAML frontmatter + 正文）；frontmatter 剥掉，只注正文。
    标题用相对路径，便于模型按类命中。skill 再涨后改 RAG 按需加载，本函数是过渡。
    """
    if not skills_dir or not skills_dir.is_dir():
        return ""

    def _sort_key(smd: Path) -> tuple[int, str]:
        rel = smd.parent.relative_to(skills_dir)
        top = rel.parts[0] if rel.parts else ""
        return (_SKILL_ORDER.get(top, 9), rel.as_posix().lower())

    parts: list[str] = []
    for smd in sorted(skills_dir.glob("**/SKILL.md"), key=_sort_key):
        try:
            text = smd.read_text(encoding="utf-8")
        except OSError:
            continue
        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                body = text[end + 4:].strip()
        if body:
            rel = smd.parent.relative_to(skills_dir).as_posix()
            parts.append(f"### 技能：{rel}\n{body}")
    if not parts:
        return ""
    return (
        "\n\n## 业务技能（命中即按技能执行，不得自由发挥）\n\n"
        "分三类：`workflow/` = Blender–KinPad–Unreal 工作流；"
        "`device/` = 设备类型特点与迭代经验；`other/` = 场景、Zenoh、打包等。\n"
        "几何常数、脉冲标定、端口以磁盘 TOML 与 `.cursor/rules` 为准，技能卡片里的数字只是提示。\n\n"
        + "\n\n".join(parts)
    )

# ---------------------------------------------------------------------------
# 定义状态图
# ---------------------------------------------------------------------------
class NlState(TypedDict, total=False):
    #TypedDictt是Python的类型提示，用法同dict普通字段，但有类型检查
    """图状态（total=False：各节点增量写自己负责的字段，不用所有字段都填上）。

    流转约定（ReAct 环）：入口写 user_input/messages/steps → plan 读 messages 写
    plan/messages/report/error → execute 写 result/messages/steps/fails/screenshot_path/error
    → 回 plan；report 节点只做终态格式化。
    """

    user_input: str              # 用户自然语言指令（invoke 时写入）
    messages: list[ChatMessage]  # 累积对话（system/user + assistant tool_calls + tool 观察）
    steps: int                   # 已 execute 次数（仅网关 operation=execute 计 1），max_steps 防死循环
    fails: int                   # 本地校验拦截**连续**计数
    plan: dict[str, Any]         # 本步计划：{tool:"unreal", arguments, call_id}；execute 按名分发
    error: str                   # 任何节点失败写此；非空 → 条件边短路到 report
    result: dict[str, Any]       # execute 的 UE 返回（UnrealResponse.raw：content_text/content_json）
    screenshot_path: str         # 给人看的，execute 截图落盘的证据 PNG（每次覆盖，单证据语义）
    vision_image: str            # 给LLM看的，最新截图 data URL（VL 回喂；execute 写、plan 消费后清零，单证据语义）
    report: str                  # 终态文本：plan 的收敛出口写结论；report 节点只在 error 时改写


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def make_plan_node(llm: LlmProvider, tool_defs: list[dict[str, Any]], max_steps: int = 18,
                   vision: bool = False):
    """
    工厂：注入 LLM 提供者 + OpenAI tools 声明 + 步数上限，产出 plan 节点，写入NlState共享状态。
    """
    valid_names = {d["function"]["name"] for d in tool_defs}  # 白名单 = 已声明工具名集合

    def plan_node(state: NlState) -> NlState:
        """
        plan 节点（ReAct 推理步）：读全量消息历史 → 原生 tool calling 出下一步。
        """
        #1错误控制：超过最大步数或错误3次，中止任务，向上报错
        if state.get("steps", 0) >= max_steps:
            return {"error": f"超过最大步数 {max_steps}（execute 上限），任务未收敛"}
        if state.get("fails", 0) >= 3:
            return {"error": "参数连续 3 次未通过本地校验，任务中止"}

        #2.msg+vision的准备    
        msgs = list(state["messages"])
        vision_consumed = False
        if vision:
            img = state.get("vision_image") or ""
            if img:
                # 新截图注入前，先把历史带图消息剥成纯文本（剥图副本替换）
                msgs = [ChatMessage(m.role, m.content, m.tool_calls, m.tool_call_id) if m.images else m
                        for m in msgs]
                # 带图 user 消息挂历史尾（tool 观察之后，协议合法
                msgs.append(ChatMessage(
                    "user", "（最新视口截图见附图——可直接据此判断场景状态/空间关系）",
                    images=[img]))
                vision_consumed = True
                logger.info("plan[vision]: 注入截图回喂（%d KB data URL）", len(img) * 3 // 4 // 1024)
        consumed: NlState = {"vision_image": ""} if vision_consumed else {}
        last_err = ""

        #3.LLM推理
        for _ in range(2):  
            try:
                msg = llm.chat(msgs, tools=tool_defs)
            except RuntimeError as e:
                #3.1LLM调用2次失败，中止任务，向上报错，
                return {"error": f"LLM 调用失败：{e}", **consumed}
            
            calls = msg.get("tool_calls") or []

            #3.2.没有工具调用，说明模型直接给出了结论，直接返回
            if not calls:
                conclusion = (msg.get("content") or "").strip()
                base_len = len(state["messages"]) + (1 if vision_consumed else 0)
                if not conclusion and len(msgs) == base_len:
                    msgs.append(ChatMessage("user", "请立即调用工具执行，或用文字给出结论。"))
                    continue
                # 收敛出口：完成、直接回答、做不了
                return {"report": conclusion or "（模型未给出结论）",
                        "messages": msgs + [ChatMessage("assistant", conclusion or "（模型未给出结论）")],
                        **consumed}

            #3.3.有工具调用，说明模型需要调用工具，继续执行
            fn = (calls[0].get("function") or {})
            name = fn.get("name", "")
            try:
                if name not in valid_names:
                    raise ValueError(f"计划不在白名单: {name}")
                arguments = json.loads(fn.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError(f"arguments 不是对象: {str(fn.get('arguments'))[:120]}")
            except (json.JSONDecodeError, ValueError) as exc:
                last_err = str(exc)
                msgs.append(ChatMessage("user", f"工具调用不合法（{exc}）。请重新调用正确的工具。"))
                continue
 
            call_id = calls[0].get("id") or "call_0"
            hist = msgs + [ChatMessage("assistant", msg.get("content") or "", tool_calls=[calls[0]])]
            plan = {"tool": name, "arguments": arguments, "call_id": call_id}
            logger.info("plan[exec %d]: %s %s", state.get("steps", 0), name,
                        str(arguments)[:200])
            return {"plan": plan, "messages": hist, **consumed}
        return {"error": f"计划解析失败（重试 2 次）：{last_err}", "messages": msgs, **consumed}

    return plan_node


def _args_hint(tool_obj) -> str:
    """从 args_schema 生成紧凑参数清单（必传/可选），拼进校验失败 Observation。

    pydantic 报错只说"缺什么"，不告诉模型"你的键名错在哪"；附清单后模型逐字对照。
    """
    sch = tool_obj.args_schema.model_json_schema()
    req = set(sch.get("required", []))
    parts = []
    for k in sch.get("properties", {}):
        tag = "必传" if k in req else "可选"
        parts.append(f"{k}({tag})")
    return ", ".join(parts)


def make_execute_node(tools: dict[str, Any], work_dir: Path,
                      sink: dict[str, str] | None = None, vision: bool = False):
    """
    工厂：注入工具表+ 证据目录，产出 execute 节点。
    """

    def execute_node(state: NlState) -> NlState:
        """
        execute 节点（ReAct 行动步）：tool.invoke 触发参数校验 → 执行 → Observation。
        """
        #1.取状态的plan+查工具表
        plan = state["plan"]
        tool_obj = tools.get(plan["tool"])
        #1.1.工具不存在，结束任务，报出错误
        if tool_obj is None:  
            return {"error": f"execute 分发失败：未知工具 {plan['tool']}"}
        
        #2.清旧图
        #sink是临时字典，记录UE的截图data URL = {"image": "data:image/png;base64,iVBORw0KGgo..."}
        if sink is not None:
            sink.pop("image", None)  # 清旧图：sink 生命周期跨整个 invoke，防上次残留误注

        #3.执行工具
        try:
            obs = tool_obj.invoke(plan["arguments"])
        except _UeTransportError as e:
            #3.1.UE连接失败，结束任务，报出错误
            return {"error": f"UE 连接失败：{e}"}
        except Exception as e: 
            #3.2.参数校验失败，结束任务，报出错误
            obs = (f"{_FAIL_PREFIX}参数校验（请对照参数清单修正后重新调用）：{str(e)[:300]}"
                   f"；参数清单：{_args_hint(tool_obj)}")
            hist = state["messages"] + [ChatMessage("tool", obs, tool_call_id=plan["call_id"])]
            return {"result": {"content_text": obs}, "messages": hist,
                    "fails": state.get("fails", 0) + 1}
        
        #3.3.参数校验成功，更新状态
        hist = state["messages"] + [ChatMessage("tool", obs, tool_call_id=plan["call_id"])]
        out: NlState = {"result": {"content_text": obs}, "messages": hist, "fails": 0}
        
        # 4.更新steps
        if plan["tool"] != _GATEWAY_TOOL or (plan["arguments"].get("operation") or "") == "execute":
            out["steps"] = state.get("steps", 0) + 1
        m = re.match(r"\[screenshot\] (.+)", obs)
        if m:
            out["screenshot_path"] = m.group(1).strip()
        # 5.如果截图了，更新state中vision_image（作为完成任务的证据），这样下一步plan时就能看到截图
        if vision and sink is not None and sink.get("image"):
            out["vision_image"] = sink["image"]
        return out

    return execute_node


def report_node(state: NlState) -> NlState:
    """report 节点（终态格式化）：error → ❌ + 原因；否则用 plan 收敛出口写好的结论。

    成功时结论由模型自己总结（ReAct 最后一步的文字），这里只补截图路径尾巴。
    """
    if state.get("error"):
        return {"report": f"❌ {state['error']}"}
    lines = [state.get("report") or "（无结论）"]
    if state.get("screenshot_path"):
        lines.append(f"截图证据: {state['screenshot_path']}")
    return {"report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# 图装配
# ---------------------------------------------------------------------------

def build_nl_graph(llm: LlmProvider, client: UeMcpClient, work_dir: Path, max_steps: int = 18,
                   vision: bool = False, extra_tools: list | None = None):
    """
    @abstract:装配 LangGraph
    """
    # 1.制作node：计划、执行、反馈这3个
    sink: dict[str, str] = {}  # 截图 data URL 旁路
    gateway = make_gateway_tool(client, work_dir, sink)

    #2.制作tools真实工具数组，tool_defs工具表转换为OpenAI工具定义（工具说明书）
    #tools是工具表，包含gateway和extra_tools
    tools: dict[str, Any] = {gateway.name: gateway}  
    for t in (extra_tools or []):
        tools[t.name] = t
    tool_defs = [convert_to_openai_tool(t) for t in tools.values()]
    g = StateGraph(NlState)
    g.add_node("plan", make_plan_node(llm, tool_defs, max_steps, vision))
    g.add_node("execute", make_execute_node(tools, work_dir, sink, vision))
    g.add_node("report", report_node)
    g.set_entry_point("plan")

    # 3.制作edge：plan ⇄ execute 的 ReAct 环   
    # 完成的情况:plan->execute->plan->report->END
    # 失败的情况:plan->execute->report->END
    g.add_conditional_edges(
        "plan",
        lambda s: "report" if (s.get("error") or s.get("report")) else "execute",
        {"execute": "execute", "report": "report"},
    )
    g.add_conditional_edges(
        "execute",
        lambda s: "report" if s.get("error") else "plan",  # ← ReAct 回边
        {"plan": "plan", "report": "report"},
    )
    g.add_edge("report", END)

    #4.编译graph图
    return g.compile()


def run_nl_command(
    user_input: str,
    *,
    llm_cfg: LlmConfig | None = None,
    ue_endpoint: str = "http://127.0.0.1:3000/mcp",
    ue_token_file: str | Path | None = None,
    work_dir: Path | None = None,
    skills_dir: str | Path | None = None,
    max_steps: int = 18,
    user_images: list[str] | None = None,
    enable_asset_tools: bool = True,
) -> NlState:
    """
    一次自然语言指令的端到端入口，返回NlState状态。
    """
    # 装配模型
    llm = LlmProvider(llm_cfg)
    # 装配 UE MCP 客户端
    client = UeMcpClient(ue_endpoint, token_file=ue_token_file)
    # 证据目录
    wd = work_dir or (Path(__file__).resolve().parents[1] / "_work")
    # 技能目录
    sd = Path(skills_dir) if skills_dir else (Path(__file__).resolve().parents[1] / "skills")
    # 是否支持vision
    vision = llm.cfg.vision
    
    # 资产编排工具
    extra: list = []
    if enable_asset_tools:
        try:
            from orchestrator.asset_tools import make_asset_tools
            extra = make_asset_tools(wd)
        except ImportError as exc:
            logger.warning("资产工具不可用（%s），本轮仅 UE 网关", exc)
    #提示词生成：系统提示词+vision提示词+资产编排提示词+技能提示词
    sys_prompt = (_PLANNER_SYS + (_VISION_SUFFIX if vision else "")
                  + (_ASSET_SUFFIX if extra else "") + _load_skills(sd))
    
    # 编译状态图
    graph = build_nl_graph(llm, client, wd, max_steps, vision, extra_tools=extra)
    # ReAct初始：system 规则 + 用户指令
    init_user = ChatMessage("user", user_input,
                            images=(user_images if (vision and user_images) else None))
    init: NlState = {
        "user_input": user_input,
        "messages": [ChatMessage("system", sys_prompt), init_user],
        "steps": 0,
        "fails": 0,
    }
    
    return graph.invoke(init, {"recursion_limit": 300})
