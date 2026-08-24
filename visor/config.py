"""Settings, all overridable by environment variable."""
import os

# Where Ollama lives. The server, usually.
OLLAMA_URL = os.environ.get("VISOR_OLLAMA_URL", "http://127.0.0.1:11434")

# Analysis model. Wants a large context window far more than raw parameter count:
# context assembly is the product, and the model's job is to read it all at once.
MODEL = os.environ.get("VISOR_MODEL", "huihui_ai/qwen3-coder-next-abliterated:latest")

# Context window to request. Analysis prompts get big.
NUM_CTX = int(os.environ.get("VISOR_NUM_CTX", "65536"))

# Reasoning effort, where the model supports it.
THINKING = os.environ.get("VISOR_THINKING", "medium")

# A single call can be long. This is not an interactive tool.
TIMEOUT_SECONDS = int(os.environ.get("VISOR_TIMEOUT", "3600"))

# Context assembly budget. Roughly 4 chars per token; keep well inside NUM_CTX
# so the model has room to think.
MAX_CONTEXT_CHARS = int(os.environ.get("VISOR_MAX_CONTEXT_CHARS", "120000"))
MAX_FILE_CHARS = int(os.environ.get("VISOR_MAX_FILE_CHARS", "20000"))
TRACE_CONTEXT_LINES = 40  # lines either side of a stack-trace hit

# Prose bug reports name no files and carry no traceback, so the extractors
# find nothing to attach. Rather than hand the model a complaint and a file
# listing, fall back to sending the source itself when there is room.
FALLBACK_WHEN_FEWER_THAN = int(os.environ.get("VISOR_FALLBACK_MIN_FILES", "2"))
FALLBACK_MAX_FILES = int(os.environ.get("VISOR_FALLBACK_MAX_FILES", "15"))

# Files worth reading as source when they turn up in an issue.
SOURCE_SUFFIXES = (
    ".gd", ".py", ".cs", ".cpp", ".h", ".hpp", ".c", ".js", ".ts",
    ".tscn", ".tres", ".godot", ".cfg", ".json", ".toml", ".yaml", ".yml",
    ".sh", ".md", ".txt",
)

# Never read these, however tempting.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "build", "dist", ".import", ".godot", "addons"}

# Where transcripts go. Every call is logged in full; this is a slow loop and
# you should be able to see exactly what the model was asked.
LOG_DIR = os.environ.get("VISOR_LOG_DIR", os.path.expanduser("~/.ai-visor/logs"))
