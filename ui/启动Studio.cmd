@echo off
rem LangGraph Studio 调试端（trace/单步/时间旅行；生产对话走 启动GUI.cmd）
rem 双进程：langgraph dev(:2026, 图服务) + PNA/CORS 垫片(:2025, 给浏览器用)
rem 浏览器打开 https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2025
rem ※ baseUrl 必须是 2025 垫片口：浏览器公网页→回环属 Private Network Access，
rem   dev server 主动拒 PNA 预检（直连 dev 口会 Failed to fetch），垫片补 PNA 头。
rem ※ PYTHONUTF8=1 是中文区硬要求（langgraph_api 裸 open()，GBK 区炸解码）。
rem ※ dev 用 2026：2024 被幽灵 socket 占（netstat 有 LISTENING 但进程已死）。
rem ※ 同端口双实例会抢连接（Windows 允许两进程同口 LISTENING，症状 = schemas
rem   /graph 500 空正文），启动前先把旧 shim/dev 进程清干净。
title sim_harness Studio shim (:2025 -^> :2026)
cd /d D:\project\lab_atuo_inteller

rem 清旧 shim / langgraph dev 进程，防双实例抢端口
for /f "tokens=2" %%p in ('wmic process where "name='python.exe' and commandline like '%%studio_cors_shim%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /pid %%p /f >nul 2>&1
for /f "tokens=2" %%p in ('wmic process where "name='python.exe' and commandline like '%%langgraph_cli%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /pid %%p /f >nul 2>&1

start "langgraph-dev" /min cmd /c "set PYTHONUTF8=1&& D:\project\kin_pad\.venv\Scripts\python.exe -m langgraph_cli dev --no-browser --allow-blocking --no-reload --port 2026"
timeout /t 6 /nobreak >nul
D:\project\kin_pad\.venv\Scripts\python.exe orchestrator\studio_cors_shim.py --port 2025 --upstream http://127.0.0.1:2026
pause
