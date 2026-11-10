"""自定义图注册表。新增一张图：实现 ``match`` / ``run``，追加到 ``CUSTOM_GRAPHS``。

对话框按列表顺序认领，第一张命中的图执行。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from orchestrator.graph.custom.asset import NAME as _ASSET_NAME
from orchestrator.graph.custom.asset import match as _asset_match
from orchestrator.graph.custom.asset import run as _asset_run
from orchestrator.graph.custom.bp_assembly import NAME as _BP_NAME
from orchestrator.graph.custom.bp_assembly import match as _bp_match
from orchestrator.graph.custom.bp_assembly import run as _bp_run


class CustomGraph(NamedTuple):
    name: str
    match: Callable
    run: Callable


CUSTOM_GRAPHS: list[CustomGraph] = [
    CustomGraph(_ASSET_NAME, _asset_match, _asset_run),
    CustomGraph(_BP_NAME, _bp_match, _bp_run),
]
