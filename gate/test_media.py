#!/usr/bin/env python3
"""Tests for media.py. The tools are stand-ins (no models needed); the wall,
ffmpeg and socat are the real ones, so these also show each tool runs walled in.

    python3 gate/test_media.py
"""

import json
import os
import struct
import sys
import tempfile
import textwrap
import unittest
import wave
import zlib

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import media  # noqa: E402

FAKE_WHISPER = textwrap.dedent('''\
    #!/usr/bin/python3
    # Stands in for whisper-cli: writes <-of>.txt, saying what it was given.
    import os, sys
    a = sys.argv
    out = a[a.index("-of") + 1] + ".txt"
    reached = []
    for path in ("@HOME@", "@HOME@/.ssh"):
        if os.path.exists(path):
            reached.append(path)
    open(out, "w").write(f"hello from the stand-in, language {a[a.index('-l') + 1]}, "
                         f"prompt given: {'--prompt' in a}, outside paths seen: {reached}\\n")
    ''')

FAKE_KOKORO = textwrap.dedent('''\
    #!/usr/bin/python3
    # Stands in for Kokoro's Python: reads the text, writes a second of silence per line.
    import sys, wave
    _, script, text_file, out, model, voices, voice, speed, lang = sys.argv
    lines = [l for l in open(text_file).read().splitlines() if l.strip()]
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\\0\\0" * 24000 * len(lines))
    open("said.txt", "w").write(f"{voice} {speed} {lang}\\n" + open(text_file).read())
    ''')

# A stand-in for ComfyUI's API: checks the workflow uses only nodes it allows, and
# saves one small PNG per image asked for.
FAKE_COMFY = textwrap.dedent('''\
    import http.server, json, os, struct, sys, zlib
    args = sys.argv
    port = int(args[args.index("--port") + 1]); out = args[args.index("--output-directory") + 1]
    assert "--disable-all-custom-nodes" in args and "--disable-api-nodes" in args, "add-ons must be off"
    assert "--enable-manager" not in args, "the add-on installer must stay off"
    BUILT_IN = {"CheckpointLoaderSimple", "LoraLoader", "CLIPSetLastLayer", "ModelSamplingDiscrete",
                "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage"}
    def png():
        raw = b"\\x00\\xff\\x00\\x00"
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + \\
               chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    history = {}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path == "/system_stats": return self.reply(200, {"system": {}})
            pid = self.path.rsplit("/", 1)[-1]
            return self.reply(200, {pid: history[pid]} if pid in history else {})
        def do_POST(self):
            graph = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["prompt"]
            odd = {n["class_type"] for n in graph.values()} - BUILT_IN
            if odd: return self.reply(400, {"error": f"not built in: {sorted(odd)}"})
            open(os.path.join(out, "..", "graph.json"), "w").write(json.dumps(graph))
            count = next(n["inputs"]["batch_size"] for n in graph.values() if n["class_type"] == "EmptyLatentImage")
            images = []
            for i in range(count):
                name = f"visor_{i + 1:05d}_.png"
                open(os.path.join(out, name), "wb").write(png())
                images.append({"filename": name, "subfolder": "", "type": "output"})
            history["p1"] = {"status": {"status_str": "success", "completed": True},
                             "outputs": {"save": {"images": images}}}
            return self.reply(200, {"prompt_id": "p1"})
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
    ''')


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        tools = os.path.join(root, "tools")
        # The stand-in looks for this account's real home, which the wall must hide.
        self.write(os.path.join(tools, "whisper.cpp", "build", "bin", "whisper-cli"),
                   FAKE_WHISPER.replace("@HOME@", os.path.realpath(os.path.expanduser("~"))), 0o755)
        self.write(os.path.join(tools, "models", "ggml-large-v3-turbo.bin"), "weights")
        self.write(os.path.join(tools, "kokoro-venv", "bin", "python"), FAKE_KOKORO, 0o755)
        self.write(os.path.join(tools, "models", "kokoro-v1.0.onnx"), "weights")
        self.write(os.path.join(tools, "models", "voices-v1.0.bin"), "voices")
        self.write(os.path.join(tools, "ComfyUI", "main.py"), FAKE_COMFY)
        for name in ("lustifyNSFWCheckpoint_zenithV9.safetensors", "ponyDiffusionV6XL_v6StartWithThisOne.safetensors",
                     "noobaiXLNAIXL_vPred10Version.safetensors"):
            self.write(os.path.join(tools, "models", "comfy", "checkpoints", name), "weights")
        self.write(os.path.join(tools, "models", "comfy", "loras", media.DMD2), "weights")
        self.settings = media.Settings({"tools": tools, "comfy_python": "/usr/bin/python3", "minutes": "5"}, root)
        self.job = os.path.join(root, "job")
        os.makedirs(self.job)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text, mode=0o644):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)

    def wav(self, seconds):
        path = os.path.join(self.tmp.name, "voice note.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"\0\0\0\0" * 44100 * seconds)
        return path

    # Transcription

    def test_transcription_runs_walled_in_and_returns_the_text(self):
        result = media.transcribe(self.settings, self.wav(3), self.job, "en")
        self.assertIn("hello from the stand-in, language en", result["text"])
        self.assertIn("prompt given: True", result["text"])
        self.assertIn("outside paths seen: []", result["text"], "the wall hides the account's files")
        self.assertAlmostEqual(result["seconds"], 3, delta=0.1)
        with wave.open(os.path.join(self.job, "audio.wav")) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (16000, 1), "converted for whisper")

    def test_a_tool_not_yet_installed_says_so(self):
        os.remove(self.settings.whisper_model)
        with self.assertRaises(media.MediaError) as caught:
            media.transcribe(self.settings, self.wav(1), self.job)
        self.assertIn("not installed on this server yet", str(caught.exception))

    # Speech

    def test_speech_reads_the_text_and_makes_an_mp3(self):
        result = media.speak(self.settings, "First line.\n\nSecond line.", self.job, "bf_emma", "0.9", "en-gb")
        (mp3,) = result["files"]
        self.assertTrue(mp3.endswith(".mp3") and os.path.getsize(mp3) > 0)
        self.assertAlmostEqual(result["seconds"], 2, delta=0.1)
        said = open(os.path.join(self.job, "said.txt")).read()
        self.assertTrue(said.startswith("bf_emma 0.9 en-gb"))

    def test_markdown_is_read_as_prose(self):
        text = media.speech_text(textwrap.dedent('''\
            ---
            tags: draft
            ---
            # Chapter 2

            The **boat** comes in, says [[Ines|she]], and [a link](http://x) too.
            > [!note] An editing note
            ![[map.png]]
            ```
            code
            ```
            - a list item
            '''))
        self.assertEqual(text, "Chapter 2\n\nThe boat comes in, says she, and a link too.\n\na list item")

    def test_nothing_to_read_and_a_bad_speed_are_explained(self):
        with self.assertRaises(media.MediaError):
            media.speak(self.settings, "  ", self.job)
        with self.assertRaises(media.MediaError) as caught:
            media.speak(self.settings, "Hello.", self.job, speed="fast")
        self.assertIn("not a number", str(caught.exception))

    # Images

    def test_an_image_is_drawn_with_the_fast_add_on_and_built_in_nodes_only(self):
        result = media.generate(self.settings, {"prompt": "a lighthouse at dusk", "count": "2"}, self.job)
        self.assertEqual(len(result["files"]), 2)
        for path in result["files"]:
            with open(path, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        graph = json.load(open(os.path.join(self.job, "graph.json")))
        self.assertEqual(graph["lora"]["inputs"]["lora_name"], media.DMD2)
        self.assertEqual(graph["sampler"]["inputs"]["steps"], 8)
        self.assertEqual(graph["sampler"]["inputs"]["cfg"], 1.0)
        self.assertEqual(graph["latent"]["inputs"]["width"], 1024)

    def test_pony_gets_its_prompt_prefix_and_clip_skip_and_noobai_its_v_prediction(self):
        pony = media.comfy_graph(media.image_spec({"prompt": "a fox", "model": "pony"}))
        self.assertTrue(pony["positive"]["inputs"]["text"].startswith("score_9, score_8_up"))
        self.assertEqual(pony["clip_skip"]["inputs"]["stop_at_clip_layer"], -2)
        noob = media.image_spec({"prompt": "a fox", "model": "noobai"})
        self.assertFalse(noob["fast"])
        self.assertEqual(media.comfy_graph(noob)["vpred"]["inputs"]["sampling"], "v_prediction")

    def test_full_quality_drops_the_add_on(self):
        spec = media.image_spec({"prompt": "a fox", "quality": "full", "size": "832x1216"})
        self.assertIsNone(spec["lora"])
        self.assertEqual((spec["steps"], spec["width"], spec["height"]), (30, 832, 1216))

    def test_bad_image_lines_are_explained(self):
        for options, said in (({"prompt": ""}, "no prompt"), ({"prompt": "x", "model": "dalle"}, "not an image model"),
                              ({"prompt": "x", "size": "huge"}, "not a size"), ({"prompt": "x", "count": "50"}, "between"),
                              ({"prompt": "x", "quality": "best"}, "fast or full")):
            with self.assertRaises(media.MediaError) as caught:
                media.image_spec(options)
            self.assertIn(said, str(caught.exception))

    def test_an_image_model_not_downloaded_says_so(self):
        with self.assertRaises(media.MediaError) as caught:
            media.generate(self.settings, {"prompt": "x", "model": "big-lust"}, self.job)
        self.assertIn("not installed on this server yet", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
