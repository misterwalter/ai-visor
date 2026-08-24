"""One call to a local Ollama model. No agent loop, no tools, no retries-with-thinking."""
import json
import time
import urllib.error
import urllib.request

from . import config


class LlmError(RuntimeError):
    pass


def _post(payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        config.OLLAMA_URL + "/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(messages, model=None, num_ctx=None, timeout=None, think=None):
    """Send one chat completion. Returns (text, stats)."""
    payload = {
        "model": model or config.MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_ctx": num_ctx or config.NUM_CTX},
    }
    level = think if think is not None else config.THINKING
    if level and level != "off":
        payload["think"] = level

    timeout = timeout or config.TIMEOUT_SECONDS
    started = time.time()
    try:
        result = _post(payload, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        # Plenty of models have no reasoning mode. Asking for one is a
        # preference, not a requirement — drop it and carry on.
        if "does not support thinking" in detail and "think" in payload:
            payload.pop("think")
            try:
                result = _post(payload, timeout)
            except urllib.error.HTTPError as exc2:
                detail2 = exc2.read().decode("utf-8", "replace")[:500]
                raise LlmError(f"HTTP {exc2.code} from Ollama: {detail2}") from exc2
        else:
            raise LlmError(f"HTTP {exc.code} from Ollama: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise LlmError(f"cannot reach Ollama at {config.OLLAMA_URL}: {exc}") from exc

    message = result.get("message", {})
    text = message.get("content", "")
    stats = {
        "wall_seconds": round(time.time() - started, 1),
        "prompt_tokens": result.get("prompt_eval_count"),
        "output_tokens": result.get("eval_count"),
        "model": result.get("model"),
        "thinking": payload.get("think", "off"),
    }
    if not text.strip():
        raise LlmError(f"model returned nothing (stats: {stats})")
    return text, stats


def available_models():
    try:
        with urllib.request.urlopen(config.OLLAMA_URL + "/api/tags", timeout=15) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in tags.get("models", [])]
    except Exception:
        return []
