#!/bin/sh
# Copyright (C) 2026 FlitAI.  SPDX-License-Identifier: GPL-3.0-only
# 本项目以 GPL-3.0 授权，不含任何担保。完整条款见同目录 LICENSE 文件。
# 双击启动：起 Range 服务器并打开浏览器。
# 不能用 python3 -m http.server —— 它不支持 Range 请求，PMTiles 会失效。
cd "$(dirname "$0")" || exit 1
exec python3 serve.py 8000 --open
