"""LangGraph Studio 本机 PNA/CORS 垫片（127.0.0.1:2025 → 127.0.0.1:2024 全转发）。

为什么需要它：浏览器从公网页（smith.langchain.com）访问回环地址属 Private Network
Access，Chrome/Edge 预检要求 `Access-Control-Allow-Private-Network: true`；
langgraph dev 出于安全**主动拒绝**该预检（Disallowed CORS private-network），
Studio 因此 Failed to fetch。本垫片：OPTIONS 自己答（带 PNA 头），真实请求剥
Origin/PNA 头转发本机 dev server，回包补 CORS 头。纯本机回环，无第三方中继
（--tunnel 走 Cloudflare，数据出机，合规不采用）；SSE 流式原样透传。

用法（先起 langgraph dev :2024）：
    D:\\project\\kin_pad\\.venv\\Scripts\\python.exe orchestrator/studio_cors_shim.py
Studio baseUrl 用 http://127.0.0.1:2025 （即 https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2025）
"""

from __future__ import annotations

import argparse
import logging

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("studio_cors_shim")

UPSTREAM = "http://127.0.0.1:2024"
_ALLOW_ORIGIN = "https://smith.langchain.com"
_CORS = {
    "Access-Control-Allow-Origin": _ALLOW_ORIGIN,
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Max-Age": "86400",
    "Access-Control-Allow-Private-Network": "true",  # PNA 预检的关键一行
}
# 转发时剥掉的浏览器侧头（上游视本请求为本机直连，避开其私网安全闸）
_STRIP_REQ = {"host", "content-length", "origin", "access-control-request-private-network",
              "access-control-request-method", "access-control-request-headers"}
_STRIP_RESP = {"content-encoding", "transfer-encoding", "connection", "content-length"}


async def _stream(resp: httpx.Response, client: httpx.AsyncClient):
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()
        await client.aclose()


async def proxy(request: Request) -> Response:
    if request.method == "OPTIONS":  # 预检不上行，自己答
        return Response(status_code=204, headers=_CORS)
    url = UPSTREAM + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k.decode(): v.decode() for k, v in request.headers.raw
               if k.decode().lower() not in _STRIP_REQ}
    client = httpx.AsyncClient(timeout=httpx.Timeout(None))  # SSE 长连接不限时
    try:
        req = client.build_request(request.method, url, headers=headers, content=body)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return Response(f"上游 2024 不可达: {exc}", status_code=502, headers=_CORS)
    resp_headers = {k.decode(): v.decode() for k, v in resp.headers.raw
                    if k.decode().lower() not in _STRIP_RESP}
    resp_headers.update(_CORS)
    return StreamingResponse(_stream(resp, client), status_code=resp.status_code,
                             headers=resp_headers)


def main() -> None:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=2025)
    ap.add_argument("--upstream", default=UPSTREAM)
    args = ap.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    logging.basicConfig(level=logging.INFO)
    app = Starlette(routes=[Route("/{path:path}", proxy,
                                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])])
    logger.info("PNA/CORS 垫片 :%d → %s", args.port, UPSTREAM)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
