"""AstrBot usage adapters. Never infer a zero charge from absent usage."""

from .billing import Usage


def field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def token_usage(value):
    values = [field(value, k) for k in ("input_cached", "input_other", "output")]
    if any(type(v) is not int or v < 0 for v in values):
        raise ValueError("缺少可信 Token 用量")
    if sum(values) == 0:
        raise ValueError("用量全部为零，需核对")
    return Usage(*values)


def response_usage(response):
    usage = field(response, "usage")
    if usage is not None:
        return token_usage(usage)
    raw = field(field(response, "raw_completion"), "usage")
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise ValueError("模型未返回用量")
    if "prompt_cache_hit_tokens" in raw:
        return Usage.parse(raw)
    prompt, output = raw.get("prompt_tokens"), raw.get("completion_tokens")
    hit = field(raw.get("prompt_tokens_details"), "cached_tokens", 0)
    if any(type(v) is not int or v < 0 for v in (prompt, output, hit)):
        raise ValueError("用量格式异常")
    if hit > prompt or raw.get("total_tokens") != prompt + output:
        raise ValueError("用量字段不一致")
    return token_usage(dict(input_cached=hit, input_other=prompt - hit, output=output))


def active_runner(event):
    # Optional compatibility adapter for cumulative tool-loop/streaming usage.
    # Verify event identity: never borrow another QQ's runner in the same group.
    try:
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
    except ImportError:
        return None
    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    owner = field(field(field(runner, "run_context"), "context"), "event")
    return runner if owner is event else None


def final_usage(event, response, tools_enabled, model):
    if field(response, "role") == "err":
        raise ValueError("模型返回错误，需核对是否产生费用")
    last = response_usage(response)
    raw_model = field(field(response, "raw_completion"), "model")
    if raw_model and raw_model != model:
        raise ValueError("响应模型与计价模型不一致")
    runner = active_runner(event)
    if runner is not None:
        actual_model = (
            field(field(runner, "req"), "model") or runner.provider.get_model()
        )
        if actual_model != model:
            raise ValueError("运行期间切换模型，需核对费用")
        total = token_usage(field(field(runner, "stats"), "token_usage"))
        if total.hit + total.miss + total.output < last.hit + last.miss + last.output:
            raise ValueError("累计用量小于末轮用量")
        return total
    if tools_enabled:
        raise ValueError("无法取得工具调用的累计用量")
    return last
