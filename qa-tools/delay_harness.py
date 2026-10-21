#!/usr/bin/env python3
"""时序缺陷复现工具（零依赖，仅用 Python 标准库）—— 刘工维护，前端文件域。

用途：给「竞态」与「防重复提交」两类缺陷提供**可实测、可复现**的慢请求环境。
两类缺陷用不同模式，禁止混用（混用会产生假阳性，见 docs/team/engineering/FRONTEND.md §1.3.1）：

  模式 A「竞态」：同一路径第一次慢、第二次快 → 验证旧响应不覆盖新结果。
  模式 B「防重复提交」：不做延迟，只统计「收到几次请求」→ 验证连点是否被前端拦住。

启动：
  python3 ssqyuce/web/tools/delay_harness.py --port 18099
  # 可选：--delay 3  （模式下第一次请求的延迟秒数，默认 3）

端点：
  GET  /reset            计数清零 + 延迟序列复位。**每轮复现前必须先调**，否则第 2 轮起顺序错乱。
  GET  /stats            返回 {total, slow_count, requests:[{n,path,delay_ms,ts}]}
  ANY  /api/<anything>   业务探针：
                         模式 A（默认）：第 1 次 sleep --delay 秒，第 2 次起立即返回
                         模式 B：加 --mode cheap，全部立即返回（只统计条数）
  GET  /                 用法说明

实测示例（手册里引用的就是这些命令）：
  curl -s localhost:18099/reset
  curl -s -o /dev/null -w 'A %{time_total}s\n' localhost:18099/api/items   # 约 3s
  curl -s -o /dev/null -w 'B %{time_total}s\n' localhost:18099/api/items   # 约 0s
  curl -s localhost:18099/stats
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
STATE = {"total": 0, "slow_count": 0, "requests": []}
CONFIG = {"delay": 3.0, "mode": "race"}  # race | cheap


def _reset() -> None:
    with LOCK:
        STATE["total"] = 0
        STATE["slow_count"] = 0
        STATE["requests"] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 前端联调常跨端口，显式放开，避免被 CORS 挡住误判成「服务异常」
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path == "/reset":
            _reset()
            self._send(200, {"ok": True, "message": "已复位（计数+延迟序列）"})
            return

        if path == "/stats":
            with LOCK:
                self._send(200, dict(STATE))
            return

        if path == "/":
            self._send(200, {
                "ok": True,
                "usage": ["GET /reset", "GET /stats", "ANY /api/*"],
                "mode": CONFIG["mode"],
                "delay_s": CONFIG["delay"],
            })
            return

        self._handle_probe(path)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 读掉 body，保证 HTTP/1.1 keep-alive 不卡
        self._handle_probe(self.path.split("?", 1)[0])

    def _handle_probe(self, path: str) -> None:
        with LOCK:
            STATE["total"] += 1
            n = STATE["total"]
            # 模式 A：只有第 1 次慢（造「A 慢 B 快」顺序）；模式 B：从不延迟
            slow = CONFIG["mode"] == "race" and n == 1
            delay = CONFIG["delay"] if slow else 0.0
            if slow:
                STATE["slow_count"] += 1
            STATE["requests"].append({
                "n": n,
                "path": path,
                "delay_ms": int(delay * 1000),
                "ts": round(time.time(), 3),
            })

        if delay:
            time.sleep(delay)

        self._send(200, {
            "ok": True,
            "n": n,
            "delay_ms": int(delay * 1000),
            "server_seq": n,  # 前端可用它判断「最后渲染的是不是最新那次请求」
        })

    def log_message(self, fmt: str, *args) -> None:  # 静默，避免污染实测输出
        return


def main() -> None:
    ap = argparse.ArgumentParser(description="时序缺陷复现工具（竞态 / 防重复提交）")
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--delay", type=float, default=3.0, help="模式 A 第一次请求的延迟秒数")
    ap.add_argument("--mode", choices=["race", "cheap"], default="race",
                    help="race=第一次慢（竞态）；cheap=全部立即返回（只数条数）")
    args = ap.parse_args()

    CONFIG["delay"] = args.delay
    CONFIG["mode"] = args.mode

    # 端口前置检查：端口被占时 httputil 只会抛一个 OSError，看起来像「工具坏了」，
    # 而真相通常是「上一轮残留进程还在延迟」——那会让实测数字翻倍（严QA 2026-10-21
    # 就撞到过：旧进程在延迟，新进程没起来，测出 6.009523s = 2×3s，差点报成 addon 缺陷）。
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"[!] 端口 {args.port} 不可用：{e}", flush=True)
        print(f"[!] 先确认占用者归属，不要直接换端口就继续：", flush=True)
        print(f"    ss -lntp | grep {args.port}        # 看 PID/进程名", flush=True)
        print(f"    ps -o pid,ppid,cmd -p <PID>        # 确认是不是你上一轮的进程", flush=True)
        print(f"    kill <PID>                          # 精确 kill，别用 pkill -f（会匹配到自己的命令行）", flush=True)
        sys.exit(3)
    srv.daemon_threads = True
    print(f"harness listening http://127.0.0.1:{args.port}  mode={args.mode} delay={args.delay}s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
