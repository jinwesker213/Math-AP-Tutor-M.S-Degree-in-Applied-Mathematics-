#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy · 全 AP 数学诊断（DeepSeek 版，支持 PDF 上传）

覆盖全部 AP 数学（不限链式法则）：学生上传 PDF（或粘贴文本），
系统抽取文字 → 交给 DeepSeek 用通用诊断提示词 → 输出八维能力数据。

运行：
    DEEPSEEK_API_KEY=sk-xxx python3 pdf_diagnose.py      # 用环境变量
    python3 pdf_diagnose.py                              # 在网页里填 Key
然后浏览器打开  http://localhost:8081

PDF 文本抽取依赖 pypdf（已随项目放到 ./.tools/lib；缺失时可 pip install pypdf）。
扫描件(图片型 PDF)需先 OCR 转文字，或直接粘贴文本。
"""
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 本地 pypdf 依赖（若存在）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".tools", "lib"))

# ------------------------------------------------------------
# 通用 AP 数学诊断提示词（与 ap_diagnosis_prompt.md 一致）
# ------------------------------------------------------------
PROMPT = (
    "你是 WorkBuddy，一位 AP 数学诊断代理。学生会上传解题过程（可能是从 PDF/照片转录的文本）。"
    "你的任务不是解题，而是诊断学生的数学能力，帮助教师判断学生是否真正吸收了知识。\n\n"
    "覆盖范围：AP 数学全部内容（Calculus AB/BC、Precalculus、Statistics、代数基础）。\n\n"
    "铁律：\n"
    "1. 绝不直接给出标准答案、正确解法或最终结果；只给苏格拉底式问题、提示或「下一步该检查什么」。\n"
    "2. 按步骤顺序检查，找出最早出现错误的那一步；全对则 first_error.found = false。\n"
    "3. 把发现映射到八个能力维度，每维给 rating + evidence + confidence。\n"
    "4. confidence 是 0~1 小数。\n"
    "5. 若整体置信度 < 0.75，或薄弱(weak/developing)维度置信度 < 0.75，则 teacher_review_required = true。\n"
    "6. 只输出一个合法 JSON 对象，不要任何其他文字或 markdown。\n\n"
    "八个能力维度（顺序固定）：\n"
    "1. conceptual_understanding 概念理解  2. prerequisite_knowledge 前置知识\n"
    "3. reasoning_structure 推理结构  4. representation_translation 表征转换\n"
    "5. algebraic_execution 代数执行  6. transfer_ability 迁移能力\n"
    "7. independence_level 独立程度  8. memory_retention 记忆保持\n"
    "rating 取值：weak(未掌握)/developing(不稳定)/proficient(已吸收)/insufficient_evidence(证据不足)。\n\n"
    "输出 JSON 结构（dimensions 恰好 8 个、顺序与上面一致）：\n"
    '{"first_error":{"found":true,"step_index":3,"step_text":"...","summary":"..."},'
    '"dimensions":[{"id":"conceptual_understanding","rating":"weak","evidence":"...","confidence":0.9}],'
    '"overall_confidence":0.8,"teacher_review_required":false,"review_reason":null,'
    '"weaknesses":["..."],"recommended_actions":["苏格拉底式问题，绝不含答案"]}\n\n'
    "对输入的说明：若含多道题聚焦第一道；步骤混乱/缺失/字迹无法辨认时，如实降低 confidence 或标 insufficient_evidence，不要臆测。"
)

VALID_RATINGS = {"weak", "developing", "proficient", "insufficient_evidence"}
DIM_IDS = ["conceptual_understanding", "prerequisite_knowledge", "reasoning_structure",
           "representation_translation", "algebraic_execution", "transfer_ability",
           "independence_level", "memory_retention"]
DIM_LABELS = ["概念理解", "前置知识", "推理结构", "表征转换", "代数执行", "迁移能力", "独立程度", "记忆保持"]

# 示例诊断（供「看示例」按钮，无需 API Key 即可预览能力数据的样子）
EXAMPLE_DIAGNOSIS = {
    "first_error": {"found": True, "step_index": 3, "step_text": "u' = cos(x^2)",
                    "summary": "链式法则漏乘内层导数 2x"},
    "dimensions": [
        {"id": "conceptual_understanding", "rating": "weak", "evidence": "对 sin(x^2) 求导只写外层导数，未体现「外层求导 × 内层 2x」的复合结构。", "confidence": 0.88},
        {"id": "prerequisite_knowledge", "rating": "developing", "evidence": "内层导数 2x 属基础公式，复合情境下未自动调用。", "confidence": 0.80},
        {"id": "reasoning_structure", "rating": "proficient", "evidence": "正确识别乘法法则拆分 u、v，框架合理。", "confidence": 0.85},
        {"id": "representation_translation", "rating": "insufficient_evidence", "evidence": "纯符号求导，未涉及图形/数值/语言表征。", "confidence": 0.40},
        {"id": "algebraic_execution", "rating": "weak", "evidence": "漏乘内层导数因子 2x，属执行失误。", "confidence": 0.85},
        {"id": "transfer_ability", "rating": "insufficient_evidence", "evidence": "单一题目无法判断迁移能力。", "confidence": 0.40},
        {"id": "independence_level", "rating": "insufficient_evidence", "evidence": "未记录提示/重试信息。", "confidence": 0.45},
        {"id": "memory_retention", "rating": "developing", "evidence": "已学规则本次未正确调用；单次不足以定论。", "confidence": 0.50},
    ],
    "overall_confidence": 0.776,
    "teacher_review_required": True,
    "review_reason": "维度「概念理解」「代数执行」存在高置信度薄弱点；「记忆保持」置信度 0.50 低于 0.75。",
    "weaknesses": ["链式法则：对 sin(x^2) 求导漏乘内层导数 2x（第 3 步）"],
    "recommended_actions": ["求导 sin(x^2) 时，除了外层导数还要再乘上什么？（先说出来，不要写答案）", "补上漏掉的一步，并用自己的话解释为什么。"],
}

# 示例处方（供「看示例」按钮预览干预层）
EXAMPLE_PRESCRIPTION = {
    "interventions": ["big_tree", "three_questions"],
    "summary": "概念理解薄弱 → 用大树法则重建链式法则知识树；再通过三问阶梯巩固推理路径。",
    "big_tree": {
        "root_concept": "链式法则",
        "branches": {
            "定义": "复合函数 y=f(g(x)) 的导数 = 外层导数 × 内层导数",
            "定理/法则": "链式法则（复合函数求导）",
            "公式/规则": "d/dx f(g(x)) = f'(g(x)) · g'(x)",
            "应用": "sin(x^2)、cos(x^2)、e^(x^2) 等复合函数",
            "变式/边界": "内层为常数时无需链式法则；乘积+链式叠加时逐层不漏",
        },
        "broken_branch": "公式/规则 —— 漏乘内层导数 g'(x)",
    },
    "brain_training": None,
    "three_questions": {
        "questions": [
            "Q1：sin(x^2) 是复合函数吗？外层是什么、内层是什么？",
            "Q2：先对外层 sin 求导得到什么？还需要再乘上谁的导数？",
            "Q3：把外层导数和内层导数乘起来，完整结果是什么？",
        ],
    },
}

# 示例验算（sympy 确定性结果，非 LLM）
EXAMPLE_VERIFICATION = {
    "function": "sin(x**2)*exp(x)",
    "student_answer": "exp(x)*(cos(x**2)+sin(x**2))",
    "correct_derivative": "2*x*exp(x)*cos(x**2) + exp(x)*sin(x**2)",
    "student_matches": False,
    "note": "学生漏乘内层导数 2x，正确答案含 2x·cos(x²) —— 这是符号计算引擎（sympy）算出来的，不靠大模型猜。",
}
try:
    import sympy as _sp
    _x = _sp.symbols("x")
    EXAMPLE_VERIFICATION["function_latex"] = _sp.latex(_sp.sympify(EXAMPLE_VERIFICATION["function"]))
    EXAMPLE_VERIFICATION["answer_latex"] = _sp.latex(_sp.sympify(EXAMPLE_VERIFICATION["student_answer"]))
    EXAMPLE_VERIFICATION["correct_derivative_latex"] = _sp.latex(_sp.diff(_sp.sympify(EXAMPLE_VERIFICATION["function"]), _x))
except Exception:
    pass


def extract_pdf_text(data):
    """从 PDF 字节流抽取文本；扫描件会返回很少文本，需 OCR。"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(t)
        return "\n\n".join(parts).strip()
    except Exception as e:
        raise RuntimeError("PDF 文本抽取失败：%s。若是扫描件，请先 OCR 或用「粘贴文本」输入。" % e)


def _validate(obj):
    dims = obj.get("dimensions") or []
    if len(dims) != 8:
        raise ValueError("dimensions 必须恰好 8 个，实际 %d" % len(dims))
    for d in dims:
        if d.get("rating") not in VALID_RATINGS:
            raise ValueError("非法 rating: %r" % d.get("rating"))
        c = d.get("confidence", -1)
        if not isinstance(c, (int, float)) or not (0 <= c <= 1):
            raise ValueError("confidence 必须在 [0,1]: %r" % c)
    return obj


def diagnose_text(api_key, text, base_url="https://api.deepseek.com", model="deepseek-chat"):
    """调用 DeepSeek，用通用提示词诊断，返回校验后的能力数据 JSON。"""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": text}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("DeepSeek API HTTP %s: %s" % (e.code, e.read().decode("utf-8")[:300]))
    except urllib.error.URLError as e:
        raise RuntimeError("网络错误: %s" % e.reason)
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError("DeepSeek 响应异常: %s" % json.dumps(resp)[:300])
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError("DeepSeek 未返回合法 JSON: %s" % content[:300])
    return _validate(obj)


def _read_sibling(name):
    try:
        with open(os.path.join(_HERE, name), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


INTERVENTION_PROMPT = (
    "你是 WorkBuddy 的「干预处方」模块，已拿到学生诊断结果（八维+最早错误+置信度）。开出针对性训练处方，三种手段：\n"
    "1) 大树法则：围绕根概念建知识树（定义→定理/法则→公式/规则→应用→变式/边界），标出断掉的枝；治概念理解/前置知识/记忆保持。\n"
    "2) 脑力训练：工作记忆不足→数独/双n-back；注意力粗心→检查清单；迷思概念→点破旧直觉再用正确模型替换；治「明明知道却做错」。\n"
    "3) 三问阶梯：把大题拆成3个递进小问（Q1热身/Q2关键/Q3组装），只引导不给答案；治推理结构/迁移/独立程度。\n"
    "按诊断自动路由，最多2个按优先级排序，未使用的给 null。只输出合法 JSON："
    '{"interventions":["big_tree"],"summary":"...","big_tree":{"root_concept":"...","branches":{"定义":"...","定理/法则":"...","公式/规则":"...","应用":"...","变式/边界":"..."},"broken_branch":"..."},"brain_training":null,"three_questions":null}'
)


def prescribe(api_key, diagnosis, problem_text, base_url="https://api.deepseek.com", model="deepseek-chat"):
    """诊断之后：调用干预处方提示词，输出针对性训练处方。"""
    prompt = _read_sibling("ap_intervention_prompt.md") or INTERVENTION_PROMPT
    ctx = {"problem": problem_text, "diagnosis": diagnosis}
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("DeepSeek API HTTP %s: %s" % (e.code, e.read().decode("utf-8")[:300]))
    except urllib.error.URLError as e:
        raise RuntimeError("网络错误: %s" % e.reason)
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError("DeepSeek 响应异常: %s" % json.dumps(resp)[:300])
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError("DeepSeek 未返回合法 JSON: %s" % content[:300])


def sympy_verify(func_str, answer_str):
    """确定性验算通道：用 sympy 符号计算引擎精确校验学生答案（不依赖 LLM）。"""
    try:
        import sympy
    except ImportError:
        return {"error": "sympy 未安装，请 pip install sympy"}
    try:
        x = sympy.symbols("x")
        f = sympy.sympify(func_str)
        a = sympy.sympify(answer_str)
        d = sympy.diff(f, x)
        matches = bool(sympy.simplify(d - a) == 0)
        return {
            "function": func_str,
            "student_answer": answer_str,
            "correct_derivative": str(d),
            "correct_derivative_latex": sympy.latex(d),
            "function_latex": sympy.latex(f),
            "answer_latex": sympy.latex(a),
            "student_matches": matches,
        }
    except Exception as e:
        return {"error": "解析失败：%s（请用 sympy 语法，如 sin(x**2)*exp(x)）" % e}


# ------------------------------------------------------------
# 网页 UI（含雷达图 + 八维卡片）
# ------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>数学硕士助教 · AP 数学诊断</title>
<link rel="stylesheet" href="/static/katex/katex.min.css">
<script src="/static/katex/katex.min.js"></script>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--accent:#6366f1;--accent-ink:#4338ca;--good:#16a34a;--warn:#d97706;--bad:#dc2626;--none:#9ca3af}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",Roboto,sans-serif}
.page{max-width:1060px;margin:0 auto;padding:24px 20px 60px}
header{background:linear-gradient(135deg,#6366f1,#4338ca);color:#fff;border-radius:16px;padding:24px 28px;box-shadow:0 10px 30px rgba(67,56,202,.25)}
header h1{margin:0 0 4px;font-size:23px}header p{margin:2px 0;opacity:.92;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.card h2{margin:0 0 12px;font-size:16px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px;font-weight:600}
input[type=text],input[type=password],textarea{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:9px;font:inherit;font-size:14px}
textarea{min-height:140px;resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace}
button{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:11px 20px;font-size:15px;font-weight:600;cursor:pointer;margin-top:14px}
button:hover{background:var(--accent-ink)}button:disabled{opacity:.6;cursor:not-allowed}
.pill{display:inline-block;font-size:12px;padding:2px 10px;border-radius:999px;color:#fff}
.pill.good{background:var(--good)}.pill.warn{background:var(--warn)}.pill.bad{background:var(--bad)}.pill.none{background:var(--none)}
.conf{display:flex;align-items:baseline;gap:10px}.conf .num{font-size:30px;font-weight:700;color:var(--accent-ink)}
.gauge{flex:1;height:11px;background:#e5e7eb;border-radius:99px;overflow:hidden}.gauge>i{display:block;height:100%;background:linear-gradient(90deg,#6366f1,#4338ca)}
.grid{display:grid;grid-template-columns:320px 1fr;gap:18px;align-items:start}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.dim{border:1px solid var(--line);border-radius:11px;padding:11px 14px;margin-bottom:10px}
.dim .top{display:flex;align-items:center;gap:8px}.dim .label{font-weight:600;flex:1}
.dim .ev{color:var(--muted);font-size:13px;margin-top:4px}
.bar{height:6px;background:#e5e7eb;border-radius:99px;margin-top:7px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--accent)}
.meta{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:3px}
.review{border-radius:12px;padding:12px 16px;margin-top:14px}
.review.need{background:#fef2f2;border:1px solid #fecaca;color:#7f1d1d}.review.ok{background:#f0fdf4;border:1px solid #bbf7d0;color:#14532d}
ol.hints{margin:0;padding-left:20px}ol.hints li{margin:6px 0}
.hidden{display:none}
.err{color:var(--bad);font-weight:600}
#extract{margin-top:8px;font-size:13px;color:var(--muted);white-space:pre-wrap;max-height:180px;overflow:auto;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px}
.banner{background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:10px 14px;margin:6px 0 4px;color:#3730a3;font-size:14px;line-height:1.6}
.tree{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-top:12px;background:#fafbfe}
.troot{display:inline-block;background:linear-gradient(135deg,#6366f1,#4338ca);color:#fff;font-weight:700;padding:7px 16px;border-radius:999px;font-size:14px;margin-bottom:12px}
.branch{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-left:2px solid #e0e7ff;margin-left:12px;padding-left:14px}
.branch.broken{background:#fef2f2;border-left:2px solid var(--bad);border-radius:0 8px 8px 0}
.blabel{flex:0 0 88px;font-weight:700;font-size:12px;color:var(--accent-ink);background:#eef2ff;border-radius:6px;padding:3px 6px;text-align:center;align-self:flex-start}
.branch.broken .blabel{background:var(--bad);color:#fff}
.btext{flex:1;font-size:13.5px;line-height:1.65;color:#374151}
.ladder{margin-top:12px}
.rung{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px dashed var(--line)}
.rung:last-child{border-bottom:none}
.rnum{flex:0 0 26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px}
.qtext{flex:1;font-size:14px;line-height:1.65}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:1px 8px;border-radius:999px;margin-right:6px;color:#fff}
.tag.warm{background:var(--warn)}.tag.key{background:var(--accent)}.tag.assemble{background:var(--good)}
.fline{display:block;padding:6px 12px;margin:4px 0;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;line-height:1.7;color:#334155}
footer{margin-top:24px;color:var(--muted);font-size:13px;text-align:center}
</style>
</head>
<body>
<div class="page">
<header>
  <h1>数学硕士助教</h1>
  <p>美国应用数学硕士 · 逐行定位最早错误 → 八项能力诊断 → 训练处方 · 绝不给答案</p>
</header>

<div class="card">
  <h2>① 上传学生的解题过程</h2>
  <label>DeepSeek API Key（留空则用服务器环境变量）</label>
  <input type="password" id="key" placeholder="sk-...">
  <label>上传 PDF 作业/试卷</label>
  <input type="file" id="file" accept=".pdf">
  <div style="text-align:center;color:var(--muted);margin:8px 0">—— 或 ——</div>
  <label>粘贴解题文本（OCR 后或手打）</label>
  <textarea id="text" placeholder="例如：&#10;1. 用乘法法则：u = sin(x^2), v = e^x&#10;2. v' = e^x&#10;3. u' = cos(x^2)"></textarea>
  <button id="go">开始诊断</button>
  <button id="example" style="background:#fff;color:var(--accent);border:1px solid var(--accent);margin-left:10px">看示例（无需 Key）</button>
  <div id="extract" class="hidden"></div>
</div>

<div class="card">
  <h2>④ 确定性验算（sympy 外部工具 · 不靠大模型猜）</h2>
  <p style="color:var(--muted);font-size:13px;margin:0 0 4px">输入函数和学生作答，符号计算引擎会精确算出正确导数并判定对错——这就是「外部工具计算」通道。</p>
  <label>函数 f(x)</label><input type="text" id="vfunc" placeholder="sin(x**2)*exp(x)">
  <label>学生作答</label><input type="text" id="vans" placeholder="exp(x)*(cos(x**2)+sin(x**2))">
  <button id="verify">确定性验算</button>
  <div id="vresult" class="hidden"></div>
</div>

<div id="result" class="hidden"></div>

<footer>本助教只做诊断与辅导，最终判断与教学决策由教师完成。</footer>
</div>

<script>
var RATING_CN={weak:'未掌握',developing:'不稳定',proficient:'已吸收',insufficient_evidence:'证据不足'};
var LABELS={conceptual_understanding:'概念理解',prerequisite_knowledge:'前置知识',reasoning_structure:'推理结构',representation_translation:'表征转换',algebraic_execution:'代数执行',transfer_ability:'迁移能力',independence_level:'独立程度',memory_retention:'记忆保持'};
function $(id){return document.getElementById(id);}
function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}
function color(r){return {proficient:'good',developing:'warn',weak:'bad',insufficient_evidence:'none'}[r]||'none';}

function escapeHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function mathToHtml(s){
  s = s.replace(/\*\*/g, '^');
  s = s.replace(/exp\s*\(([^()]*)\)/g, 'e^($1)');
  s = s.replace(/\*/g, '·');
  s = s.replace(/\^\{([^}]*)\}/g, '<sup>$1</sup>');
  s = s.replace(/_\{([^}]*)\}/g, '<sub>$1</sub>');
  s = s.replace(/\^\(([^()]*)\)/g, '<sup>$1</sup>');
  s = s.replace(/\^([0-9A-Za-zπλ±∞\-])/g, '<sup>$1</sup>');
  s = s.replace(/_([0-9A-Za-zπλ±∞\-])/g, '<sub>$1</sub>');
  return s;
}

function katexRender(latex, display){
  try { return katex.renderToString(latex, {throwOnError:false, displayMode:!!display}); }
  catch(e){ return '<code>'+escapeHtml(latex)+'</code>'; }
}

function renderMathSegments(text){
  var out='', last=0, m;
  var re=/\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
  while((m=re.exec(text))!==null){
    out += mathToHtml(escapeHtml(text.slice(last, m.index)));
    out += katexRender(m[1]||m[2], !!m[1]);
    last = re.lastIndex;
  }
  out += mathToHtml(escapeHtml(text.slice(last)));
  return out;
}

function renderText(parent, text){
  if(!text) return;
  var hasLatex = /\$[^$\n]+\$/.test(String(text));
  var parts = String(text).split(/[；;]/).filter(function(s){return s.trim()!=='';});
  if(parts.length > 1){
    parts.forEach(function(seg){
      var line = el('div','fline');
      line.innerHTML = hasLatex ? renderMathSegments(seg.trim()) : mathToHtml(escapeHtml(seg.trim()));
      parent.appendChild(line);
    });
  } else {
    var span = el('span');
    span.innerHTML = hasLatex ? renderMathSegments(String(text)) : mathToHtml(escapeHtml(String(text)));
    parent.appendChild(span);
  }
}

function renderRadar(dims){
  var n=dims.length,cx=140,cy=140,R=112;
  var scale={weak:1,developing:2,proficient:3,insufficient_evidence:0.6};
  var ang=function(i){return -Math.PI/2+i*2*Math.PI/n;};
  var s='';
  for(var ring=1;ring<=3;ring++){var p=[];for(var i=0;i<n;i++){var a=ang(i),r=R*ring/3;p.push((cx+r*Math.cos(a)).toFixed(1)+','+(cy+r*Math.sin(a)).toFixed(1));}s+='<polygon points="'+p.join(' ')+'" fill="none" stroke="#e5e7eb"/>';}
  for(var i=0;i<n;i++){var a=ang(i);s+='<line x1="'+cx+'" y1="'+cy+'" x2="'+(cx+R*Math.cos(a)).toFixed(1)+'" y2="'+(cy+R*Math.sin(a)).toFixed(1)+'" stroke="#e5e7eb"/>';var lx=cx+(R+20)*Math.cos(a),ly=cy+(R+20)*Math.sin(a);s+='<text x="'+lx.toFixed(0)+'" y="'+ly.toFixed(0)+'" text-anchor="middle" dominant-baseline="middle" font-size="11" fill="#374151">'+(LABELS[dims[i].id]||dims[i].id)+'</text>';}
  var p=[];for(var i=0;i<n;i++){var a=ang(i),r=R*scale[dims[i].rating]/3;p.push((cx+r*Math.cos(a)).toFixed(1)+','+(cy+r*Math.sin(a)).toFixed(1));}
  s+='<polygon points="'+p.join(' ')+'" fill="rgba(99,102,241,.22)" stroke="#6366f1" stroke-width="2"/>';
  for(var i=0;i<n;i++){var a=ang(i),r=R*scale[dims[i].rating]/3;s+='<circle cx="'+(cx+r*Math.cos(a)).toFixed(1)+'" cy="'+(cy+r*Math.sin(a)).toFixed(1)+'" r="3" fill="#6366f1"/>';}
  return '<svg width="280" height="280" viewBox="0 0 280 280">'+s+'</svg>';
}

function render(dg){
  var w=el('div');
  var c1=el('div','card'); c1.appendChild(el('h2',null,'② 能力数据'));
  var conf=el('div','conf'); conf.appendChild(el('span','num',dg.overall_confidence));
  var g=el('div','gauge');var i=el('i');i.style.width=(dg.overall_confidence*100)+'%';g.appendChild(i);conf.appendChild(g);
  c1.appendChild(conf);
  if(dg.first_error && dg.first_error.found){
    c1.appendChild(el('div','err','✗ 最早错误：第 '+dg.first_error.step_index+' 步 —— '+(dg.first_error.summary||'')));
  } else {
    c1.appendChild(el('div',null,'✓ 未发现明显错误'));
  }
  if(dg.teacher_review_required){var rv=el('div','review need');rv.appendChild(el('b',null,'⚠ 需教师复核'));if(dg.review_reason)rv.appendChild(el('div',null,dg.review_reason));c1.appendChild(rv);}
  else{c1.appendChild(el('div','review ok','✓ 无需教师复核'));}
  w.appendChild(c1);

  var grid=el('div','grid');
  var rc=el('div','card');rc.appendChild(el('h2',null,'能力雷达'));rc.innerHTML+=renderRadar(dg.dimensions);
  var dc=el('div','card');dc.appendChild(el('h2',null,'八个能力维度'));
  dg.dimensions.forEach(function(d){
    var box=el('div','dim');
    var top=el('div','top');top.appendChild(el('span','label',LABELS[d.id]||d.id));top.appendChild(el('span','pill '+color(d.rating),RATING_CN[d.rating]));box.appendChild(top);
    var ev=el('div','ev'); renderText(ev, d.evidence); box.appendChild(ev);
    var bar=el('div','bar');var f=el('i');f.style.width=(d.confidence*100)+'%';bar.appendChild(f);box.appendChild(bar);
    var m=el('div','meta');m.appendChild(el('span',null,'置信度 '+d.confidence));box.appendChild(m);
    dc.appendChild(box);
  });
  grid.appendChild(rc);grid.appendChild(dc);w.appendChild(grid);

  if(dg.weaknesses && dg.weaknesses.length){
    var c3=el('div','card');c3.appendChild(el('h2',null,'薄弱点'));c3.appendChild(el('div',null,dg.weaknesses.join('；')));w.appendChild(c3);
  }
  if(dg.recommended_actions && dg.recommended_actions.length){
    var c4=el('div','card');c4.appendChild(el('h2',null,'辅导建议（苏格拉底式，不直接给答案）'));
    var ol=el('ol','hints');dg.recommended_actions.forEach(function(x){ol.appendChild(el('li',null,x));});c4.appendChild(ol);w.appendChild(c4);
  }
  return w;
}

function parseQuestion(q,i){
  var m=q.match(/^Q\s*(\d+)\s*(热身|关键|组装)?\s*[：:]\s*(.*)$/);
  if(m) return {num:m[1], tag:m[2]||null, text:m[3]};
  var t=q.replace(/^Q\s*\d+\s*[：:]?\s*/,'');
  return {num:String(i+1), tag:null, text:t||q};
}

function renderRx(p){
  if(!p) return el('div');
  var wrap=el('div');

  var head=el('div','card');
  head.appendChild(el('h2',null,'③ 干预处方（诊断之后怎么补）'));
  if(p.summary){
    var b=el('div','banner');
    b.appendChild(el('b',null,'为什么：'));
    b.appendChild(document.createTextNode(p.summary));
    head.appendChild(b);
  }
  wrap.appendChild(head);

  if(p.big_tree){
    var t=el('div','card');
    t.appendChild(el('h2',null,'🌳 大树法则（重建知识树）'));
    var tree=el('div','tree');
    tree.appendChild(el('span','troot', p.big_tree.root_concept||'概念'));
    var branches=p.big_tree.branches||{};
    var broken=p.big_tree.broken_branch||'';
    Object.keys(branches).forEach(function(k){
      var isBroken = broken.indexOf(k)===0;
      var row=el('div','branch'+(isBroken?' broken':''));
      row.appendChild(el('span','blabel', k));
      var txt=el('span','btext');
      if(isBroken){ txt.appendChild(document.createTextNode('⚠ ')); }
      renderText(txt, branches[k]);
      row.appendChild(txt);
      tree.appendChild(row);
    });
    if(broken){
      var fb=el('div','err','✗ 断掉的枝：'+broken);
      fb.style.marginTop='10px';
      tree.appendChild(fb);
    }
    t.appendChild(tree);
    wrap.appendChild(t);
  }

  if(p.brain_training){
    var g=el('div','card');
    g.appendChild(el('h2',null,'🧠 脑力训练'));
    var line='目标：'+p.brain_training.target+' · 活动：'+p.brain_training.activity;
    if(p.brain_training.dose) line+=' · 剂量：'+p.brain_training.dose;
    g.appendChild(el('div',null,line));
    if(p.brain_training.misconception_to_replace) g.appendChild(el('div',null,'要替换的「旧版本想法」：'+p.brain_training.misconception_to_replace));
    wrap.appendChild(g);
  }

  if(p.three_questions && p.three_questions.questions){
    var l=el('div','card');
    l.appendChild(el('h2',null,'🪜 三问阶梯（答得出三问，就能解出原题）'));
    var ladder=el('div','ladder');
    p.three_questions.questions.forEach(function(q,i){
      var pq=parseQuestion(q,i);
      var r=el('div','rung');
      r.appendChild(el('span','rnum', pq.num));
      var tb=el('span','qtext');
      if(pq.tag){ tb.appendChild(el('span','tag '+(pq.tag==='热身'?'warm':(pq.tag==='关键'?'key':'assemble')), pq.tag)); }
      renderText(tb, pq.text);
      r.appendChild(tb);
      ladder.appendChild(r);
    });
    l.appendChild(ladder);
    wrap.appendChild(l);
  }

  return wrap;
}

function renderVerify(v){
  var c=el('div','card');
  c.appendChild(el('h2',null,'④ 确定性验算（sympy 外部工具）'));
  if(v.error){c.appendChild(el('div','err',v.error));return c;}

  var r1=el('div'); r1.appendChild(el('b',null,'f(x) = '));
  var s1=el('span'); s1.innerHTML = v.function_latex ? katexRender(v.function_latex,false) : mathToHtml(escapeHtml(v.function)); r1.appendChild(s1);
  r1.appendChild(document.createTextNode('　　学生作答 = '));
  var s2=el('span'); s2.innerHTML = v.answer_latex ? katexRender(v.answer_latex,false) : mathToHtml(escapeHtml(v.student_answer)); r1.appendChild(s2);
  c.appendChild(r1);

  c.appendChild(el('div', v.student_matches?'review ok':'review need', v.student_matches?'✓ 学生作答正确':'✗ 学生作答错误（与正确导数不一致）'));

  var r2=el('div'); r2.appendChild(el('b',null,'正确导数 = '));
  var s3=el('span'); s3.innerHTML = v.correct_derivative_latex ? katexRender(v.correct_derivative_latex,true) : mathToHtml(escapeHtml(v.correct_derivative)); r2.appendChild(s3);
  c.appendChild(r2);

  if(v.note)c.appendChild(el('div',null,'说明：'+v.note));
  return c;
}

$('verify').onclick=function(){
  var f=$('vfunc').value.trim(), a=$('vans').value.trim();
  if(!f||!a){alert('请填写函数和学生作答');return;}
  fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({func:f,answer:a})})
  .then(function(r){return r.json();}).then(function(res){
    var v=$('vresult');v.innerHTML='';v.classList.remove('hidden');v.appendChild(renderVerify(res));
  });
};

$('go').onclick=function(){
  var key=$('key').value.trim(), file=$('file').files[0], text=$('text').value.trim();
  if(!file && !text){alert('请上传 PDF 或粘贴文本');return;}
  var payload={api_key:key, mode:file?'pdf':'text'};
  if(file){var rd=new FileReader();rd.onload=function(){payload.base64=rd.result.split(',')[1];send();};rd.readAsDataURL(file);}
  else{payload.text=text;send();}
  function send(){
    $('go').disabled=true;$('go').textContent='诊断中…';
    $('result').classList.add('hidden');$('extract').classList.add('hidden');
    fetch('/api/diagnose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(function(r){return r.json();})
    .then(function(res){
      $('go').disabled=false;$('go').textContent='开始诊断';
      if(res.error){alert(res.error);return;}
      if(res.extracted){
        var ex=$('extract');ex.textContent='已扫描文字（共 '+res.extracted.length+' 字）：\n'+res.extracted.slice(0,800)+(res.extracted.length>800?' …':'');ex.classList.remove('hidden');
      }
      $('result').innerHTML='';$('result').appendChild(render(res.diagnosis));
      if(res.prescription){$('result').appendChild(renderRx(res.prescription));}
      $('result').classList.remove('hidden');
    }).catch(function(e){$('go').disabled=false;$('go').textContent='开始诊断';alert('请求失败：'+e);});
  }
};

$('example').onclick=function(){
  fetch('/api/example').then(function(r){return r.json();}).then(function(res){
    $('result').innerHTML='';$('result').appendChild(render(res.diagnosis));
    if(res.prescription){$('result').appendChild(renderRx(res.prescription));}
    if(res.verification){$('result').appendChild(renderVerify(res.verification));}
    $('result').classList.remove('hidden');
    $('extract').classList.add('hidden');
  });
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, relpath):
        full = os.path.normpath(os.path.join(_HERE, relpath))
        if not full.startswith(os.path.join(_HERE, "static")):
            return False
        if not os.path.isfile(full):
            return False
        ext = os.path.splitext(full)[1].lower()
        ctype = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if path.startswith("/static/"):
            if self._serve_static(path.lstrip("/")):
                return
            return self._send(404, {"error": "not found"})
        if path == "/api/example":
            return self._send(200, {"diagnosis": EXAMPLE_DIAGNOSIS, "prescription": EXAMPLE_PRESCRIPTION,
                                    "verification": EXAMPLE_VERIFICATION})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._send(400, {"error": "bad request: %s" % e})

        if path == "/api/verify":
            return self._send(200, sympy_verify(body.get("func") or "", body.get("answer") or ""))

        if path != "/api/diagnose":
            return self._send(404, {"error": "not found"})

        api_key = (body.get("api_key") or "").strip() or os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return self._send(400, {"error": "缺少 DeepSeek API Key（请在页面填写或设置 DEEPSEEK_API_KEY）"})

        extracted = ""
        try:
            if body.get("mode") == "pdf":
                raw = base64.b64decode(body.get("base64") or "")
                extracted = extract_pdf_text(raw)
            else:
                extracted = (body.get("text") or "").strip()
            if not extracted.strip():
                return self._send(400, {"error": "未抽取到文本（扫描件请先 OCR，或改用粘贴文本）"})

            base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
            diag = diagnose_text(api_key, extracted, base_url=base_url, model=model)
            try:
                presc = prescribe(api_key, diag, extracted, base_url=base_url, model=model)
            except Exception:
                presc = None
        except Exception as e:
            return self._send(500, {"error": str(e)})

        return self._send(200, {"diagnosis": diag, "prescription": presc, "extracted": extracted})

    def log_message(self, *args):
        pass


def main():
    host = os.environ.get("WORKBUDDY_HOST", "127.0.0.1")
    port = int(os.environ.get("WORKBUDDY_PORT", "8081"))
    server = ThreadingHTTPServer((host, port), Handler)
    print("=" * 56)
    print("  数学硕士助教 · AP 数学诊断")
    print("  请在浏览器打开： http://%s:%d" % ("localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port))
    print("  对外部署：WORKBUDDY_HOST=0.0.0.0 python3 pdf_diagnose.py")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
