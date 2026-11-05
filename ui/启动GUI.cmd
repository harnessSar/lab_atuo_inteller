@echo off
rem sim_harness 自然语言 GUI 入口（Chainlit 薄面板）
rem 浏览器打开 http://localhost:8501 ；端口勿改 8000（UE MCP 占用）
title sim_harness NL-GUI (Chainlit :8501)
cd /d D:\project\sim_harness
D:\project\kin_pad\.venv\Scripts\python.exe -m chainlit run ui\chat_ui.py --port 8501
pause
