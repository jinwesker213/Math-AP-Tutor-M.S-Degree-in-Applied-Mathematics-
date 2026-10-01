#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 诊断 CLI（技能包自检 / 本地验证 / 无服务器模式）

用法：
  python3 diagnose_cli.py --input sample_submission.json [--output diagnosis.json]
  python3 diagnose_cli.py --problem "题目原文" --steps "第1步" "第2步"
  python3 diagnose_cli.py --input sample_submission.json --previous diagnosis.json   # 复测进步对比
  python3 diagnose_cli.py --input sample_submission.json --api                      # 需 DEEPSEEK_API_KEY
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workbuddy.diagnosis import run_diagnosis  # noqa: E402
from workbuddy.llm import DeepSeekClient  # noqa: E402
from workbuddy.models import Diagnosis  # noqa: E402
from workbuddy.progress import compare  # noqa: E402


def build_config():
    return {
        "llm": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "temperature": 0.2,
            "max_tokens": 2048,
            "timeout_seconds": 60,
            "max_retries": 2,
        },
        "diagnosis": {"schema_version": "1.0", "confidence_threshold": 0.75},
    }


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 诊断 CLI")
    parser.add_argument("--input", help="学生提交 JSON 路径（与 api_server 请求体同构）")
    parser.add_argument("--problem", help="题目原文（与 --steps 配合）")
    parser.add_argument("--steps", nargs="+", help="解题步骤（每步一个参数）")
    parser.add_argument("--previous", help="上一轮诊断 JSON 路径（复测进步对比）")
    parser.add_argument("--output", help="诊断结果 JSON 输出路径")
    parser.add_argument("--api", action="store_true", help="使用 DeepSeek API（需 DEEPSEEK_API_KEY）")
    args = parser.parse_args()

    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            payload = json.load(f)
    elif args.problem and args.steps:
        payload = {"problem": args.problem, "steps": list(args.steps)}
    else:
        parser.error("需要 --input，或 --problem 与 --steps")

    config = build_config()
    client = None
    if args.api:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key.startswith("sk-"):
            parser.error("--api 需要有效的 DEEPSEEK_API_KEY（以 sk- 开头）")
        client = DeepSeekClient(
            base_url=config["llm"]["base_url"], api_key=key, model=config["llm"]["model"]
        )

    if not isinstance(payload.get("problem"), str) or not isinstance(payload.get("steps"), list):
        parser.error("输入 JSON 需要字段：problem(字符串)、steps(字符串数组)")
    submission = {
        "problem": {"statement": payload["problem"], "format": "symbolic"},
        "steps": [{"index": i + 1, "text": s} for i, s in enumerate(payload["steps"])],
    }
    if isinstance(payload.get("final_answer"), str) and payload["final_answer"].strip():
        submission["final_answer"] = payload["final_answer"]

    diag = run_diagnosis(submission, config, api_client=client)
    result = diag.to_dict()
    if args.previous:
        with open(args.previous, "r", encoding="utf-8") as f:
            result["improvement"] = compare(Diagnosis.from_dict(json.load(f)), diag)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("已写入 {0}（mode={1}）".format(args.output, diag.meta["mode"]))
    else:
        print(text)


if __name__ == "__main__":
    main()
