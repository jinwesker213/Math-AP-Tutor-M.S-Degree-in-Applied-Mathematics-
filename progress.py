"""跨次提交的进步追踪：判断上一轮诊断出的弱点是否真正改善。

对应 MVP 成功标准第二条：教师确认诊断后，系统通过下一次作答判断弱点是否改善。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from .models import Diagnosis

RATING_ORDINAL = {"weak": 0, "developing": 1, "proficient": 2}


def compare(prev: Diagnosis, curr: Diagnosis) -> Dict[str, Any]:
    prev_map = {d.id: d for d in prev.dimensions}
    curr_map = {d.id: d for d in curr.dimensions}

    per_dim: List[Dict[str, Any]] = []
    for d in curr.dimensions:
        p = prev_map.get(d.id)
        pr = p.rating if p else None
        cr = d.rating
        if pr in RATING_ORDINAL and cr in RATING_ORDINAL:
            delta = RATING_ORDINAL[cr] - RATING_ORDINAL[pr]
            verdict = "improved" if delta > 0 else ("regressed" if delta < 0 else "stable")
        elif pr == "insufficient_evidence" and cr in RATING_ORDINAL:
            verdict = "now_assessed"
        elif pr is None:
            verdict = "not_comparable"
        else:
            verdict = "stable"  # 两次都证据不足
        per_dim.append({
            "id": d.id,
            "label": d.label,
            "previous_rating": pr,
            "current_rating": cr,
            "verdict": verdict,
        })

    prev_weak = {d.id for d in prev.dimensions if d.rating in ("weak", "developing")}
    curr_proficient = {d.id for d in curr.dimensions if d.rating == "proficient"}
    curr_weak = {d.id for d in curr.dimensions if d.rating in ("weak", "developing")}

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "student_id": curr.meta.get("student_id"),
        "previous_submission_id": prev.meta.get("submission_id"),
        "current_submission_id": curr.meta.get("submission_id"),
        "dimension_deltas": per_dim,
        "weaknesses_resolved": sorted(prev_weak & curr_proficient),
        "weaknesses_persisting": sorted(prev_weak - curr_proficient),
        "new_weaknesses": sorted(curr_weak - prev_weak),
    }


def load_diagnosis(path: str) -> Diagnosis:
    with open(path, "r", encoding="utf-8") as f:
        return Diagnosis.from_dict(json.load(f))


def save_progress(result: Dict[str, Any], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
