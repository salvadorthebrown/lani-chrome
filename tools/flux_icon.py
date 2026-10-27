#!/usr/bin/env python3
"""Generates Lani icon candidates with Flux schnell through the local ComfyUI API (runs on the Mule)."""
import json, pathlib, sys, time, urllib.request, urllib.parse

API = "http://127.0.0.1:8286"
OUT = pathlib.Path.home() / "lani-icons"
OUT.mkdir(exist_ok=True)

STYLE = ("app icon design, flat vector illustration with subtle gradients, bold simple shapes, centered, "
         "on a smooth deep indigo to teal gradient background, minimal, clean, no text, no border")
PROMPTS = {
    "hibiscus": "a single hibiscus flower seen from the front, five soft rounded pink-magenta petals with a deep crimson "
                "center and one long golden stamen with yellow pollen, " + STYLE,
    "lily": "a single stargazer lily flower seen from the front, six pointed white and pink petals with darker magenta "
            "streaks and speckles, golden center with dark red stamens, " + STYLE,
}
JOBS = [("hibiscus", s) for s in (11, 22, 33, 44)] + [("lily", s) for s in (11, 22)]


def call(path, payload=None):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=30).read()


def workflow(prompt, seed, prefix):
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux1-schnell-Q4_K_S.gguf"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
                                                        "clip_name2": "clip_l.safetensors", "type": "flux"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": seed, "steps": 4, "cfg": 1.0,
                                                  "sampler_name": "euler", "scheduler": "simple",
                                                  "positive": ["3", 0], "negative": ["4", 0],
                                                  "latent_image": ["5", 0], "denoise": 1.0}},
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["7", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }


def run(name, seed):
    started = time.time()
    try:
        reply = json.loads(call("/prompt", {"prompt": workflow(PROMPTS[name], seed, f"lani_{name}_{seed}")}))
    except urllib.error.HTTPError as err:
        print(f"{name} {seed}: ComfyUI rejected the workflow: {err.read().decode()[:600]}", flush=True)
        return False
    pid = reply["prompt_id"]
    while time.time() - started < 600:
        history = json.loads(call(f"/history/{pid}"))
        if pid in history:
            status = history[pid]["status"]
            if status.get("status_str") == "error":
                print(f"{name} {seed}: failed: {json.dumps(status)[:600]}", flush=True)
                return False
            for node in history[pid]["outputs"].values():
                for img in node.get("images", []):
                    query = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img["subfolder"], "type": img["type"]})
                    data = call("/view?" + query)
                    (OUT / f"{name}_{seed}.png").write_bytes(data)
                    print(f"{name} {seed}: saved {name}_{seed}.png in {time.time() - started:.0f}s", flush=True)
                    return True
        time.sleep(2)
    print(f"{name} {seed}: timed out", flush=True)
    return False


if __name__ == "__main__":
    nodes = json.loads(call("/object_info/UnetLoaderGGUF"))
    print("GGUF loader present:", "UnetLoaderGGUF" in nodes, flush=True)
    for name, seed in JOBS:
        run(name, seed)
    print("[done]", flush=True)
