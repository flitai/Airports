@echo off
REM Copyright (C) 2026 FlitAI.  SPDX-License-Identifier: GPL-3.0-only
REM 本项目以 GPL-3.0 授权，不含任何担保。完整条款见同目录 LICENSE 文件。
REM 双击启动：起 Range 服务器并打开浏览器。
REM 不能用 python -m http.server —— 它不支持 Range 请求，PMTiles 会失效。
cd /d "%~dp0"
python serve.py 8000 --open
pause
