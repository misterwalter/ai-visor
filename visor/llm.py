"""One call to a local Ollama model. No agent loop, no tools, no retries-with-thinking."""
import json
import time
import urllib.error
import urllib.request

from . import config


class LlmError(RuntimeError):
    pass


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

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        config.OLLAMA_URL + "/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.TIMEOUT_SECONDS) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
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
