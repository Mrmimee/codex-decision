"""Jev/TypeSafe AI 决策小脑（Codex 内嵌引擎）。

供 `scripts/server.py` 通过 MCP 暴露"单选 (Choice)"、"打分 (Score)"、"是非率 (Noul)"决策。
支持单题/多题并发打满（1 次网络往返裁决多个问题），具备双层持久化磁盘缓存、8 秒硬超时熔断与黑匣子审计日志。

引擎（`system_one_adapter` → OpenRouter/Elephant 或 Agnes，结果里带 `engine` 字段）：
默认在线引擎是 OpenRouter 上的 `baseten/elephant-1:free`（需要 `OPENROUTER_API_KEY`）；
可通过 `JEV_ENGINE=agnes` 显式切回 Agnes 3.0 Flash（需要 `AGNES_API_KEY`）。
官方 TypeSafe 直连按量计费，本插件不接入。

引擎不可用、超时或报错时**直接返回 error**：不做本地离线兜底判定，不产生任何"看起来正常"的假结论。

零常驻显存，零后台进程，极低内存消耗。
"""
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

def _extra_site_packages() -> list[str]:
    """Optional site-packages dirs that may host ``system_one_adapter``.

    Ordered most-specific first. Missing dirs are ignored, and a dir is only
    added when it actually contains the adapter.
    """
    home = Path.home()
    candidates = [
        os.environ.get("JEV_SITE_PACKAGES"),
        os.environ.get("JEV_VENV") and str(Path(os.environ["JEV_VENV"]) / "Lib" / "site-packages"),
        home / "jev-starter" / ".venv" / "Lib" / "site-packages",
        home / ".jev" / "venv" / "Lib" / "site-packages",
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
    from system_one_adapter import SystemOneAdapterClient, Choice, Noul, Score
    from system_one_adapter.providers.openai import OpenAIProvider
except Exception:
    _adapter_available = False

    class _MissingOpenAIProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("system_one_adapter 未安装：请 pip install 'system-one-adapter[openai]'")

    OpenAIProvider: Any = _MissingOpenAIProvider

_AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
_AGNES_MODEL_NAME = "agnes-3.0-flash"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api"
_OPENROUTER_MODEL_NAME = "baseten/elephant-1:free"
_DEFAULT_ENGINE = "openrouter"
_DEFAULT_TIMEOUT_SECONDS = 8.0
_DEFAULT_REVIEW_MIN_CONFIDENCE = 0.75
_DEFAULT_REVIEW_ABSTAIN_BELOW = 0.5

_ENGINE_AGNES = "agnes"
_ENGINE_OPENROUTER = "openrouter"
_ENGINE_NONE = "none"     # 无可用引擎：直接报错，不做离线兜底

_REVIEW_LIMITS = {
    "point": 64,
    "question": 500,
    "option_id": 64,
    "option_description": 500,
    "options": 8,
    "evidence_items": 12,
    "evidence_fact": 1200,
    "evidence_total": 6000,
    "constraints": 10,
    "constraint": 500,
    "constraints_total": 2500,
}
_POINT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_REVIEW_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)\b\s*[:=]\s*[^\s,;\"']{6,}"),
)


# 1. 缓存路径与黑匣子日志路径
def _get_cache_dir() -> Path:
    """Jev 自己的状态目录（插件更新不会清空）。

    优先 JEV_STATE_DIR，其次 Codex 在用户数据目录下的专属目录，最后用户目录兜底。
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = [
        os.environ.get("JEV_STATE_DIR"),
        Path(local_app_data) / "codex-jev" / "jev" if local_app_data else None,
        Path.home() / ".jev" / "cache",
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
        except OSError:
            continue
        return path
    raise RuntimeError("Jev 无法创建可写状态目录，请设置 JEV_STATE_DIR")


_CACHE_DIR = _get_cache_dir()
_CACHE_FILE = _CACHE_DIR / "jev_cache.json"
_JOURNAL_FILE = _CACHE_DIR / "jev_journal.jsonl"

# LRU 决策缓存（上限 256 条，默认 3600 秒有效期）
_CACHE_MAX_SIZE = 256
_CACHE_TTL_SECONDS = 3600
_CACHE_SCHEMA_VERSION = 3   # 结果结构变化时递增，旧格式缓存自然失效
_DECISION_CACHE: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()
_CACHE_LOCK = threading.RLock()  # 保护 _DECISION_CACHE 的并发读写
_DISK_LOCK = threading.Lock()     # 保护磁盘写（避免并发 write_text 竞态）

# 模块级单例线程池：超时后不 join 阻塞，后台 worker 自行结束
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="jev")


def _load_disk_cache() -> None:
    """启动时从磁盘恢复决策缓存，跨会话/重启 0ms 秒命中"""
    if not _CACHE_FILE.exists():
        return
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        for k, item in data.items():
            if isinstance(item, dict) and "created_at" in item and "result" in item:
                created_at = item["created_at"]
                if now - created_at <= _CACHE_TTL_SECONDS:
                    _DECISION_CACHE[k] = (created_at, item["result"])
    except Exception:
        pass


def _save_disk_cache() -> None:
    """轻量原子写盘，持久化缓存（锁保护 + 临时文件原子替换，防异常断电坏文件）"""
    try:
        with _DISK_LOCK:
            payload = {}
            with _CACHE_LOCK:
                for k, (created_at, res) in _DECISION_CACHE.items():
                    payload[k] = {"created_at": created_at, "result": res}
            # 原子写入：写 tmp 文件后原子替换，避免破坏原文件
            tmp_file = _CACHE_FILE.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_file.replace(_CACHE_FILE)
    except Exception:
        pass


# 初始化加载磁盘缓存
_load_disk_cache()


def _get_cache_key(args: dict[str, Any]) -> str:
    """计算状态与问题的确定性 SHA256 指纹（剥离 timeout/no_cache 等控制参数，防缓存击穿）"""
    try:
        # 仅保留具有决策语义的字段
        semantic_keys = {"state", "choice", "choices", "noul", "nouls", "score", "scores", "guard"}
        semantic_payload = {k: v for k, v in args.items() if k in semantic_keys}
        # 引擎/模型也进缓存键：换引擎或换 Jev 版本后不复用旧引擎的结论
        semantic_payload["__engine"] = _active_engine()
        # 结果结构变化时递增，避免命中旧格式缓存（例如去掉 advisory 字段）
        semantic_payload["__schema"] = _CACHE_SCHEMA_VERSION
        raw = json.dumps(semantic_payload, sort_keys=True, ensure_ascii=True)
    except Exception:
        raw = str(args)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    """获取缓存并执行 LRU 提升与 TTL 过期检查（锁保护）"""
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
    """写入缓存，超出容量时淘汰最老条目；磁盘持久化丢给线程池（写失败仅丢缓存，不影响判定）"""
    with _CACHE_LOCK:
        if len(_DECISION_CACHE) >= _CACHE_MAX_SIZE:
            _DECISION_CACHE.popitem(last=False)
        _DECISION_CACHE[key] = (time.time(), result)
    _executor.submit(_save_disk_cache)


def _log_journal(entry: dict[str, Any]) -> None:
    """记录判定黑匣子流水（审计日志，带 10MB 自动轮转备份）"""
    try:
        if _JOURNAL_FILE.exists() and _JOURNAL_FILE.stat().st_size > 10 * 1024 * 1024:
            bak = _JOURNAL_FILE.with_suffix(".jsonl.old")
            try:
                if bak.exists():
                    bak.unlink()
                _JOURNAL_FILE.replace(bak)
            except Exception:
                pass

        record = dict(entry)
        record["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _key_from_file() -> str:
    key_file = os.environ.get("JEV_KEY_FILE")
    if not key_file:
        return ""
    path = Path(key_file).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _get_engine_name() -> str:
    """Select the backend; OpenRouter/Elephant is the default."""
    requested = os.environ.get("JEV_ENGINE", "").strip().lower()
    if requested in (_ENGINE_OPENROUTER, _ENGINE_AGNES):
        return requested
    return _DEFAULT_ENGINE


def _guard_engine_name() -> str:
    """Use Agnes for guard by default, while allowing an explicit override."""
    requested = os.environ.get("JEV_GUARD_ENGINE", "").strip().lower()
    if requested in (_ENGINE_OPENROUTER, _ENGINE_AGNES):
        return requested
    return _ENGINE_AGNES


def _get_openrouter_key() -> str:
    """Read the OpenRouter key without scanning arbitrary files."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    return _key_from_file()


def _get_agnes_key() -> str:
    """Read the legacy Agnes key without scanning arbitrary files."""
    key = os.environ.get("AGNES_API_KEY", "").strip()
    if key:
        return key
    return _key_from_file()


def _get_engine_key(engine_name: str | None = None) -> str:
    if (engine_name or _get_engine_name()) == _ENGINE_AGNES:
        return _get_agnes_key()
    return _get_openrouter_key()


def _active_engine() -> str:
    """Current engine identifier, used for reporting and cache isolation."""
    if not _adapter_available:
        return _ENGINE_NONE
    if _get_engine_name() == _ENGINE_AGNES:
        return (
            f"{_ENGINE_AGNES}:{_AGNES_MODEL_NAME}"
            if _get_agnes_key()
            else _ENGINE_NONE
        )
    return (
        f"{_ENGINE_OPENROUTER}:{_OPENROUTER_MODEL_NAME}"
        if _get_openrouter_key()
        else _ENGINE_NONE
    )


def _active_guard_engine() -> str:
    """Current guard engine identifier."""
    if not _adapter_available:
        return _ENGINE_NONE
    if _guard_engine_name() == _ENGINE_AGNES:
        return (
            f"{_ENGINE_AGNES}:{_AGNES_MODEL_NAME}"
            if _get_agnes_key()
            else _ENGINE_NONE
        )
    return (
        f"{_ENGINE_OPENROUTER}:{_OPENROUTER_MODEL_NAME}"
        if _get_openrouter_key()
        else _ENGINE_NONE
    )


def _default_model_name(engine_name: str | None = None) -> str:
    if (engine_name or _get_engine_name()) == _ENGINE_AGNES:
        return _AGNES_MODEL_NAME
    return _OPENROUTER_MODEL_NAME


# 发送给第三方裁决前先脱敏（凭据绝不进网络请求，也不进审计日志预览）
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),                      # Google API key
    re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),                # Slack token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),             # PEM 私钥
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)\b\s*[:=]\s*[^\s,;\"']{6,}"),
)


# 会触发“整值脱敏”的键名（结构化 state 里出现这些键，值直接遮蔽，不论值是不是字符串）
_SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential|auth)")


def _redact(value: Any) -> Any:
    """脱敏：遮蔽 state 里的密钥/令牌/口令（字符串、字典、列表均可；敏感键下的任何值都整体遮蔽）。"""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEYS.search(str(key)) else _redact(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    text = str(value)
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    """把 SDK Usage 对象转成可 JSON 序列化的 dict。

    工具结果、磁盘缓存、审计日志都要 `json.dumps`；若直接塞进 SDK 的 Usage 对象，
    `json.dumps(result)` 会抛 TypeError，把整条在线结果打成异常。
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in usage.items()}
    for meth in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, meth, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in d.items()}
            except Exception:
                pass
    out = {}
    for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
        v = getattr(usage, k, None)
        if v is not None:
            out[k] = v
    return out or {"raw": str(usage)}


def _guard_verdict_from_signals(signals: dict[str, Any], threshold: float) -> tuple | None:
    """Turn validated model signals into a deterministic guard verdict."""
    required = (
        "verdict",
        "policy_allows",
        "reads_secrets",
        "sends_outbound",
        "self_advocating",
        "blast_radius",
    )
    if any(signals.get(k) is None for k in required):
        return None
    blast = float(signals["blast_radius"])
    raw_verdict = str(signals["verdict"]).lower()
    if raw_verdict not in ("allow", "ask", "deny"):
        return None
    if float(signals["self_advocating"]) >= 0.6:
        return "ask", "命令文本疑似自我游说（self_advocating >= 0.6）"
    if (
        float(signals["reads_secrets"]) >= 0.7
        and float(signals["sends_outbound"]) >= 0.7
    ):
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


def _jev_tool_available() -> bool:
    """判断 Jev 小脑是否可用：adapter 已装 + 当前引擎 key 可获取"""
    return bool(_adapter_available and _get_engine_key())


def _guard_tool_available() -> bool:
    """The guard has its own backend preference."""
    return bool(_adapter_available and _get_engine_key(_guard_engine_name()))


# 2. 工具定义 Schema
# 两个工具共用的控制参数（避免在两份 schema 里重复写同一段描述）
_TIMEOUT_PARAM = {
    "type": "number",
    "description": "网络裁决超时熔断阈值（秒，默认 8.0 秒）。超时直接返回 error，不做本地兜底。",
}
_NO_CACHE_PARAM = {
    "type": "boolean",
    "description": "是否跳过本地缓存强制实时推理（默认 false）。",
}

JEV_SCHEMA = {
    "name": "jev",
    "description": (
        "Jev 决策小脑（system_one_adapter → OpenRouter/Elephant 结构化裁决）："
        "用于在多个可用工具/执行路径之间确有选择不确定性时，先把候选项及其用途传入 Choice 进行按需路由；"
        "主 Agent 再负责调用所选工具并处理结果。"
        "普通对话、工具选择显而易见时不要调用 Jev；不要每轮调用，也不要对每次工具调用重复裁决。"
        "Jev 只输出结构化判断，不会替你调用工具。风险评估可用 Noul，质量/复杂度可用 Score；"
        "支持多题并发（nouls/choices/scores），带缓存、8秒默认超时熔断和审计记录；"
        "返回里带 engine/model/usage 便于核对实际走的引擎；发送前会自动脱敏凭据。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": ["object", "string"],
                "description": "完整当前状态；只传与本次路由/判断相关的信息，避免放入密钥、凭据或无关个人内容。",
            },
            "choice": {
                "type": "object",
                "description": "单项选择题（单题）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "单选意图描述"},
                    "criteria": {
                        "type": "object",
                        "description": "选项及其含义，格式为 {'选项key': '该选项的描述'}",
                    },
                },
            },
            "choices": {
                "type": "array",
                "description": "多个单选题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "单选意图描述"},
                        "criteria": {"type": "object", "description": "选项键值对"},
                    },
                    "required": ["criteria"],
                },
            },
            "noul": {
                "type": "object",
                "description": "是非题（单题，布尔概率 0.0~1.0）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "判断意图描述"},
                },
            },
            "nouls": {
                "type": "array",
                "description": "多个是非题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "判断意图描述"},
                    },
                    "required": ["instructions"],
                },
            },
            "score": {
                "type": "object",
                "description": "打分题（单题，离散评分梯度）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "打分意图描述"},
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "评分梯度数组，如 ['低', '中', '高']",
                    },
                },
            },
            "scores": {
                "type": "array",
                "description": "多个打分题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "打分意图描述"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "评分梯度数组"},
                    },
                    "required": ["criteria"],
                },
            },
            "timeout_seconds": dict(_TIMEOUT_PARAM),
            "no_cache": dict(_NO_CACHE_PARAM),
        },
        "required": ["state"],
    },
}

# guard 动作闸门 Schema：执行前对动作做 allow/ask/deny 三档裁决
JEV_GUARD_SCHEMA = {
    "name": "jev_guard",
    "description": (
        "Jev 动作闸门（guard）：仅在动作本身具有实质风险、需要额外风险评估时按需调用；"
        "这是第二意见，不会替代系统或用户授权规则。普通低风险工具调用无需逐次运行 guard。"
        "在线时一次请求并行裁决 6 个信号（是否安全 / 策略是否允许 / 是否读取凭据 / 是否外发内容 / "
        "是否自我游说 / 破坏半径），再由确定性代码给出 allow/ask/deny，避免把自由文本当授权。"
        "引擎不可用、超时或调用失败时直接返回 error，不给 verdict（没有本地兜底判定）；拿不到 verdict 就不要当作放行凭据。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": ["object", "string"],
                "description": "只提供与待评估动作直接相关的上下文；移除密钥、令牌、密码和无关个人信息。引擎不可用或调用失败时会直接返回 error。",
            },
            "guard": {
                "type": "object",
                "description": "闸门配置（可选）。",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": "判定意图描述，默认：该动作是否可安全执行。",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "放行阈值（0~1，默认 0.75）。概率低于阈值判 ask。",
                    },
                },
            },
            "timeout_seconds": dict(_TIMEOUT_PARAM),
            "no_cache": dict(_NO_CACHE_PARAM),
        },
        "required": ["state"],
    },
}

# 复核工具：吸收 decision-review 的“主模型预选 + 独立证据复核”协议，
# 但仍由 Jev/Agnes 给出结构化 Choice 结果，不引入 DSH 或第二个网关。
JEV_REVIEW_SCHEMA = {
    "name": "jev_review",
    "description": (
        "对主模型已经形成的预选做独立复核。适用于两个以上合法候选、"
        "证据可能冲突、或用户没有明确选择且结果会实质影响后续动作的场景。"
        "调用前先独立给出 primaryChoice；reviewer 只返回 recommendation、"
        "confidence、probabilities 和 adoptable 阈值提示，主模型保留最终决定权。"
        "不要用于权限审批、凭据、写操作或网络目标的放行。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "point": {
                "type": "string",
                "description": "决策点名称，如 candidate_selection、entity_resolution、plan_consistency_review。",
            },
            "question": {
                "type": "string",
                "description": "需要复核的具体决策问题。",
            },
            "primaryChoice": {
                "type": "string",
                "description": "主模型在复核前已经独立形成的候选 id。",
            },
            "options": {
                "type": "array",
                "description": "2 到 8 个合法候选，id 必须唯一。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "evidenceRefs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["id", "description"],
                },
            },
            "evidence": {
                "type": "array",
                "description": "可验证证据，id 必须唯一；不要放凭据或无关个人数据。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "fact": {"type": "string"},
                    },
                    "required": ["id", "fact"],
                },
            },
            "constraints": {
                "type": "array",
                "description": "必须满足的硬约束或用户明确要求。",
                "items": {"type": "string"},
            },
            "minConfidence": {
                "type": "number",
                "description": "达到该置信度才标记 adoptable，默认 0.75。",
            },
            "abstainBelow": {
                "type": "number",
                "description": "低于该置信度时弃权，默认 0.5。",
            },
            "mode": {
                "type": "string",
                "enum": ["advisory", "shadow"],
                "description": "advisory 返回建议；shadow 只记录复核，不向主模型透出建议。",
            },
            "timeout_seconds": dict(_TIMEOUT_PARAM),
            "no_cache": dict(_NO_CACHE_PARAM),
        },
        "required": ["point", "question", "primaryChoice", "options"],
    },
}


def _build_provider(engine_name: str | None = None) -> Any:
    """Build the provider for the selected backend."""
    selected = engine_name or _get_engine_name()
    if selected == _ENGINE_AGNES:
        return OpenAIProvider(
            _AGNES_MODEL_NAME,
            base_url=_AGNES_BASE_URL,
            api_key=_get_agnes_key(),
            api="chat_completions",
        )
    from .decisions_provider import DecisionsProvider

    return DecisionsProvider(
        _OPENROUTER_MODEL_NAME,
        base_url=_OPENROUTER_BASE_URL,
        api_key=_get_openrouter_key(),
    )


def _execute_system_one_with_timeout(
    client: Any,
    state: Any,
    questions: dict[str, Any],
    provider: Any,
    timeout: float,
) -> Any:
    """带硬超时保护的 System One 执行包装器（system_one_adapter → 当前引擎）。

    使用模块级单例线程池：超时时 fut.result(timeout) 抛出 TimeoutError，
    主调用方立即返回（真硬超时），后台 worker 继续执行至自然结束，
    绝不阻塞主流程。
    """
    fut = _executor.submit(
        client.system_one,
        state=state,
        questions=questions,
        model=provider,
    )
    return fut.result(timeout=timeout)


def _dump(payload: Any) -> str:
    """工具结果统一序列化：ensure_ascii=False 让模型直接看到中文，2 空格缩进便于阅读。"""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _timeout_from_args(args: dict, default: float = _DEFAULT_TIMEOUT_SECONDS) -> float:
    try:
        return max(0.1, min(float(args.get("timeout_seconds") or default), 120.0))
    except (ValueError, TypeError):
        return default


def _review_string(
    value: Any,
    field: str,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
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
    normalized = [
        _review_string(item, f"{field}[{index}]", max_item_length)
        for index, item in enumerate(value)
    ]
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
    """Validate review input before it can reach the network.

    This deliberately returns compact, normalized data: the reviewer should
    only see the question, candidates, evidence and constraints it needs.
    """
    point = _review_string(args.get("point"), "point", _REVIEW_LIMITS["point"], _POINT_PATTERN)
    question = _review_string(args.get("question"), "question", _REVIEW_LIMITS["question"])
    primary_choice = _review_string(
        args.get("primaryChoice"),
        "primaryChoice",
        _REVIEW_LIMITS["option_id"],
        _ID_PATTERN,
    )
    raw_options = args.get("options")
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= _REVIEW_LIMITS["options"]:
        raise ValueError(f"options 数量必须在 2 到 {_REVIEW_LIMITS['options']} 之间")
    options: list[dict[str, Any]] = []
    for index, raw_option in enumerate(raw_options):
        if not isinstance(raw_option, dict):
            raise ValueError(f"options[{index}] 必须是对象")
        option_id = _review_string(
            raw_option.get("id"),
            f"options[{index}].id",
            _REVIEW_LIMITS["option_id"],
            _ID_PATTERN,
        )
        description = _review_string(
            raw_option.get("description"),
            f"options[{index}].description",
            _REVIEW_LIMITS["option_description"],
        )
        evidence_refs = _review_string_list(
            raw_option.get("evidenceRefs"),
            f"options[{index}].evidenceRefs",
            _REVIEW_LIMITS["evidence_items"],
            _REVIEW_LIMITS["option_id"],
            _REVIEW_LIMITS["evidence_items"] * _REVIEW_LIMITS["option_id"],
        )
        options.append({"id": option_id, "description": description, "evidenceRefs": evidence_refs})
    option_ids = [option["id"] for option in options]
    if len(set(option_ids)) != len(option_ids):
        raise ValueError("候选 id 不能重复")
    if primary_choice not in option_ids:
        raise ValueError("primaryChoice 必须属于候选集合")

    raw_evidence = args.get("evidence")
    if raw_evidence is None:
        raw_evidence = []
    if not isinstance(raw_evidence, list) or len(raw_evidence) > _REVIEW_LIMITS["evidence_items"]:
        raise ValueError(f"evidence 数量不能超过 {_REVIEW_LIMITS['evidence_items']}")
    evidence: list[dict[str, str]] = []
    for index, raw_item in enumerate(raw_evidence):
        if not isinstance(raw_item, dict):
            raise ValueError(f"evidence[{index}] 必须是对象")
        evidence.append({
            "id": _review_string(
                raw_item.get("id"),
                f"evidence[{index}].id",
                _REVIEW_LIMITS["option_id"],
                _ID_PATTERN,
            ),
            "fact": _review_string(
                raw_item.get("fact"),
                f"evidence[{index}].fact",
                _REVIEW_LIMITS["evidence_fact"],
            ),
        })
    evidence_ids = [item["id"] for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("证据 id 不能重复")
    if sum(len(item["fact"]) for item in evidence) > _REVIEW_LIMITS["evidence_total"]:
        raise ValueError(f"evidence 超过总字符预算 {_REVIEW_LIMITS['evidence_total']}")
    evidence_id_set = set(evidence_ids)
    for option in options:
        for reference in option["evidenceRefs"]:
            if reference not in evidence_id_set:
                raise ValueError(f"候选 {option['id']} 引用了不存在的证据 {reference}")

    constraints = _review_string_list(
        args.get("constraints"),
        "constraints",
        _REVIEW_LIMITS["constraints"],
        _REVIEW_LIMITS["constraint"],
        _REVIEW_LIMITS["constraints_total"],
    )
    outbound_text = [
        question,
        *(option["description"] for option in options),
        *(item["fact"] for item in evidence),
        *constraints,
    ]
    if any(pattern.search(text) for text in outbound_text for pattern in _REVIEW_SECRET_PATTERNS):
        raise ValueError("复核输入疑似包含凭据或密钥")
    min_confidence = _review_number(
        args.get("minConfidence"),
        "minConfidence",
        _DEFAULT_REVIEW_MIN_CONFIDENCE,
        0.0,
        1.0,
    )
    abstain_below = _review_number(
        args.get("abstainBelow"),
        "abstainBelow",
        _DEFAULT_REVIEW_ABSTAIN_BELOW,
        0.0,
        1.0,
    )
    if abstain_below > min_confidence:
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
        min_confidence,
        abstain_below,
        mode == "shadow",
    )


def _review_state_and_question(
    review: dict[str, Any],
) -> tuple[dict[str, Any], Choice]:
    state = {
        "point": review["point"],
        "question": review["question"],
        "evidence": [item["fact"] for item in review["evidence"]],
        "constraints": review["constraints"],
    }
    instructions = (
        "根据 state 中的 question、evidence 与 constraints 独立评估 criteria 中的候选，"
        "选择证据最充分的选项。"
        "约束和可验证证据优先于偏好，不得根据候选顺序选择；证据不足时保持低置信度。"
    )
    criteria = {option["id"]: option["description"] for option in review["options"]}
    return state, Choice(instructions=instructions, criteria=criteria)


def _review_projection(
    answer: Any,
    review: dict[str, Any],
    min_confidence: float,
    abstain_below: float,
    shadow: bool,
) -> dict[str, Any]:
    recommendation = getattr(answer, "choice", None)
    confidence = getattr(answer, "confidence", None)
    if recommendation not in {option["id"] for option in review["options"]}:
        raise ValueError("复核结果不在候选集合内")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("复核结果缺少有效 confidence")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("复核结果 confidence 越界")
    result: dict[str, Any] = {
        "point": review["point"],
        "primaryChoice": review["primaryChoice"],
        "recommendation": recommendation,
        "confidence": confidence,
        "agreement": recommendation == review["primaryChoice"],
        "mode": "shadow" if shadow else "advisory",
        "minConfidence": min_confidence,
        "abstainBelow": abstain_below,
        "adoptable": not shadow and confidence >= min_confidence,
    }
    if confidence < abstain_below:
        result["status"] = "abstained"
        result["reasonCode"] = "insufficient_evidence"
    elif shadow:
        result["status"] = "shadow"
    elif confidence >= min_confidence:
        result["status"] = "ok"
    else:
        result["status"] = "not_adoptable"
        result["reasonCode"] = "low_confidence"
    if shadow:
        result.pop("recommendation", None)
        result.pop("confidence", None)
        result.pop("agreement", None)
        result.pop("probabilities", None)
    return result


def _cache_hit_json(tool: str, args: dict, state: Any, use_cache: bool):
    cache_key = _get_cache_key(args)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            _log_journal({"tool": tool, "cache_hit": True, "latency_seconds": 0.0,
                          "state_preview": str(state)[:100]})
            return _dump(hit), cache_key
    return None, cache_key


def _no_engine_json(tool: str, state: Any) -> str:
    _log_journal({"tool": tool, "error": "no_engine", "state_preview": str(state)[:100]})
    message = (
        "Jev 闸门不可用：当前没有可用引擎，本次未做任何裁决。"
        if tool == "jev_guard"
        else "Jev 在线裁决不可用：当前没有可用引擎，本次未做任何判定。"
    )
    return _dump({
        "error": message,
        "engine": _ENGINE_NONE,
        "hint": "需要当前引擎的 API key（OpenRouter/Elephant 默认）和可导入的 system_one_adapter；拿不到 verdict 就不要当成 allow。",
    })


def _failure_json(tool: str, state: Any, error: Exception, timeout: float) -> str:
    is_timeout = isinstance(error, concurrent.futures.TimeoutError)
    entry = {"tool": tool, "state_preview": str(state)[:100]}
    entry["timeout" if is_timeout else "error"] = True if is_timeout else str(error)
    _log_journal(entry)
    if is_timeout:
        message = (
            f"在线裁决超时 (>{timeout:.1f}s)，已熔断：本次未做任何裁决（无本地兜底）。"
            if tool == "jev_guard"
            else f"在线裁决超时 (>{timeout:.1f}s)，已熔断：本次未做任何判定。"
        )
        latency = timeout
    else:
        message = f"在线裁决失败：{type(error).__name__}: {error}"
        latency = 0.0
    return _dump({"error": message, "engine": _active_engine(), "latency_seconds": latency})


def _build_jev_questions(args: dict) -> tuple[dict, dict]:
    questions = {}
    configs = {kind: args.get(kind) for kind in ("choice", "noul", "score")}

    def add_many(items, kind, cls, criteria_required=False):
        for i, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            if criteria_required and not item.get("criteria"):
                continue
            if not criteria_required and not item.get("instructions"):
                continue
            name = item.get("name") or f"{kind}_{i + 1}"
            params = {"instructions": item.get("instructions", "") or ""}
            if criteria_required:
                params["criteria"] = item["criteria"]
            questions[name] = cls(**params)

    single_types = {
        "choice": (Choice, "choice_q", "下一步做什么？", "criteria"),
        "noul": (Noul, "noul_q", "是否发生？", None),
        "score": (Score, "score_q", "评估打分", "criteria"),
    }
    for kind, (cls, fallback_name, default_instruction, criteria_key) in single_types.items():
        item = configs[kind]
        if not isinstance(item, dict):
            continue
        criteria = item.get(criteria_key) if criteria_key else True
        if not criteria:
            continue
        params = {"instructions": item.get("instructions", default_instruction)}
        if criteria_key:
            params["criteria"] = criteria
        questions[item.get("name", fallback_name)] = cls(**params)

    add_many(args.get("choices"), "choice", Choice, criteria_required=True)
    add_many(args.get("nouls"), "noul", Noul)
    add_many(args.get("scores"), "score", Score, criteria_required=True)
    return questions, configs


def _answers_from_response(response: Any) -> dict:
    answers = {}
    for field, value_name in (("choices", "choice"), ("nouls", "noul"), ("scores", "score")):
        answers.update({name: getattr(value, value_name) for name, value in getattr(response, field, {}).items()})
    return answers


def _details_from_response(response: Any) -> dict[str, dict[str, Any]]:
    """Expose full probabilities and confidence without breaking ``answers``."""
    details: dict[str, dict[str, Any]] = {}
    for name, value in getattr(response, "choices", {}).items():
        details[name] = {
            "type": "choice",
            "choice": getattr(value, "choice", None),
            "confidence": getattr(value, "confidence", None),
            "probabilities": dict(getattr(value, "probabilities", {}) or {}),
        }
    for name, value in getattr(response, "nouls", {}).items():
        details[name] = {
            "type": "noul",
            "noul": getattr(value, "noul", None),
        }
    for name, value in getattr(response, "scores", {}).items():
        details[name] = {
            "type": "score",
            "score": getattr(value, "score", None),
            "confidence": getattr(value, "confidence", None),
            "probabilities": {
                str(k): v
                for k, v in (getattr(value, "probabilities", {}) or {}).items()
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


def _guard_model_confidence(response: Any) -> float | None:
    verdict = response.choices.get("verdict")
    confidence = getattr(verdict, "confidence", None)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    return float(confidence)


def _handle_jev(args: dict[str, Any], **kwargs) -> str:
    """Build questions, call Agnes, and return structured answers; failures stay explicit."""
    if args.get("state") is None:
        return _dump({"error": "缺少 state（系统当前状态）参数。"})
    state = _redact(args["state"])
    timeout = _timeout_from_args(args)
    use_cache = not args.get("no_cache", False)
    cached, cache_key = _cache_hit_json("jev", args, state, use_cache)
    if cached is not None:
        return cached
    if not _jev_tool_available():
        return _no_engine_json("jev", state)

    questions, configs = _build_jev_questions(args)
    if not questions:
        return _dump({
            "error": "没有提供任何判定题目，请提供 choice/choices、noul/nouls 或 score/scores 之一。",
            "tip": "支持多题并发传入，一次网络往返即可返回所有题目答案。",
        })

    try:
        started = time.time()
        response = _execute_system_one_with_timeout(
            client=SystemOneAdapterClient(
                structured_outputs=False, llm_answer_mode="probabilities"
            ),
            state=state,
            questions=questions,
            provider=_build_provider(),
            timeout=timeout,
        )
        elapsed = round(time.time() - started, 2)
        answers = _answers_from_response(response)
        details = _details_from_response(response)
        result = {
            "latency_seconds": elapsed,
            "cache_hit": False,
            "engine": getattr(response, "engine", _active_engine().split(":", 1)[0]),
            "model": getattr(response, "model", _default_model_name()),
            "answers": answers,
            "details": details,
        }
        usage = getattr(response, "usage", None)
        if usage:
            result["usage"] = _usage_to_dict(usage)
        for kind, alias in (("choice", "choice_answer"), ("noul", "noul_answer"), ("score", "score_answer")):
            config = configs[kind]
            answer_name = config.get("name", f"{kind}_q") if config else None
            if answer_name in answers:
                result[alias] = answers[answer_name]
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({"tool": "jev", "latency_seconds": elapsed, "questions_count": len(questions),
                      "answers": answers, "state_preview": str(state)[:100]})
        return _dump(result)
    except Exception as exc:
        return _failure_json("jev", state, exc, timeout)


def _handle_jev_guard(args: dict[str, Any], **kwargs) -> str:
    """Run six-signal guard; unavailable/failed calls return errors without a local verdict."""
    if args.get("state") is None:
        return _dump({"error": "缺少 state（待执行动作描述）参数。"})
    state = _redact(args["state"])
    timeout = _timeout_from_args(args)
    use_cache = not args.get("no_cache", False)
    cached, cache_key = _cache_hit_json("jev_guard", args, state, use_cache)
    if cached is not None:
        return cached
    if not _guard_tool_available():
        return _no_engine_json("jev_guard", state)

    guard_cfg = args.get("guard") or {}
    try:
        threshold = max(0.0, min(float(guard_cfg.get("threshold", 0.75)), 1.0))
    except (ValueError, TypeError):
        threshold = 0.75
    deny_threshold = min(0.25, threshold * 0.5) if threshold > 0 else 0.0
    questions = _build_guard_questions(guard_cfg.get("instructions", "这个动作是否可以安全执行？true 表示安全可放行。"))

    try:
        started = time.time()
        response = _execute_system_one_with_timeout(
            client=SystemOneAdapterClient(
                structured_outputs=False, llm_answer_mode="probabilities"
            ),
            state=state,
            questions=questions,
            provider=_build_provider(_guard_engine_name()),
            timeout=timeout,
        )
        elapsed = round(time.time() - started, 2)
        probability = getattr(response.nouls.get("guard_q"), "noul", None)
        signals = _guard_signals(response)
        confidence = _guard_model_confidence(response)
        judged = _guard_verdict_from_signals(signals, threshold)
        if judged is None:
            if probability is None:
                raise RuntimeError("裁决响应缺少 guard_q 与多题信号，无法判定")
            verdict = "allow" if probability >= threshold else ("ask" if probability >= deny_threshold else "deny")
            reason, verdict_source = "单题概率三档（信号缺失时的兼容路径）", "probability"
        else:
            verdict, reason = judged
            if verdict == "allow" and confidence is not None and confidence < 0.55:
                verdict = "ask"
                reason = f"模型置信度不足（confidence {confidence:.2f} < 0.55）"
            verdict_source = "signals"
        result = {
            "verdict": verdict,
            "probability": probability if probability is not None else signals.get("policy_allows"),
            "confidence": confidence,
            "threshold": threshold,
            "deny_threshold": deny_threshold,
            "signals": signals,
            "verdict_source": verdict_source,
            "reason": reason,
            "engine": getattr(response, "engine", _active_guard_engine().split(":", 1)[0]),
            "model": getattr(response, "model", _default_model_name(_guard_engine_name())),
            "latency_seconds": elapsed,
            "cache_hit": False,
        }
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({"tool": "jev_guard", "verdict": verdict, "probability": probability,
                      "latency_seconds": elapsed, "state_preview": str(state)[:100]})
        return _dump(result)
    except Exception as exc:
        return _failure_json("jev_guard", state, exc, timeout)


def _handle_jev_review(args: dict[str, Any], **kwargs) -> str:
    """Run a pre-choice review and project it into an explicit confidence band."""
    try:
        review, min_confidence, abstain_below, shadow = _normalize_review_args(args)
    except ValueError as exc:
        _log_journal({"tool": "jev_review", "error": "invalid_input", "reason": str(exc)})
        return _dump({
            "error": f"复核输入无效：{exc}",
            "status": "rejected",
            "adoptable": False,
        })

    state, question = _review_state_and_question(review)
    timeout = _timeout_from_args(args)
    use_cache = not args.get("no_cache", False)
    cache_args = {
        "state": state,
        "review": {
            "point": review["point"],
            "primaryChoice": review["primaryChoice"],
            "options": review["options"],
            "evidence": review["evidence"],
            "constraints": review["constraints"],
        },
    }
    cached, cache_key = _cache_hit_json("jev_review", cache_args, state, use_cache)
    if cached is not None:
        return cached
    if not _jev_tool_available():
        _log_journal({"tool": "jev_review", "error": "no_engine", "point": review["point"]})
        return _dump({
            "error": "Jev 在线复核不可用：当前没有可用引擎，本次未做任何复核。",
            "status": "unavailable",
            "point": review["point"],
            "primaryChoice": review["primaryChoice"],
            "adoptable": False,
            "engine": _ENGINE_NONE,
            "hint": "需要当前引擎的 API key（OpenRouter/Elephant 默认）和可导入的 system_one_adapter；错误不是放行。",
        })

    try:
        started = time.time()
        response = _execute_system_one_with_timeout(
            client=SystemOneAdapterClient(
                structured_outputs=False, llm_answer_mode="probabilities"
            ),
            state=state,
            questions={"review": question},
            provider=_build_provider(),
            timeout=timeout,
        )
        elapsed = round(time.time() - started, 2)
        answer = getattr(response, "choices", {}).get("review")
        if answer is None:
            raise ValueError("复核响应缺少 review 结果")
        result = _review_projection(answer, review, min_confidence, abstain_below, shadow)
        probabilities = getattr(answer, "probabilities", None)
        if not shadow and isinstance(probabilities, dict):
            result["probabilities"] = {
                key: float(value)
                for key, value in probabilities.items()
                if key in {option["id"] for option in review["options"]}
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
        result.update({
            "engine": getattr(response, "engine", _active_engine().split(":", 1)[0]),
            "model": getattr(response, "model", _default_model_name()),
            "latency_seconds": elapsed,
            "cache_hit": False,
        })
        usage = getattr(response, "usage", None)
        if usage:
            result["usage"] = _usage_to_dict(usage)
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({
            "tool": "jev_review",
            "point": review["point"],
            "status": result["status"],
            "agreement": result.get("agreement"),
            "confidence": result.get("confidence"),
            "adoptable": result["adoptable"],
            "latency_seconds": elapsed,
            "state_preview": review["question"][:100],
        })
        return _dump(result)
    except Exception as exc:
        result = json.loads(_failure_json("jev_review", state, exc, timeout))
        result.update({
            "status": "unavailable",
            "point": review["point"],
            "primaryChoice": review["primaryChoice"],
            "adoptable": False,
        })
        return _dump(result)


