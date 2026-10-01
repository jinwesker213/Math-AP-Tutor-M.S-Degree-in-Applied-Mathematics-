# WorkBuddy 诊断契约（API 请求 / 返回 JSON Schema）

> 与 `scripts/workbuddy/models.py`、`workbuddy_tool_schema.json` 同源。
> API 模式（按次计费）下，`POST /diagnose` 即对 `workbuddy_diagnose` 工具的完整实现。

## 1. 请求参数（对齐 workbuddy_diagnose）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `problem` | string | 是 | 题目原文 |
| `steps` | string[] | 是 | 学生解题步骤，按顺序，每步一个字符串（原文，不要改写） |
| `final_answer` | string | 否 | 学生的最终作答 |
| `previous_diagnosis` | object | 否 | 该生上一轮的诊断结果；传入则返回 `improvement` 进步对比 |

## 2. 返回结构（诊断 JSON，dimensions 恰好 8 个、顺序固定）

```json
{
  "schema_version": "1.0",
  "meta": {
    "mode": "offline | api",
    "model": "deepseek-chat",
    "generated_at": "ISO 时间"
  },
  "first_error": {
    "found": true,
    "step_index": 3,
    "step_text": "u' = cos(x^2)",
    "summary": "链式法则漏乘内层导数 2x"
  },
  "dimensions": [
    {"id": "conceptual_understanding", "label": "概念理解", "rating": "weak", "evidence": "……", "confidence": 0.9}
  ],
  "overall_confidence": 0.8,
  "teacher_review_required": false,
  "review_reason": null,
  "weaknesses": ["……"],
  "recommended_actions": ["苏格拉底式问题，绝不含答案"]
}
```

### 字段说明
- `first_error.step_index`：最早错误步骤（从 1 起）；全部正确时 `found=false`，step_index/step_text 为 null。
- `dimensions`：8 项，顺序固定为 概念理解 → 前置知识 → 推理结构 → 表征转换 → 代数执行 → 迁移能力 → 独立程度 → 记忆保持；rating 取值见 `diagnosis-rubric.md`。
- `confidence`：0~1 浮点数，表示对该维度判断的把握程度。
- `teacher_review_required`：满足任一条件时为 true：
  1. `overall_confidence` < 0.75；
  2. 存在 weak/developing 维度且其 confidence < 0.75；
  3. 存在高置信度（≥0.75）的 weak 维度。
- `recommended_actions`：只含引导式问题/提示，**绝不包含答案**。

## 3. 复测进步对比（传入 previous_diagnosis 时附加）

```json
{
  "improvement": {
    "dimension_deltas": [
      {"id": "algebraic_execution", "label": "代数执行", "previous_rating": "weak", "current_rating": "proficient", "verdict": "improved"}
    ],
    "weaknesses_resolved": ["algebraic_execution"],
    "weaknesses_persisting": [],
    "new_weaknesses": []
  }
}
```

- `verdict`：improved（改善）/ regressed（退步）/ stable（稳定）/ now_assessed（从证据不足转为可评估）/ not_comparable。
- 维度评级序：weak(0) < developing(1) < proficient(2)。

## 4. 离线引擎范围声明（meta.mode = "offline"）

未配置 `DEEPSEEK_API_KEY` 时，使用 `scripts/workbuddy/analyzer.py` 的确定性启发式引擎，
当前仅编目 AP 微积分「求导」常见错误：
1. 链式法则遗漏内层导数（如 sin(x^2) 写成 cos(x^2)）；
2. 把 e^x 当幂函数求导（写成 x·e^(x-1)）。

超出编目范围的题目会返回「未发现已编目错误」并给出完整路径假设——生产环境请配置
`DEEPSEEK_API_KEY` 走 LLM 全题型诊断（meta.mode = "api"）。
