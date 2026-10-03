#!/usr/bin/env python3
"""Media jobs: transcription, speech and images. Each is a fixed program run on a
note's inputs, never an agent: whisper.cpp, Kokoro, ComfyUI, and ffmpeg between
them. Every one runs inside media_wall.sh, with no network, seeing only its own
program, its models, and the job's scratch folder.

The dispatcher calls these with a job folder of their own and copies what they
make into the notes afterwards; nothing here writes to the notes.

ComfyUI is the one with a history of trouble: its add-ons ("custom nodes") are
arbitrary Python from the internet. Visor starts it with every add-on disabled,
builds each workflow itself from built-in nodes only, loads only .safetensors
weights (the older formats can run code when loaded), and talks to it through a
socket in the job folder, since it has no network to listen on.
"""

import configparser
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import time
import wave

HERE = os.path.dirname(os.path.realpath(__file__))
AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".webm", ".mp4", ".mov", ".aac")
# A starting prompt sets the transcript's style. Whisper learned from subtitles that
# sometimes softened swearing; an example of verbatim speech keeps it verbatim.
WHISPER_PROMPT = ("A verbatim transcript, every word as spoken, swearing and explicit words included, "
                  "uncensored: \"Oh, fuck, that was so damn good. Shit, do it again.\"")


class MediaError(Exception):
    """Why a media job could not be done, told to the owner in the note."""


class Settings:
    def __init__(self, section=None, state_dir="/tmp"):
        get = (section or {}).get
        tools = get("tools", "/srv/code/tools")
        self.whisper = get("whisper", f"{tools}/whisper.cpp/build/bin/whisper-cli")
        self.whisper_dir = get("whisper_dir", f"{tools}/whisper.cpp")
        self.whisper_model = get("whisper_model", f"{tools}/models/ggml-large-v3-turbo.bin")
        self.whisper_prompt = get("whisper_prompt", WHISPER_PROMPT)
        self.threads = get("threads", "16")
        self.kokoro_python = get("kokoro_python", f"{tools}/kokoro-venv/bin/python")
        self.kokoro_model = get("kokoro_model", f"{tools}/models/kokoro-v1.0.onnx")
        self.kokoro_voices = get("kokoro_voices", f"{tools}/models/voices-v1.0.bin")
        self.comfyui = get("comfyui", f"{tools}/ComfyUI")
        self.comfy_python = get("comfy_python", f"{tools}/comfy-venv/bin/python")
        self.comfy_models = get("comfy_models", f"{tools}/models/comfy")
        self.work = get("work", os.path.join(state_dir, "media"))
        # How long a job may take before it is stopped, in minutes.
        self.minutes = int(get("minutes", "360"))
        self.sandbox = (get("sandbox", "yes") or "yes").lower() != "no"


def settings_from(config_path, state_dir):
    parser = configparser.ConfigParser()
    parser.read(config_path)
    return Settings(parser["media"] if parser.has_section("media") else None, state_dir)


# ── Running a tool walled in ──────────────────────────────────────────────────

def _venv_root(python):
    """A Python environment's folder, from its interpreter's path (…/venv/bin/python)."""
    return os.path.dirname(os.path.dirname(python))


def walled(settings, job, readable, command, timeout=None, background=False, log=None):
    """Run command inside media_wall.sh, able to read `readable` and write only `job`."""
    tool = os.path.basename(command[0])
    if settings.sandbox:
        wall = [os.path.join(HERE, "media_wall.sh"), job]
        for path in readable:
            wall += ["--ro", path]
        command = wall + ["--"] + list(command)
    if background:
        out = open(log, "wb") if log else subprocess.DEVNULL
        return subprocess.Popen(command, cwd=job, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        done = subprocess.run(command, cwd=job, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise MediaError(f"`{tool}` did not finish within {timeout // 60} minutes and was stopped.")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip()[-800:]
        raise MediaError(f"`{tool}` failed (exit {done.returncode}):\n\n```\n{tail}\n```")
    return done


def _require(*paths, what):
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise MediaError(f"{what} is not installed on this server yet: missing `{missing[0]}`.")


def _audio_seconds(wav_path):
    with wave.open(wav_path) as w:
        return w.getnframes() / w.getframerate()


def _to_wav(settings, job, source, out_name, rate=16000):
    walled(settings, job, [], ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", source,
                               "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le", out_name],
           timeout=settings.minutes * 60)
    return os.path.join(job, out_name)


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(settings, audio, job, language="auto"):
    """The words spoken in an audio file. Returns {"text", "seconds"}."""
    _require(settings.whisper, settings.whisper_model, what="Transcription (whisper.cpp)")
    source = os.path.join(job, "input" + os.path.splitext(audio)[1].lower())
    shutil.copyfile(audio, source)
    wav = _to_wav(settings, job, os.path.basename(source), "audio.wav")
    walled(settings, job, [settings.whisper_dir, os.path.dirname(settings.whisper), settings.whisper_model],
           [settings.whisper, "-m", settings.whisper_model, "-f", "audio.wav", "-otxt", "-of", "transcript",
            "-l", language or "auto", "-t", str(settings.threads), "-np", "--prompt", settings.whisper_prompt],
           timeout=settings.minutes * 60)
    with open(os.path.join(job, "transcript.txt"), encoding="utf-8", errors="replace") as f:
        text = f.read().strip()
    return {"text": text, "seconds": _audio_seconds(wav)}


# ── Speech ────────────────────────────────────────────────────────────────────

def speech_text(markdown):
    """Markdown as it should be read aloud: no front matter, markup, links' targets or code."""
    text = re.sub(r"\A---\n.*?\n---\n", "", markdown, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^>[ \t]*\[![^\]]*\][-+]?.*$", "", text, flags=re.MULTILINE)  # callout headings
    text = re.sub(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)", "", text)           # embeds
    text = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", text)                 # [[target|shown]]
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)                        # [shown](target)
    text = re.sub(r"<[^>]+>", "", text)
    # [ \t], not \s: a pattern that may cross a line end swallows the blank line before it.
    text = re.sub(r"^[ \t]{0,3}(#{1,6}[ \t]*|>[ \t]?|[-*+][ \t]+|\d+\.[ \t]+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__|\*|_|~~|`)", "", text)
    text = re.sub(r"^[ \t]*([-*_][ \t]*){3,}$", "", text, flags=re.MULTILINE)  # rules
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def speak(settings, text, job, voice="af_heart", speed="1.0", language="en-us"):
    """Read text aloud. Returns {"files": [mp3], "seconds"}."""
    _require(settings.kokoro_python, settings.kokoro_model, settings.kokoro_voices, what="Speech (Kokoro)")
    if not text.strip():
        raise MediaError("there is no text to read: write it below the header, or embed a note with `![[…]]`.")
    try:
        float(speed)
    except ValueError:
        raise MediaError(f"`Speed: {speed}` is not a number: use 1.0 for normal, 0.8 slower, 1.2 faster.")
    with open(os.path.join(job, "text.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    walled(settings, job, [_venv_root(settings.kokoro_python), settings.kokoro_model, settings.kokoro_voices,
                           os.path.join(HERE, "media")],
           [settings.kokoro_python, os.path.join(HERE, "media", "speak.py"), "text.txt", "speech.wav",
            settings.kokoro_model, settings.kokoro_voices, voice, str(speed), language],
           timeout=settings.minutes * 60)
    seconds = _audio_seconds(os.path.join(job, "speech.wav"))
    walled(settings, job, [], ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", "speech.wav",
                               "-codec:a", "libmp3lame", "-q:a", "4", "speech.mp3"], timeout=settings.minutes * 60)
    return {"files": [os.path.join(job, "speech.mp3")], "seconds": seconds}


# ── Images ────────────────────────────────────────────────────────────────────

# The image models visor knows, by the name a note uses. "fast" models are SDXL ones
# that can take the DMD2 add-on, which cuts 30 steps to 8: on a CPU, the difference
# between a quarter of an hour and a few minutes per image.
DMD2 = "dmd2_sdxl_4step_lora_fp16.safetensors"
IMAGE_MODELS = {
    "lustify": {"file": "lustifyNSFWCheckpoint_zenithV9.safetensors", "kind": "sdxl", "fast": True,
                "about": "SDXL, photographic, made for adult content"},
    "big-lust": {"file": "bigLust_v16.safetensors", "kind": "sdxl", "fast": True,
                 "about": "SDXL, photographic, explicit-tuned"},
    "pony": {"file": "ponyDiffusionV6XL_v6StartWithThisOne.safetensors", "kind": "sdxl", "fast": True,
             "clip_skip": 2, "prefix": "score_9, score_8_up, score_7_up, ",
             "about": "Pony Diffusion V6 XL, illustrated and anime; prompts in tags"},
    "noobai": {"file": "noobaiXLNAIXL_vPred10Version.safetensors", "kind": "sdxl", "vpred": True,
               "steps": 28, "cfg": 4.5, "sampler": "euler", "scheduler": "normal",
               "about": "NoobAI-XL, anime; no fast mode, so slow on this machine"},
    "chroma": {"file": "Chroma1-HD.safetensors", "kind": "chroma", "text_encoder": "t5xxl_fp8_e4m3fn.safetensors",
               "vae": "ae.safetensors", "steps": 26, "cfg": 3.8, "sampler": "euler", "scheduler": "beta",
               "negative": "low quality, ugly, unfinished, out of focus, deformed, disfigured, blurry, smudged, "
                           "restricted palette, flat colors, watermark, signature",
               "about": "Chroma1-HD (Flux class), photographic; the best here, and by far the slowest"},
    "realistic-vision": {"file": "realisticVisionV60B1_v51HyperVAE_418901.safetensors", "kind": "sd15",
                         "width": 512, "height": 768, "steps": 6, "cfg": 1.5,
                         "sampler": "dpmpp_sde", "scheduler": "karras",
                         "about": "Realistic Vision V6 (SD 1.5), photographic; the fastest"},
}
DEFAULT_IMAGE_MODEL = "realistic-vision"
NEGATIVE = "lowres, blurry, deformed, bad anatomy, extra limbs, watermark, text, signature"


def image_spec(options):
    """What to generate, from the note's lines, with each model's defaults filled in."""
    name = (options.get("model") or DEFAULT_IMAGE_MODEL).strip().lower()
    if name not in IMAGE_MODELS:
        raise MediaError(f"`Model: {name}` is not an image model visor knows: use "
                         f"{', '.join(sorted(IMAGE_MODELS))}.")
    model = IMAGE_MODELS[name]
    prompt = (options.get("prompt") or "").strip()
    if not prompt:
        raise MediaError("there is no prompt: write what to draw after `Image:` or below the header.")
    sdxl = model["kind"] == "sdxl"
    big = model["kind"] in ("sdxl", "chroma")
    quality = (options.get("quality") or "fast").strip().lower()
    if quality not in ("fast", "full"):
        raise MediaError(f"`Quality: {quality}` is not one visor knows: use fast or full.")
    fast = sdxl and model.get("fast") and quality == "fast"
    width, height = model.get("width", 1024 if big else 512), model.get("height", 1024 if big else 768)
    if options.get("size"):
        m = re.fullmatch(r"\s*(\d{3,4})\s*[x×]\s*(\d{3,4})\s*", options["size"])
        if not m:
            raise MediaError(f"`Size: {options['size']}` is not a size: write it as 832x1216.")
        width, height = int(m.group(1)) // 8 * 8, int(m.group(2)) // 8 * 8
        if not (256 <= width <= 2048 and 256 <= height <= 2048):
            raise MediaError("a size must be between 256 and 2048 pixels each way.")
    def number(key, default, low, high):
        value = options.get(key)
        if value is None or str(value).strip() == "":
            return default
        try:
            n = int(str(value).strip())
        except ValueError:
            raise MediaError(f"`{key.capitalize()}: {value}` is not a whole number.")
        if not low <= n <= high:
            raise MediaError(f"`{key.capitalize()}:` must be between {low} and {high}.")
        return n
    return {
        "name": name, "file": model["file"], "kind": model["kind"],
        "prompt": model.get("prefix", "") + prompt,
        "negative": (options.get("negative") or model.get("negative", NEGATIVE)).strip(),
        "text_encoder": model.get("text_encoder"), "vae": model.get("vae"),
        "width": width, "height": height,
        "count": number("count", 1, 1, 8),
        "seed": number("seed", int(time.time() * 1000) % 2**32, 0, 2**32 - 1),
        "steps": number("steps", 8 if fast else model.get("steps", 30), 1, 150),
        "cfg": 1.0 if fast else model.get("cfg", 4.0),
        "sampler": "lcm" if fast else model.get("sampler", "dpmpp_2m_sde"),
        "scheduler": "sgm_uniform" if fast else model.get("scheduler", "karras"),
        "lora": DMD2 if fast else None,
        "clip_skip": model.get("clip_skip"),
        "vpred": bool(model.get("vpred")),
        "fast": bool(fast),
    }


def comfy_graph(spec):
    """A ComfyUI workflow, in its API form, from built-in nodes only."""
    if spec["kind"] == "chroma":
        return _chroma_graph(spec)
    graph = {"checkpoint": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": spec["file"]}}}
    model, clip, vae = ["checkpoint", 0], ["checkpoint", 1], ["checkpoint", 2]
    if spec["lora"]:
        graph["lora"] = {"class_type": "LoraLoader", "inputs": {
            "model": model, "clip": clip, "lora_name": spec["lora"], "strength_model": 1.0, "strength_clip": 1.0}}
        model, clip = ["lora", 0], ["lora", 1]
    if spec["clip_skip"]:
        graph["clip_skip"] = {"class_type": "CLIPSetLastLayer",
                              "inputs": {"clip": clip, "stop_at_clip_layer": -spec["clip_skip"]}}
        clip = ["clip_skip", 0]
    if spec["vpred"]:
        graph["vpred"] = {"class_type": "ModelSamplingDiscrete",
                          "inputs": {"model": model, "sampling": "v_prediction", "zsnr": True}}
        model = ["vpred", 0]
    graph["positive"] = {"class_type": "CLIPTextEncode", "inputs": {"text": spec["prompt"], "clip": clip}}
    graph["negative"] = {"class_type": "CLIPTextEncode", "inputs": {"text": spec["negative"], "clip": clip}}
    graph["latent"] = {"class_type": "EmptyLatentImage",
                       "inputs": {"width": spec["width"], "height": spec["height"], "batch_size": spec["count"]}}
    graph["sampler"] = {"class_type": "KSampler", "inputs": {
        "model": model, "positive": ["positive", 0], "negative": ["negative", 0], "latent_image": ["latent", 0],
        "seed": spec["seed"], "steps": spec["steps"], "cfg": spec["cfg"], "sampler_name": spec["sampler"],
        "scheduler": spec["scheduler"], "denoise": 1.0}}
    graph["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": vae}}
    graph["save"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "visor"}}
    return graph


def _chroma_graph(spec):
    """Chroma's own workflow, as its author publishes it for ComfyUI: the model, the T5 text
    encoder and the Flux decoder are separate files, and sampling is the custom kind."""
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": spec["file"], "weight_dtype": "default"}},
        "shift": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 1.0}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": spec["text_encoder"], "type": "chroma"}},
        "t5": {"class_type": "T5TokenizerOptions", "inputs": {"clip": ["clip", 0], "min_padding": 1, "min_length": 0}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": spec["vae"]}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": spec["prompt"], "clip": ["t5", 0]}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": spec["negative"], "clip": ["t5", 0]}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["shift", 0], "positive": ["positive", 0],
                                                         "negative": ["negative", 0], "cfg": spec["cfg"]}},
        "pick": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": spec["sampler"]}},
        "sigmas": {"class_type": "BetaSamplingScheduler",
                   "inputs": {"model": ["shift", 0], "steps": spec["steps"], "alpha": 0.45, "beta": 0.45}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": spec["seed"]}},
        "latent": {"class_type": "EmptySD3LatentImage",
                   "inputs": {"width": spec["width"], "height": spec["height"], "batch_size": spec["count"]}},
        "sampler": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["pick", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "visor"}},
    }


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=60):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _comfy(sock, method, path, body=None):
    conn = _UnixHTTP(sock)
    try:
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Content-Type": "application/json"} if body is not None else {})
        reply = conn.getresponse()
        data = reply.read()
        return reply.status, (json.loads(data) if data.strip() else {})
    except http.client.HTTPException as error:  # the connection dropped mid-answer
        raise OSError(f"ComfyUI dropped the connection: {error!r}") from error
    finally:
        conn.close()


def _ask(sock, job, method, path, body=None):
    """A request to ComfyUI once it is up: any failure to talk to it is told to the owner."""
    try:
        return _comfy(sock, method, path, body)
    except (OSError, ValueError) as error:
        raise MediaError(f"lost touch with ComfyUI ({error}):\n\n```\n{_tail(job)}\n```")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def generate(settings, options, job, on_progress=None):
    """Draw images. Returns {"files": [png...], "spec"}."""
    spec = image_spec(options)
    main = os.path.join(settings.comfyui, "main.py")
    if spec["kind"] == "chroma":
        weights = [os.path.join(settings.comfy_models, "diffusion_models", spec["file"]),
                   os.path.join(settings.comfy_models, "text_encoders", spec["text_encoder"]),
                   os.path.join(settings.comfy_models, "vae", spec["vae"])]
    else:
        weights = [os.path.join(settings.comfy_models, "checkpoints", spec["file"])]
    if spec["lora"]:
        weights.append(os.path.join(settings.comfy_models, "loras", spec["lora"]))
    _require(main, settings.comfy_python, *weights, what=f"Image generation ({spec['name']})")
    for folder in ("out", "temp", "user", "input"):
        os.makedirs(os.path.join(job, folder), exist_ok=True)
    paths = os.path.join(job, "model-paths.yaml")
    with open(paths, "w") as f:
        f.write(f"visor:\n  base_path: {settings.comfy_models}\n  checkpoints: checkpoints\n  loras: loras\n"
                "  vae: vae\n  text_encoders: text_encoders\n  diffusion_models: diffusion_models\n")
    sock, port = os.path.join(job, "comfy.sock"), _free_port()
    # ComfyUI listens on a port inside the wall, where the network is its own and empty;
    # socat joins that port to a socket in the job folder, which visor can reach.
    serve = ["sh", "-c", 'sock=$0; port=$1; shift; socat UNIX-LISTEN:"$sock",fork TCP:127.0.0.1:"$port" & exec "$@"',
             sock, str(port), settings.comfy_python, main, "--cpu", "--listen", "127.0.0.1", "--port", str(port),
             # No add-ons, no paid cloud nodes; and the built-in add-on installer stays off,
             # since it runs only when given --enable-manager.
             "--disable-auto-launch", "--disable-all-custom-nodes", "--disable-api-nodes",
             "--output-directory", os.path.join(job, "out"), "--temp-directory", os.path.join(job, "temp"),
             "--user-directory", os.path.join(job, "user"), "--input-directory", os.path.join(job, "input"),
             "--extra-model-paths-config", paths]
    readable = [settings.comfyui, _venv_root(settings.comfy_python), settings.comfy_models]
    server = walled(settings, job, readable, serve, background=True, log=os.path.join(job, "comfy.log"))
    deadline = time.time() + settings.minutes * 60
    try:
        for _ in range(600):  # starting ComfyUI loads Python and PyTorch: up to a few minutes
            if server.poll() is not None:
                raise MediaError("ComfyUI stopped while starting:\n\n```\n" + _tail(job) + "\n```")
            try:
                if _comfy(sock, "GET", "/system_stats")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise MediaError("ComfyUI did not start within five minutes:\n\n```\n" + _tail(job) + "\n```")
        status, reply = _ask(sock, job, "POST", "/prompt", {"prompt": comfy_graph(spec), "client_id": "visor"})
        if status != 200 or "prompt_id" not in reply:
            raise MediaError(f"ComfyUI turned the workflow down: {json.dumps(reply)[:800]}")
        prompt_id = reply["prompt_id"]
        while True:
            if time.time() > deadline:
                raise MediaError(f"the images were not finished within {settings.minutes} minutes.")
            if server.poll() is not None:
                raise MediaError("ComfyUI stopped while drawing:\n\n```\n" + _tail(job) + "\n```")
            _, history = _ask(sock, job, "GET", f"/history/{prompt_id}")
            entry = history.get(prompt_id)  # absent until the workflow has finished, one way or the other
            if entry:
                state = entry.get("status") or {}
                if state.get("status_str") == "error":
                    raise MediaError("ComfyUI failed: " + json.dumps(state.get("messages"))[-800:])
                break
            if on_progress:
                on_progress()
            time.sleep(5)
        files = []
        for output in entry["outputs"].values():
            for image in output.get("images", []):
                path = os.path.join(job, "out", image.get("subfolder", ""), image["filename"])
                if image.get("type") == "output" and path.endswith(".png") and os.path.isfile(path):
                    files.append(path)
        if not files:
            raise MediaError("ComfyUI finished but saved no images.")
        return {"files": files, "spec": spec}
    finally:
        try:
            os.killpg(server.pid, 15)
        except ProcessLookupError:
            pass
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, 9)


def _tail(job):
    try:
        with open(os.path.join(job, "comfy.log"), encoding="utf-8", errors="replace") as f:
            return f.read()[-1500:].strip()
    except FileNotFoundError:
        return "(no log)"
