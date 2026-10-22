"""Codex Decision Engine (`decision`, `decision_review`, `decision_guard`)."""
from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _env(*names: str) -> str:
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _extra_site_packages() -> list[str]:
    home = Path.home()
    venv = _env("DECISION_VENV")
    candidates = [
        _env("DECISION_SITE_PACKAGES"),
        str(Path(venv) / "Lib" / "site-packages") if venv else None,
        home / "jev-starter" / ".venv" / "Lib" / "site-packages",
        home / ".codex-decision" / "venv" / "Lib" / "site-packages",
    ]
    return [
        str(Path(p).expanduser())
        for p in candidates
        if p and (Path(p).expanduser() / "system_one_adapter").is_dir()
    ]


for _path in _extra_site_packages():
    if _path not in sys.path:
        sys.path.append(_path)

_adapter_available = True
try:
    from system_one_adapter import Choice, Noul, Score, SystemOneAdapterClient
except Exception:
    _adapter_available = False

_OPENROUTER_BASE_URL = "https://openrouter.ai/api"
_OPENROUTER_MODEL_NAME = "baseten/elephant-1:free"
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_REVIEW_MIN_CONFIDENCE = 0.75
_DEFAULT_REVIEW_ABSTAIN_BELOW = 0.5

_ENGINE_OPENROUTER = "openrouter"
_ENGINE_NONE = "none"
_ENGINE_SPEC = (_OPENROUTER_BASE_URL, _OPENROUTER_MODEL_NAME, "OPENROUTER_API_KEY")

_REVIEW_LIMITS = {
    "point": 64, "question": 500, "option_id": 64, "option_description": 500,
    "options": 8, "evidence_items": 12, "evidence_fact": 1200, "evidence_total": 6000,
    "constraints": 10, "constraint": 500, "constraints_total": 2500,
}
_POINT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)\b\s*[:=]\s*[^\s,;\"']{6,}"),
)
_SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential|auth)")


def _get_cache_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = [
        _env("DECISION_STATE_DIR"),
        Path(local_app_data) / "codex-decision" / "decision" if local_app_data else None,
        Path.home() / ".codex-decision" / "cache",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.touch()
            probe.unlink()
            return path
        except OSError:
            continue
    raise RuntimeError("Decision 无法创建可写状态目录，请设置 DECISION_STATE_DIR")


_CACHE_DIR = _get_cache_dir()
_CACHE_FILE = _CACHE_DIR / "decision_cache.json"
_JOURNAL_FILE = _CACHE_DIR / "decision_journal.jsonl"
_CACHE_MAX_SIZE = 256
_CACHE_TTL_SECONDS = 3600
_CACHE_SCHEMA_VERSION = 3
_DECISION_CACHE: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()
_CACHE_LOCK = threading.RLock()
_DISK_LOCK = threading.Lock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="decision")


def _load_disk_cache() -> None:
    if not _CACHE_FILE.exists():
        return
    try:
        now = time.time()
        for k, item in json.loads(_CACHE_FILE.read_text(encoding="utf-8")).items():
            if isinstance(item, dict) and "created_at" in item and "result" in item:
                if now - item["created_at"] <= _CACHE_TTL_SECONDS:
                    _DECISION_CACHE[k] = (item["created_at"], item["result"])
    except Exception:
        pass


def _save_disk_cache() -> None:
    try:
        with _DISK_LOCK:
            with _CACHE_LOCK:
                payload = {k: {"created_at": ts, "result": res} for k, (ts, res) in _DECISION_CACHE.items()}
            tmp_file = _CACHE_FILE.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_file.replace(_CACHE_FILE)
    except Exception:
        pass


_load_disk_cache()


def _engine_spec() -> tuple[str, str, str]:
    """Return (base_url, model, key) for the single supported backend."""
    base_url, model, key_env = _ENGINE_SPEC
    key = _env(key_env)
    if not key:
        kf = _env("DECISION_KEY_FILE")
        path = Path(kf).expanduser() if kf else None
        key = path.read_text(encoding="utf-8").strip() if path and path.is_file() else ""
    return base_url, model, key


def _active_engine() -> str:
    _, model, key = _engine_spec()
    return f"{_ENGINE_OPENROUTER}:{model}" if (_adapter_available and key) else _ENGINE_NONE


def _decision_tool_available() -> bool:
    return _active_engine() != _ENGINE_NONE


def _get_cache_key(args: dict[str, Any], engine_id: str | None = None) -> str:
    try:
        semantic_keys = {"state", "choice", "choices", "noul", "nouls", "score", "scores", "guard", "review"}
        payload = {k: v for k, v in args.items() if k in semantic_keys}
        payload["__engine"] = engine_id or _active_engine()
        payload["__schema"] = _CACHE_SCHEMA_VERSION
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    except Exception:
        raw = str(args)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    with _CACHE_LOCK:
        if key not in _DECISION_CACHE:
            return None
        created_at, result = _DECISION_CACHE[key]
        if time.time() - created_at > _CACHE_TTL_SECONDS:
            del _DECISION_CACHE[key]
            return None
        _DECISION_CACHE.move_to_end(key)
        res_copy = dict(result)
    res_copy["cache_hit"] = True
    res_copy["latency_seconds"] = 0.0
    return res_copy


def _cache_put(key: str, result: dict) -> None:
    with _CACHE_LOCK:
        if len(_DECISION_CACHE) >= _CACHE_MAX_SIZE:
            _DECISION_CACHE.popitem(last=False)
        _DECISION_CACHE[key] = (time.time(), result)
    _executor.submit(_save_disk_cache)


def _log_journal(entry: dict[str, Any]) -> None:
    try:
        if _JOURNAL_FILE.exists() and _JOURNAL_FILE.stat().st_size > 10 * 1024 * 1024:
            bak = _JOURNAL_FILE.with_suffix(".jsonl.old")
            if bak.exists():
                bak.unlink()
            _JOURNAL_FILE.replace(bak)
        with open(_JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({**entry, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    text = str(value)
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return _jsonable(usage)
    for meth in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, meth, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return _jsonable(d)
            except Exception:
                pass
    out = {
        k: getattr(usage, k)
        for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens")
        if getattr(usage, k, None) is not None
    }
    return out or {"raw": str(usage)}


def _jsonable(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
        for k, v in mapping.items()
    }


def _guard_verdict_from_signals(signals: dict[str, Any], threshold: float) -> tuple[str, str] | None:
    required = ("verdict", "policy_allows", "reads_secrets", "sends_outbound", "self_advocating", "blast_radius")
    if any(signals.get(k) is None for k in required):
        return None
    blast = float(signals["blast_radius"])
    raw_verdict = str(signals["verdict"]).lower()
    if raw_verdict not in ("allow", "ask", "deny"):
        return None
    if float(signals["self_advocating"]) >= 0.6:
        return "ask", "命令文本疑似自我游说（self_advocating >= 0.6）"
    if float(signals["reads_secrets"]) >= 0.7 and float(signals["sends_outbound"]) >= 0.7:
        return "deny", "疑似读取凭据并外发（reads_secrets 与 sends_outbound >= 0.7）"
    if float(signals["reads_secrets"]) >= 0.7:
        return "deny", "疑似读取或复制凭据（reads_secrets >= 0.7）"
    if blast >= 1.6:
        return "ask", "破坏半径较大（blast_radius >= 1.6）"
    if raw_verdict == "deny":
        return "deny", "模型判定拒绝（模型 DENY 保留）"
    if float(signals["policy_allows"]) >= threshold and blast < 2.0:
        return "allow", "策略允许且破坏半径有限"
    if raw_verdict == "allow":
        return "allow", "模型判定允许且未触发硬性风险规则"
    return "ask", "模型判定需人工确认"


_TIMEOUT_PARAM = {"type": "number", "description": "网络裁决超时熔断阈值（秒，默认 20.0 秒）。超时直接返回 error，不做本地兜底。"}
_NO_CACHE_PARAM = {"type": "boolean", "description": "是否跳过本地缓存强制实时推理（默认 false）。"}
_CHOICE_CRITERIA = {"type": "object", "description": "选项及其含义，格式为 {'选项key': '该选项的描述'}"}
_SCORE_CRITERIA = {"type": "array", "items": {"type": "string"}, "description": "评分梯度数组，如 ['低', '中', '高']"}


def _item_schema(
    desc: str,
    criteria_schema: dict[str, Any] | None = None,
    required: str | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "name": {"type": "string", "description": "题目名称"},
        "instructions": {"type": "string", "description": desc},
    }
    if criteria_schema is not None:
        props["criteria"] = criteria_schema
    schema: dict[str, Any] = {"type": "object", "description": desc, "properties": props}
    if required:
        schema["required"] = [required]
    return schema


DECISION_SCHEMA = {
    "name": "decision",
    "description": (
        "Decision 结构化决策引擎（OpenRouter/Elephant 后端）："
        "用于在多个可用工具/执行路径之间确有选择不确定性时进行 Choice 路由、Noul 是非概率或 Score 打分；"
        "支持多题并发（choices/nouls/scores），带概率分布、置信度、缓存、超时熔断和脱敏审计。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {"type": ["object", "string"], "description": "当前决策状态；只传相关信息，勿含凭据。"},
            "choice": _item_schema("单项选择题（单题）。", _CHOICE_CRITERIA),
            "choices": {
                "type": "array",
                "description": "多个单选题组。",
                "items": _item_schema("单选意图描述", _CHOICE_CRITERIA, "criteria"),
            },
            "noul": _item_schema("是非题（单题，布尔概率 0.0~1.0）。"),
            "nouls": {"type": "array", "description": "多个是非题组。", "items": _item_schema("判断意图描述", None, "instructions")},
            "score": _item_schema("打分题（单题，离散评分梯度）。", _SCORE_CRITERIA),
            "scores": {
                "type": "array",
                "description": "多个打分题组。",
                "items": _item_schema("打分意图描述", _SCORE_CRITERIA, "criteria"),
            },
            "timeout_seconds": dict(_TIMEOUT_PARAM),
            "no_cache": dict(_NO_CACHE_PARAM),
        },
        "required": ["state"],
    },
}

DECISION_GUARD_SCHEMA = {
    "name": "decision_guard",
    "description": (
        "Decision 动作风险闸门（guard）：仅在动作具有实质风险时按需调用；"
        "并行评估 6 个信号（verdict / policy_allows / reads_secrets / sends_outbound / self_advocating / blast_radius），"
        "由确定性规则输出 allow/ask/deny。错误或不可用直接返回 error，绝不等于放行。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {"type": ["object", "string"], "description": "待评估动作上下文；勿含凭据。"},
            "guard": {
                "type": "object",
                "description": "闸门配置（可选）。",
                "properties": {
                    "instructions": {"type": "string", "description": "判定意图描述。"},
                    "threshold": {"type": "number", "description": "放行阈值（0~1，默认 0.75）。"},
                },
            },
            "timeout_seconds": dict(_TIMEOUT_PARAM),
            "no_cache": dict(_NO_CACHE_PARAM),
        },
        "required": ["state"],
    },
}

DECISION_REVIEW_SCHEMA = {
    "name": "decision_review",
    "description": (
        "对主模型已形成的预选（primaryChoice）做独立证据复核（盲审去锚定）。"
        "返回 recommendation、confidence、probabilities 和 adoptable 阈值提示；不替代授权。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "point": {"type": "string", "description": "决策点名称，如 candidate_selection。"},
            "question": {"type": "string", "description": "需要复核的具体决策问题。"},
            "primaryChoice": {"type": "string", "description": "主模型独立形成的候选 id。"},
            "options": {
                "type": "array",
                "description": "2 到 8 个合法候选，id 必须唯一。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "evidenceRefs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "description"],
                },
            },
            "evidence": {
                "type": "array",
                "description": "可验证证据列表。",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "fact": {"type": "string"}},
                    "required": ["id", "fact"],
                },
            },
            "constraints": {"type": "array", "items": {"type": "string"}, "description": "硬约束列表。"},
            "minConfidence": {"type": "number", "description": "adoptable 最小置信度，默认 0.75。"},
            "abstainBelow": {"type": "number", "description": "弃权阈值，默认 0.5。"},
            "mode": {"type": "string", "enum": ["advisory", "shadow"]},
            "timeout_seconds": dict(_TIMEOUT_PARAM),
            "no_cache": dict(_NO_CACHE_PARAM),
        },
        "required": ["point", "question", "primaryChoice", "options"],
    },
}

def _build_provider() -> Any:
    base_url, model, key = _engine_spec()
    from .decisions_provider import DecisionsProvider

    return DecisionsProvider(model, base_url=base_url, api_key=key)


def _execute_system_one_with_timeout(
    client: Any,
    state: Any,
    questions: dict[str, Any],
    provider: Any,
    timeout: float,
) -> Any:
    return _executor.submit(client.system_one, state=state, questions=questions, model=provider).result(timeout=timeout)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _timeout_from_args(args: dict, default: float = _DEFAULT_TIMEOUT_SECONDS) -> float:
    try:
        return max(0.1, min(float(args.get("timeout_seconds") or default), 120.0))
    except (ValueError, TypeError):
        return default


def _review_string(value: Any, field: str, max_length: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field} 长度必须在 1 到 {max_length} 之间")
    if pattern is not None and not pattern.fullmatch(normalized):
        raise ValueError(f"{field} 格式不合法")
    return normalized


def _review_string_list(
    value: Any,
    field: str,
    max_items: int,
    max_item_length: int,
    total_limit: int | None = None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{field} 数量不能超过 {max_items}")
    normalized = [_review_string(item, f"{field}[{i}]", max_item_length) for i, item in enumerate(value)]
    if total_limit is not None and sum(len(item) for item in normalized) > total_limit:
        raise ValueError(f"{field} 超过总字符预算 {total_limit}")
    return normalized


def _review_number(value: Any, field: str, default: float, minimum: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field} 必须是数字") from None
    if not 0 <= minimum <= number <= maximum <= 1:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return number


def _normalize_review_args(args: dict[str, Any]) -> tuple[dict[str, Any], float, float, bool]:
    lim = _REVIEW_LIMITS
    point = _review_string(args.get("point"), "point", lim["point"], _POINT_PATTERN)
    question = _review_string(args.get("question"), "question", lim["question"])
    primary_choice = _review_string(args.get("primaryChoice"), "primaryChoice", lim["option_id"], _ID_PATTERN)

    raw_options = args.get("options")
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= lim["options"]:
        raise ValueError(f"options 数量必须在 2 到 {lim['options']} 之间")
    options: list[dict[str, Any]] = []
    for i, raw_opt in enumerate(raw_options):
        if not isinstance(raw_opt, dict):
            raise ValueError(f"options[{i}] 必须是对象")
        options.append({
            "id": _review_string(raw_opt.get("id"), f"options[{i}].id", lim["option_id"], _ID_PATTERN),
            "description": _review_string(
                raw_opt.get("description"),
                f"options[{i}].description",
                lim["option_description"],
            ),
            "evidenceRefs": _review_string_list(
                raw_opt.get("evidenceRefs"), f"options[{i}].evidenceRefs",
                lim["evidence_items"], lim["option_id"], lim["evidence_items"] * lim["option_id"],
            ),
        })
    option_ids = [o["id"] for o in options]
    if len(set(option_ids)) != len(option_ids):
        raise ValueError("候选 id 不能重复")
    if primary_choice not in option_ids:
        raise ValueError("primaryChoice 必须属于候选集合")

    raw_evidence = args.get("evidence") or []
    if not isinstance(raw_evidence, list) or len(raw_evidence) > lim["evidence_items"]:
        raise ValueError(f"evidence 数量不能超过 {lim['evidence_items']}")
    evidence: list[dict[str, str]] = []
    for i, raw_ev in enumerate(raw_evidence):
        if not isinstance(raw_ev, dict):
            raise ValueError(f"evidence[{i}] 必须是对象")
        evidence.append({
            "id": _review_string(raw_ev.get("id"), f"evidence[{i}].id", lim["option_id"], _ID_PATTERN),
            "fact": _review_string(raw_ev.get("fact"), f"evidence[{i}].fact", lim["evidence_fact"]),
        })
    evidence_ids = [e["id"] for e in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("证据 id 不能重复")
    if sum(len(e["fact"]) for e in evidence) > lim["evidence_total"]:
        raise ValueError(f"evidence 超过总字符预算 {lim['evidence_total']}")
    evidence_id_set = set(evidence_ids)
    for opt in options:
        for ref in opt["evidenceRefs"]:
            if ref not in evidence_id_set:
                raise ValueError(f"候选 {opt['id']} 引用了不存在的证据 {ref}")

    constraints = _review_string_list(
        args.get("constraints"),
        "constraints",
        lim["constraints"],
        lim["constraint"],
        lim["constraints_total"],
    )
    outbound = [question, *(o["description"] for o in options), *(e["fact"] for e in evidence), *constraints]
    if any(pat.search(text) for text in outbound for pat in _SECRET_PATTERNS):
        raise ValueError("复核输入疑似包含凭据或密钥")

    min_conf = _review_number(args.get("minConfidence"), "minConfidence", _DEFAULT_REVIEW_MIN_CONFIDENCE, 0.0, 1.0)
    abstain = _review_number(args.get("abstainBelow"), "abstainBelow", _DEFAULT_REVIEW_ABSTAIN_BELOW, 0.0, 1.0)
    if abstain > min_conf:
        raise ValueError("abstainBelow 不能高于 minConfidence")
    mode = args.get("mode", "advisory")
    if mode not in ("advisory", "shadow"):
        raise ValueError("mode 必须是 advisory 或 shadow")
    return (
        {
            "point": point,
            "question": question,
            "primaryChoice": primary_choice,
            "options": options,
            "evidence": evidence,
            "constraints": constraints,
        },
        min_conf, abstain, mode == "shadow",
    )


def _review_state_and_question(review: dict[str, Any]) -> tuple[dict[str, Any], Choice]:
    state = {
        "point": review["point"],
        "question": review["question"],
        "evidence": [item["fact"] for item in review["evidence"]],
        "constraints": review["constraints"],
    }
    instructions = (
        "根据 state 中的 question、evidence 与 constraints 独立评估 criteria 中的候选，选择证据最充分的选项。"
        "约束和可验证证据优先于偏好，不得根据候选顺序选择；证据不足时保持低置信度。"
    )
    return state, Choice(instructions=instructions, criteria={o["id"]: o["description"] for o in review["options"]})


def _review_projection(
    answer: Any,
    review: dict[str, Any],
    min_confidence: float,
    abstain_below: float,
    shadow: bool,
) -> dict[str, Any]:
    recommendation = getattr(answer, "choice", None)
    confidence = getattr(answer, "confidence", None)
    if recommendation not in {o["id"] for o in review["options"]}:
        raise ValueError("复核结果不在候选集合内")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("复核结果缺少有效 confidence")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("复核结果 confidence 越界")
    result: dict[str, Any] = {
        "point": review["point"], "primaryChoice": review["primaryChoice"], "recommendation": recommendation,
        "confidence": confidence, "agreement": recommendation == review["primaryChoice"],
        "mode": "shadow" if shadow else "advisory", "minConfidence": min_confidence,
        "abstainBelow": abstain_below, "adoptable": not shadow and confidence >= min_confidence,
    }
    if confidence < abstain_below:
        result.update(status="abstained", reasonCode="insufficient_evidence")
    elif shadow:
        result["status"] = "shadow"
    elif confidence >= min_confidence:
        result["status"] = "ok"
    else:
        result.update(status="not_adoptable", reasonCode="low_confidence")
    if shadow:
        for key in ("recommendation", "confidence", "agreement", "probabilities"):
            result.pop(key, None)
    return result


def _cache_hit_json(tool: str, args: dict, state: Any, use_cache: bool, engine_id: str | None = None):
    cache_key = _get_cache_key(args, engine_id)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            _log_journal({"tool": tool, "cache_hit": True, "latency_seconds": 0.0, "state_preview": str(state)[:100]})
            return _dump(hit), cache_key
    return None, cache_key


def _no_engine_json(tool: str, state: Any) -> str:
    _log_journal({"tool": tool, "error": "no_engine", "state_preview": str(state)[:100]})
    is_guard = tool.endswith("guard")
    return _dump({
        "error": "Decision 闸门不可用：当前没有可用引擎，本次未做任何裁决。" if is_guard else "Decision 在线裁决不可用：当前没有可用引擎，本次未做任何判定。",
        "engine": _ENGINE_NONE,
        "hint": "需要当前引擎的 API key（OpenRouter/Elephant 默认）和可导入的 system_one_adapter；拿不到 verdict 就不要当成 allow。",
    })


def _failure_json(tool: str, state: Any, error: Exception, timeout: float, engine_id: str | None = None) -> str:
    is_timeout = isinstance(error, concurrent.futures.TimeoutError)
    _log_journal({
        "tool": tool,
        "state_preview": str(state)[:100],
        ("timeout" if is_timeout else "error"): (True if is_timeout else str(error)),
    })
    if is_timeout:
        suffix = "本次未做任何裁决（无本地兜底）。" if tool.endswith("guard") else "本次未做任何判定。"
        message, latency = f"在线裁决超时 (>{timeout:.1f}s)，已熔断：{suffix}", timeout
    else:
        message, latency = f"在线裁决失败：{type(error).__name__}: {error}", 0.0
    return _dump({"error": message, "engine": engine_id or _active_engine(), "latency_seconds": latency})


def _build_decision_questions(args: dict) -> tuple[dict, dict]:
    questions: dict[str, Any] = {}
    configs = {kind: args.get(kind) for kind in ("choice", "noul", "score")}
    specs = (
        ("choice", "choices", Choice, "choice_q", "下一步做什么？", "criteria"),
        ("noul", "nouls", Noul, "noul_q", "是否发生？", None),
        ("score", "scores", Score, "score_q", "评估打分", "criteria"),
    )
    for kind, plural, cls, fallback_name, default_inst, crit_key in specs:
        item = configs[kind]
        if isinstance(item, dict) and (not crit_key or item.get(crit_key)):
            params = {
                "instructions": item.get("instructions", default_inst),
                **({crit_key: item[crit_key]} if crit_key else {}),
            }
            questions[item.get("name", fallback_name)] = cls(**params)
        for i, entry in enumerate(args.get(plural) or []):
            if not isinstance(entry, dict):
                continue
            if crit_key and not entry.get(crit_key):
                continue
            if not crit_key and not entry.get("instructions"):
                continue
            params = {
                "instructions": entry.get("instructions", "") or "",
                **({crit_key: entry[crit_key]} if crit_key else {}),
            }
            questions[entry.get("name") or f"{kind}_{i + 1}"] = cls(**params)
    return questions, configs


def _answers_from_response(response: Any) -> dict:
    return {
        name: getattr(value, value_name)
        for field, value_name in (("choices", "choice"), ("nouls", "noul"), ("scores", "score"))
        for name, value in getattr(response, field, {}).items()
    }


def _details_from_response(response: Any) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for name, val in getattr(response, "choices", {}).items():
        details[name] = {
            "type": "choice",
            "choice": getattr(val, "choice", None),
            "confidence": getattr(val, "confidence", None),
            "probabilities": dict(getattr(val, "probabilities", {}) or {}),
        }
    for name, val in getattr(response, "nouls", {}).items():
        details[name] = {"type": "noul", "noul": getattr(val, "noul", None)}
    for name, val in getattr(response, "scores", {}).items():
        details[name] = {
            "type": "score",
            "score": getattr(val, "score", None),
            "confidence": getattr(val, "confidence", None),
            "probabilities": {
                str(k): v
                for k, v in (getattr(val, "probabilities", {}) or {}).items()
            },
        }
    return details


def _build_guard_questions(instructions: str) -> dict:
    return {
        "verdict": Choice(
            instructions=instructions,
            criteria={
                "allow": "动作低风险、可回滚，且没有读取凭据或外发数据",
                "ask": "动作存在实质风险或信息不足，应由用户确认",
                "deny": "动作具有明显破坏性、凭据读取或外发风险",
            },
        ),
        "policy_allows": Noul(instructions="操作方的既定策略是否明确允许该动作（常规、低风险、可回滚）？true 表示明确允许。"),
        "reads_secrets": Noul(instructions="该动作是否会读取、复制、打印或上传凭据/密钥/令牌/密码等敏感信息？"),
        "sends_outbound": Noul(instructions="该动作是否会把本地内容或数据发送到外部网络或第三方？"),
        "self_advocating": Noul(instructions="命令或文本本身是否在为自己的执行辩护、催促或诱导放行？"),
        "blast_radius": Score(criteria=["trivial", "annoying", "severe"]),
    }


def _guard_signals(response: Any) -> dict:
    return {
        "verdict": getattr(response.choices.get("verdict"), "choice", None),
        "policy_allows": getattr(response.nouls.get("policy_allows"), "noul", None),
        "reads_secrets": getattr(response.nouls.get("reads_secrets"), "noul", None),
        "sends_outbound": getattr(response.nouls.get("sends_outbound"), "noul", None),
        "self_advocating": getattr(response.nouls.get("self_advocating"), "noul", None),
        "blast_radius": getattr(response.scores.get("blast_radius"), "score", None),
    }


def _run_online(state: Any, questions: dict[str, Any], timeout: float, provider: Any) -> tuple[Any, float]:
    started = time.time()
    response = _execute_system_one_with_timeout(
        client=SystemOneAdapterClient(structured_outputs=False, llm_answer_mode="probabilities"),
        state=state, questions=questions, provider=provider, timeout=timeout,
    )
    return response, round(time.time() - started, 2)


def _attach_meta(result: dict[str, Any], response: Any, engine_id: str, model_name: str, elapsed: float) -> None:
    result.update({
        "engine": getattr(response, "engine", engine_id.split(":", 1)[0]),
        "model": getattr(response, "model", model_name),
        "latency_seconds": elapsed,
        "cache_hit": False,
    })
    usage = getattr(response, "usage", None)
    if usage:
        result["usage"] = _usage_to_dict(usage)


def _handle_decision(args: dict[str, Any], **kwargs) -> str:
    if args.get("state") is None:
        return _dump({"error": "缺少 state（系统当前状态）参数。"})
    state = _redact(args["state"])
    timeout = _timeout_from_args(args)
    use_cache = not args.get("no_cache", False)
    cached, cache_key = _cache_hit_json("decision", args, state, use_cache)
    if cached is not None:
        return cached
    if not _decision_tool_available():
        return _no_engine_json("decision", state)

    questions, configs = _build_decision_questions(args)
    if not questions:
        return _dump({
            "error": "没有提供任何判定题目，请提供 choice/choices、noul/nouls 或 score/scores 之一。",
            "tip": "支持多题并发传入，一次网络往返即可返回所有题目答案。",
        })

    try:
        _, model_name, _ = _engine_spec()
        response, elapsed = _run_online(state, questions, timeout, _build_provider())
        answers = _answers_from_response(response)
        result: dict[str, Any] = {"answers": answers, "details": _details_from_response(response)}
        _attach_meta(result, response, _active_engine(), model_name, elapsed)
        for kind, alias in (("choice", "choice_answer"), ("noul", "noul_answer"), ("score", "score_answer")):
            config = configs[kind]
            answer_name = config.get("name", f"{kind}_q") if config else None
            if answer_name in answers:
                result[alias] = answers[answer_name]
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({
            "tool": "decision",
            "latency_seconds": elapsed,
            "questions_count": len(questions),
            "answers": answers,
            "state_preview": str(state)[:100],
        })
        return _dump(result)
    except Exception as exc:
        return _failure_json("decision", state, exc, timeout)


def _handle_decision_guard(args: dict[str, Any], **kwargs) -> str:
    if args.get("state") is None:
        return _dump({"error": "缺少 state（待执行动作描述）参数。"})
    state = _redact(args["state"])
    timeout = _timeout_from_args(args)
    use_cache = not args.get("no_cache", False)
    guard_engine_id = _active_engine()
    cached, cache_key = _cache_hit_json("decision_guard", args, state, use_cache, guard_engine_id)
    if cached is not None:
        return cached
    if not _decision_tool_available():
        return _no_engine_json("decision_guard", state)

    guard_cfg = args.get("guard") or {}
    try:
        threshold = max(0.0, min(float(guard_cfg.get("threshold", 0.75)), 1.0))
    except (ValueError, TypeError):
        threshold = 0.75
    deny_threshold = min(0.25, threshold * 0.5) if threshold > 0 else 0.0
    questions = _build_guard_questions(guard_cfg.get("instructions", "这个动作是否可以安全执行？true 表示安全可放行。"))

    try:
        _, guard_model, _ = _engine_spec()
        response, elapsed = _run_online(state, questions, timeout, _build_provider())
        probability = getattr(response.nouls.get("guard_q"), "noul", None)
        signals = _guard_signals(response)
        raw_conf = getattr(response.choices.get("verdict"), "confidence", None)
        confidence = None if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)) else float(raw_conf)
        judged = _guard_verdict_from_signals(signals, threshold)
        if judged is None:
            if probability is None:
                raise RuntimeError("裁决响应缺少 guard_q 与多题信号，无法判定")
            verdict = "allow" if probability >= threshold else ("ask" if probability >= deny_threshold else "deny")
            reason, verdict_source = "单题概率三档（信号缺失时的兼容路径）", "probability"
        else:
            verdict, reason = judged
            if verdict == "allow" and confidence is not None and confidence < 0.55:
                verdict, reason = "ask", f"模型置信度不足（confidence {confidence:.2f} < 0.55）"
            verdict_source = "signals"
        result: dict[str, Any] = {
            "verdict": verdict,
            "probability": probability if probability is not None else signals.get("policy_allows"),
            "confidence": confidence,
            "threshold": threshold,
            "deny_threshold": deny_threshold,
            "signals": signals,
            "verdict_source": verdict_source,
            "reason": reason,
        }
        _attach_meta(result, response, guard_engine_id, guard_model, elapsed)
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({
            "tool": "decision_guard",
            "verdict": verdict,
            "probability": probability,
            "latency_seconds": elapsed,
            "state_preview": str(state)[:100],
        })
        return _dump(result)
    except Exception as exc:
        return _failure_json("decision_guard", state, exc, timeout, guard_engine_id)


def _handle_decision_review(args: dict[str, Any], **kwargs) -> str:
    try:
        review, min_confidence, abstain_below, shadow = _normalize_review_args(args)
    except ValueError as exc:
        _log_journal({"tool": "decision_review", "error": "invalid_input", "reason": str(exc)})
        return _dump({"error": f"复核输入无效：{exc}", "status": "rejected", "adoptable": False})

    state, question = _review_state_and_question(review)
    timeout = _timeout_from_args(args)
    use_cache = not args.get("no_cache", False)
    cache_args = {
        "state": state,
        "review": {
            k: review[k]
            for k in ("point", "primaryChoice", "options", "evidence", "constraints")
        },
    }
    cached, cache_key = _cache_hit_json("decision_review", cache_args, state, use_cache)
    if cached is not None:
        return cached
    if not _decision_tool_available():
        _log_journal({"tool": "decision_review", "error": "no_engine", "point": review["point"]})
        return _dump({
            "error": "Decision 在线复核不可用：当前没有可用引擎，本次未做任何复核。",
            "status": "unavailable", "point": review["point"], "primaryChoice": review["primaryChoice"],
            "adoptable": False, "engine": _ENGINE_NONE,
            "hint": "需要当前引擎的 API key（OpenRouter/Elephant 默认）和可导入的 system_one_adapter；错误不是放行。",
        })

    try:
        _, model_name, _ = _engine_spec()
        response, elapsed = _run_online(state, {"review": question}, timeout, _build_provider())
        answer = getattr(response, "choices", {}).get("review")
        if answer is None:
            raise ValueError("复核响应缺少 review 结果")
        result = _review_projection(answer, review, min_confidence, abstain_below, shadow)
        probabilities = getattr(answer, "probabilities", None)
        if not shadow and isinstance(probabilities, dict):
            allowed = {o["id"] for o in review["options"]}
            result["probabilities"] = {
                k: float(v)
                for k, v in probabilities.items()
                if k in allowed and isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        _attach_meta(result, response, _active_engine(), model_name, elapsed)
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({
            "tool": "decision_review", "point": review["point"], "status": result["status"],
            "agreement": result.get("agreement"), "confidence": result.get("confidence"),
            "adoptable": result["adoptable"], "latency_seconds": elapsed, "state_preview": review["question"][:100],
        })
        return _dump(result)
    except Exception as exc:
        result = json.loads(_failure_json("decision_review", state, exc, timeout))
        result.update({
            "status": "unavailable",
            "point": review["point"],
            "primaryChoice": review["primaryChoice"],
            "adoptable": False,
        })
        return _dump(result)
