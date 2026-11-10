"""本地/云端模型适配层（OpenAI-compatible 统一入口）。

开发期 Ollama（5060 Win 原生）、联调/生产 vLLM（5090 Linux 原生）、云端 API，
三者都暴露 OpenAI-compat ``/v1/chat/completions``，本层只认 ``base_url + model``，
切换只改配置，业务节点不感知（规则：模型抽象单点）。

依赖：仅标准库 + ``requests``（kin_pad 主依赖已含）。本地推理零许可费；
云端 API 按 token 计费（本层不强制，由配置决定 base_url 指向哪）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class ChatMessage:
    """OpenAI-compat 消息；ReAct 扩展：assistant 回放 tool_calls、tool 角色带配对 id。

    协议硬要求：assistant 带 tool_calls 的消息后必须跟 role="tool" 且 tool_call_id
    配对的消息，缺一 vLLM/Ollama 直接 400。

    VL 扩展：``images``（data URL 列表）非空时 content 组装为 multimodal parts
    （``[{"type":"text",...},{"type":"image_url",...}]``，OpenAI vision 规范）；
    仅挂 **user** 角色（tool 角色带图各 serving 栈支持不一，user 最稳，已实测
    vLLM Qwen3-VL 带图+tools 同轮兼容）。纯文本模型（Ollama qwen2.5 等）须保持
    images=None——配置侧 ``LlmConfig.vision`` 是总闸。
    """

    role: str
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None   # assistant 发起工具调用时原样回放
    tool_call_id: str | None = None                  # role="tool" 时与 assistant 的调用配对
    images: list[str] | None = None                  # data URL（"data:image/png;base64,..."），仅 user 角色使用

    def to_dict(self) -> dict[str, Any]:
        if self.images:
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            parts += [{"type": "image_url", "image_url": {"url": u}} for u in self.images]
            d: dict[str, Any] = {"role": self.role, "content": parts}
        else:
            d = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class LlmConfig:
    """模型连接配置；从环境变量或 sim_harness.toml 注入。"""

    base_url: str = field(default_factory=lambda: os.environ.get("SIM_HARNESS_LLM_BASE_URL", "http://localhost:11434/v1"))
    model: str = field(default_factory=lambda: os.environ.get("SIM_HARNESS_LLM_MODEL", "qwen2.5:7b"))
    api_key: str = field(default_factory=lambda: os.environ.get("SIM_HARNESS_LLM_API_KEY", "ollama"))
    temperature: float = 0.2
    timeout_s: float = 120.0
    # VL 总闸：True = 当前模型是多模态（如 Qwen3-VL），nl_graph 截图回喂带图 user 消息；
    # False（默认）= 纯文本模型，图像只落盘不进消息（Ollama qwen2.5 等，防 400）
    vision: bool = False


class LlmProvider:
    """OpenAI-compat chat 客户端；tool calling 与 guided decoding（JSON schema）。"""

    def __init__(self, cfg: LlmConfig | None = None) -> None:
        self.cfg = cfg or LlmConfig()

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """单次对话。``json_schema`` 提供时请求 guided/structured 输出（vLLM 支持；Ollama 部分支持）。

        ``max_tokens``：输出上限（长 JSON 结论防截断；缺省由服务端决定）。
        ``temperature``：单次覆盖采样温度（缺省用 ``cfg.temperature``）；
        确定性单发（如资产语义步）传 0.0。

        Returns:
            原始 ``choices[0].message`` dict（含 content / tool_calls）。
        Raises:
            RuntimeError: HTTP 非 2xx 或响应结构异常。
        """
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_schema:
            # vLLM: response_format json_schema；Ollama: format=json（弱）。
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_output", "schema": json_schema},
            }

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.api_key}"}
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=self.cfg.timeout_s)
        except requests.RequestException as exc:
            raise RuntimeError(f"LLM 请求失败 {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"LLM 响应结构异常: {str(data)[:300]}") from exc

    def chat_text(self, messages: list[ChatMessage], **kwargs: Any) -> str:
        msg = self.chat(messages, **kwargs)
        return str(msg.get("content", ""))
