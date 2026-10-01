#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 诊断 API 服务（纯 Python 标准库，供扣子/豆包技能商店「按次计费」模式调用）

部署方式（任选其一）：
  1. 自有服务器 / 轻量云主机：
       export WORKBUDDY_API_TOKEN="你的调用密钥"   # 可选；设置后调用方必须携带 X-Api-Token
       export DEEPSEEK_API_KEY="sk-..."            # 可选；设置后走 LLM 全题型诊断，否则用离线确定性引擎（仅覆盖 AP 微积分求导编目）
       python3 api_server.py --host 0.0.0.0 --port 8000
  2. 云函数（阿里云函数计算 / 腾讯云 SCF / AWS Lambda 等）：把本目录整体上传，入口指向本文件。

接口：
  GET  /health
       -> 200 {"ok": true, "mode": "offline"|"api", "version": "0.1.0"}

  POST /diagnose
       Content-Type: application/json
       Body（对齐 workbuddy_tool_schema.json 的 workbuddy_diagnose 参数）:
         {
           "problem": "题目原文",
           "steps": ["第1步", "第2步", "..."],
           "final_answer": "最终作答（可选）",
           "previous_diagnosis": {上一轮诊断 JSON（可选，触发进步对比）}
         }
       -> 200:
         {
           "schema_version": "1.0",
           "meta": {...},
           "first_error": {"found": bool, "step_index": int|null, "step_text": str|null, "summary": str},
           "dimensions": [8 个维度，每个 {id, label, rating, evidence, confidence}],
           "overall_confidence": float,
           "teacher_review_required": bool,
           "review_reason": str|null,
           "weaknesses": [str],
           "recommended_actions": [str],
           "improvement": {...}   # 仅在传入 previous_diagnosis 时返回
         }

计费说明：扣子技能商店「按次付费」按一次成功 API 调用计一次使用。
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workbuddy.diagnosis import run_diagnosis  # noqa: E402
from workbuddy.llm import DeepSeekClient  # noqa: E402
from workbuddy.models import Diagnosis  # noqa: E402
from workbuddy.progress import compare  # noqa: E402

VERSION = "0.1.0"
THRESHOLD = 0.75


def build_config():
    return {
        "llm": {
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            "temperature": 0.2,
            "max_tokens": 2048,
            "timeout_seconds": 60,
            "max_retries": 2,
        },
        "diagnosis": {"schema_version": "1.0", "confidence_threshold": THRESHOLD},
    }


def make_client(config):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    return DeepSeekClient(
        base_url=config["llm"]["base_url"],
        api_key=api_key,
        model=config["llm"]["model"],
        temperature=config["llm"]["temperature"],
        max_tokens=config["llm"]["max_tokens"],
        timeout_seconds=config["llm"]["timeout_seconds"],
        max_retries=config["llm"]["max_retries"],
    )


def to_submission(payload):
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps 必须是非空数组（学生解题步骤，按顺序）")
    if not all(isinstance(s, str) and s.strip() for s in steps):
        raise ValueError("steps 每一项必须是字符串")
    problem = payload.get("problem")
    if not isinstance(problem, str) or not problem.strip():
        raise ValueError("problem 必须是非空字符串（题目原文）")
    submission = {
        "problem": {"statement": problem, "format": "symbolic"},
        "steps": [{"index": i + 1, "text": s} for i, s in enumerate(steps)],
    }
    if isinstance(payload.get("final_answer"), str) and payload["final_answer"].strip():
        submission["final_answer"] = payload["final_answer"]
    return submission


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, obj):
        if status == 204:
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Token, Authorization")
            self.end_headers()
            return
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Token, Authorization")
        self.end_headers()
        if status != 204:
            self.wfile.write(body)

    def _check_auth(self):
        token = os.environ.get("WORKBUDDY_API_TOKEN", "").strip()
        if not token:
            return None
        got = self.headers.get("X-Api-Token", "")
        return None if got == token else "X-Api-Token 无效或缺失"

    def do_OPTIONS(self):
        self._send_json(204, None)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            mode = "api" if make_client(build_config()) is not None else "offline"
            self._send_json(200, {"ok": True, "mode": mode, "version": VERSION})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/diagnose":
            self._send_json(404, {"error": "not found"})
            return
        auth_err = self._check_auth()
        if auth_err:
            self._send_json(401, {"error": auth_err})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send_json(400, {"error": "空请求体"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {"error": "请求体不是合法 JSON: " + str(e)})
            return

        try:
            submission = to_submission(payload)
            diag = run_diagnosis(submission, build_config(), api_client=make_client(build_config()))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001 —— 服务端错误统一包装
            self._send_json(500, {"error": "诊断失败: " + str(e)})
            return

        result = diag.to_dict()
        prev = payload.get("previous_diagnosis")
        if prev is not None:
            try:
                result["improvement"] = compare(Diagnosis.from_dict(prev), diag)
            except Exception as e:  # noqa: BLE001
                self._send_json(400, {"error": "previous_diagnosis 无法解析: " + str(e)})
                return
        self._send_json(200, result)

    def log_message(self, fmt, *args):
        sys.stderr.write("[workbuddy-api] " + fmt % args + "\n")


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 诊断 API 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    config = build_config()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "api(LLM)" if make_client(config) is not None else "offline(确定性引擎)"
    print("WorkBuddy API 已启动: http://{0}:{1}  mode={2}".format(args.host, args.port, mode))
    if os.environ.get("WORKBUDDY_API_TOKEN", "").strip():
        print("已启用 X-Api-Token 鉴权")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
