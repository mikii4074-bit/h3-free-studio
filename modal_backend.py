"""
H3 Free Studio — Modal backend
==============================
Standalone, open-source backend that runs MiniMax H3 (video) and
MiniMax H3-Image (images) on YOUR OWN Modal account, using Modal's free
monthly Starter credit instead of a paid per-generation service.

    POST /jobs            {"type": "image"|"video", "input": {...}}  -> {"job_id": "..."}
    GET  /jobs/{id}       {"status": "queued|running|done|error", "result": {...}, "error": "..."}
    GET  /jobs/{id}/chunk/{i}     base64 chunks for results larger than 5 MB
    POST /jobs/{id}/cancel
    GET  /health

Quick start (full guide in README.md):
    pip install modal
    modal setup
    modal secret create huggingface HF_TOKEN=<your-hf-token>
    modal secret create h3-studio-api API_TOKEN=<choose-your-own-token>
    modal run modal_backend.py     # one-off weights download (~260 GB)
    modal deploy modal_backend.py

The core startup lesson of this project: both model classes use
`enable_memory_snapshot=True` with a TWO-PHASE enter —

  * `@modal.enter(snap=True)` runs on Modal's snapshot-creation boot, which
    has NO GPU attached. It loads the model into CPU RAM only. Any CUDA call
    in this phase crashes snapshot creation.
  * A plain `@modal.enter()` runs on every real (GPU) container start,
    including snapshot restores, and does the CUDA-touching setup there.

Together with `scaledown_window=1200` (a 20-minute warm window), a working
session starts in seconds from the snapshot instead of paying a full
~130 GB reload from the volume on every cold start.
"""
import base64, io, os, time, uuid
import modal

APP_NAME = "h3-free-studio"
H3_REPO = "MiniMaxAI/MiniMax-H3"
H3IMG_REPO = "MiniMaxAI/MiniMax-H3-Image"
DIFFUSERS_PIN = "git+https://github.com/huggingface/diffusers.git@665f578278365ea4a3318cb8c9b66ce6c01204b9"
DIFFUSERS_MAIN = "git+https://github.com/huggingface/diffusers.git"  # H3-Image support exists only in diffusers git main
# Community pipeline-split code (the stock pipeline does not fit on one
# GPU; these blocks split it in two) is vendored under `vendor/` in this
# repo — the container image copies it from here at build time and never
# downloads code from external sources. See vendor/README.md.
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
FPS, FRAMES_PER_CHUNK, LATENTS_PER_CHUNK = 24, 17, 5
CANVASES = {
    "960x544 · 16:9 fast": (544, 960), "1024x576 · 16:9 fast": (576, 1024), "1152x640 · 16:9": (640, 1152),
    "1280x704 · 16:9": (704, 1280), "1344x768 · 16:9 full": (768, 1344),
    "544x960 · 9:16 fast": (960, 544), "640x1152 · 9:16": (1152, 640), "768x1344 · 9:16 full": (1344, 768),
    "544x544 · 1:1 fast": (544, 544), "768x768 · 1:1 full": (768, 768), "1024x1024 · 1:1 max": (1024, 1024),
    "768x576 · 4:3 fast": (576, 768), "1024x768 · 4:3 full": (768, 1024),
    "576x768 · 3:4 fast": (768, 576), "768x1024 · 3:4 full": (1024, 768),
    "1152x512 · 21:9 fast": (512, 1152), "1536x672 · 21:9 full": (672, 1536),
}
DEFAULT_CANVAS = "960x544 · 16:9 fast"
CHUNK = 3_000_000          # base64 chars per result chunk
SEG_SECONDS = 10.0         # target length of one video segment before stitching

app = modal.App(APP_NAME)
wvol = modal.Volume.from_name("h3-studio-weights", create_if_missing=True)
rvol = modal.Volume.from_name("h3-studio-results", create_if_missing=True)
jobs = modal.Dict.from_name("h3-studio-jobs", create_if_missing=True)
hf_secret = modal.Secret.from_name("huggingface")   # YOUR OWN HF token (see README)

# ----------------------------------------------------------------------
# Container images
# ----------------------------------------------------------------------
h3_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "curl")
    .pip_install("torch==2.11.0", "torchvision==0.26.0", index_url="https://download.pytorch.org/whl/cu130",
                 extra_index_url="https://pypi.org/simple")
    .pip_install(DIFFUSERS_PIN, "transformers==5.8.0", "accelerate==1.14.0", "huggingface-hub==1.24.0",
                 "av", "pillow", "numpy", "requests", "safetensors>=0.8.0", "hf_transfer", "sentencepiece", "protobuf")
    .add_local_file(os.path.join(_VENDOR_DIR, "h3_split_blocks.py"), remote_path="/opt/h3/h3_split_blocks.py")
    .add_local_file(os.path.join(_VENDOR_DIR, "h3_lora.py"), remote_path="/opt/h3/h3_lora.py")
    .add_local_file(os.path.join(_VENDOR_DIR, "prompt_rewrite.py"), remote_path="/opt/h3/prompt_rewrite.py")
    .env({"PYTHONPATH": "/opt/h3", "HF_HOME": "/weights/hf", "HF_HUB_ENABLE_HF_TRANSFER": "1",
          "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
)

h3img_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "curl")
    .pip_install("torch==2.11.0", "torchvision==0.26.0",
                 index_url="https://download.pytorch.org/whl/cu130",
                 extra_index_url="https://pypi.org/simple")
    .pip_install(DIFFUSERS_MAIN, "transformers==5.8.0", "accelerate==1.14.0", "huggingface-hub>=1.32",
                 "pillow", "numpy", "safetensors>=0.8.0", "hf_transfer",
                 "sentencepiece", "protobuf")
    .env({"HF_HOME": "/weights/hf", "HF_HUB_ENABLE_HF_TRANSFER": "1",
          "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
)

api_image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]")


# ----------------------------------------------------------------------
# One-off weights downloads (run once: `modal run modal_backend.py`)
# Weights live on the persistent volume; every later start reads them
# from there instead of re-downloading ~260 GB.
# ----------------------------------------------------------------------
@app.function(image=h3_image, volumes={"/weights": wvol}, secrets=[hf_secret],
              timeout=6 * 3600, cpu=8, memory=16384)
def download_video_weights():
    """H3 video pipeline (~124 GB: transformer ~62 GB + Qwen3-VL encoder ~62 GB) + turbo LoRA files."""
    from huggingface_hub import snapshot_download, hf_hub_download
    import h3_lora
    snapshot_download(H3_REPO, allow_patterns=[
        "text_encoder/*", "tokenizer/*", "processor/*", "transformer/*", "vae/*", "audio_vae/*",
        "scheduler/*", "audio_scheduler/*", "*.json"])
    hf_hub_download(h3_lora.LARRY_REPO, h3_lora.LARRY_FILE)
    hf_hub_download(h3_lora.LIGHTX_REPO, h3_lora.LIGHTX_FILE)
    wvol.commit()
    return "ok"


@app.function(image=h3img_image, volumes={"/weights": wvol}, secrets=[hf_secret],
              timeout=6 * 3600, cpu=8, memory=16384)
def download_image_weights():
    """H3-Image full BF16 weights, diffusers layout only (~135.4 GB).

    The full HF repo is ~204 GB because it also carries a duplicate
    native-format copy — `allow_patterns` skips it.
    """
    from huggingface_hub import snapshot_download
    snapshot_download(H3IMG_REPO, allow_patterns=[
        "text_encoder/*", "tokenizer/*", "processor/*", "transformer_diffusers/*",
        "vae_diffusers/*", "scheduler/*", "*.json", "pdd_fused_heads.safetensors"])
    wvol.commit()
    return "ok"


@app.local_entrypoint()
def main():
    """`modal run modal_backend.py` — download BOTH models' weights once."""
    print("video weights:", download_video_weights.remote())
    print("image weights:", download_image_weights.remote())


# ----------------------------------------------------------------------
# MiniMax H3 video — split-pipeline layout:
#   Conditioner (Qwen3-VL ~62 GB, H100) -> prompt_embeds
#   Generator   (transformer ~62 GB + VAEs, H200) -> mp4 with audio
# Both classes carry the snapshot fix: two-phase enter + 20-min warm window.
# ----------------------------------------------------------------------
def snap_frames(seconds):
    frames = max(1, round(float(seconds) * FPS))
    while frames % FRAMES_PER_CHUNK != LATENTS_PER_CHUNK:
        frames += 1
    return frames


def _lower_floor():
    from diffusers.modular_pipelines.minimax_h3.modular_pipeline import MiniMaxH3ModularPipeline
    MiniMaxH3ModularPipeline.min_duration = property(lambda self: 2.0)


@app.cls(image=h3_image, gpu="H100", timeout=30 * 60, scaledown_window=1200,
         volumes={"/weights": wvol}, secrets=[hf_secret],
         enable_memory_snapshot=True, cpu=8, memory=131072)
class Conditioner:
    @modal.enter(snap=True)
    def load(self):
        # SNAPSHOT PHASE — CPU only. This boot has no GPU attached; any CUDA
        # call here crashes snapshot creation. Weights land in CPU RAM and
        # are captured by the memory snapshot.
        import torch
        from h3_split_blocks import MiniMaxH3ConditionerBlocks
        _lower_floor()
        self.pipe = MiniMaxH3ConditionerBlocks().init_pipeline(H3_REPO)
        self.pipe.load_components(dtype=torch.bfloat16)

    @modal.enter()
    def activate(self):
        # Runs on every real (GPU) container start, including snapshot restores.
        self.pipe.to("cuda")

    @modal.method()
    def encode(self, prompt, image_bytes, last_bytes, canvas, num_frames, rewrite=False):
        import io, torch
        from PIL import Image, ImageOps
        from safetensors.torch import save as st_save
        height, width = CANVASES[canvas]
        op = lambda b: ImageOps.exif_transpose(Image.open(io.BytesIO(b))).convert("RGB") if b else None
        image, last = op(image_bytes), op(last_bytes)
        refined = None
        if rewrite:
            try:
                import prompt_rewrite
                refined = prompt_rewrite.refine_keyframe_prompt(self.pipe, prompt, image, last, num_frames, 1000)
            except Exception as e:  # noqa
                print("rewrite failed", repr(e))
        with torch.no_grad():
            state = self.pipe(prompt=refined or prompt, image=image, last_image=last, height=int(height), width=int(width))
        embeds = state.get("prompt_embeds").cpu().contiguous()
        tags = state.get("text_token_tags").cpu().contiguous()
        meta = {"height": str(int(state.get("height"))), "width": str(int(state.get("width"))), "num_frames": str(int(num_frames))}
        return st_save({"prompt_embeds": embeds, "text_token_tags": tags}, meta), {
            "height": int(state.get("height")), "width": int(state.get("width")), "tokens": int(embeds.shape[1]), "refined": refined or ""}


@app.cls(image=h3_image, gpu="H200", timeout=45 * 60, scaledown_window=1200,
         volumes={"/weights": wvol, "/results": rvol}, secrets=[hf_secret],
         enable_memory_snapshot=True, cpu=16, memory=262144)
class Generator:
    @modal.enter(snap=True)
    def load(self):
        # SNAPSHOT PHASE — CPU only (no GPU on the snapshot-creation boot).
        # The turbo LoRA is merged into the transformer weights HERE, on CPU,
        # so snapshot restores come back with it already applied.
        import torch
        import h3_lora
        from diffusers import ComponentsManager
        from h3_split_blocks import MiniMaxH3GeneratorBlocks

        def _apply(entries, params, sign):
            for key, a, b in entries:
                p = params.get(key)
                if p is None:
                    raise KeyError(f"LoRA target `{key}` not found")
                d = sign * (b.to(p.device, torch.float32) @ a.to(p.device, torch.float32))
                p.data = (p.data.float() + d).to(p.dtype)
        h3_lora._apply = _apply

        _lower_floor()
        self.manager = ComponentsManager()
        pipe = MiniMaxH3GeneratorBlocks().init_pipeline(H3_REPO, components_manager=self.manager, collection="h3")
        pipe.load_components(dtype=torch.bfloat16)
        self.lora_status = h3_lora.apply_lora(pipe.transformer)
        self.pipe, self.h3_lora = pipe, h3_lora

    @modal.enter()
    def activate(self):
        # Runs on every real (GPU) container start, including snapshot restores.
        self.pipe.to("cuda")
        try:
            self.pipe.transformer.set_attention_backend("_native_cudnn")
        except Exception as e:  # noqa
            print("attention backend fallback:", repr(e))
        print("[gen] ready", self.lora_status)

    @modal.method()
    def generate(self, job_id, embeds_bytes, image_bytes, last_bytes, height, width, num_frames, steps, seed, lora):
        import io, os, time, torch
        from PIL import Image, ImageOps
        from safetensors.torch import load as st_load
        from diffusers.utils import encode_video
        t0 = time.time()
        op = lambda b: ImageOps.exif_transpose(Image.open(io.BytesIO(b))).convert("RGB") if b else None
        wire = st_load(embeds_bytes)
        active = self.h3_lora.set_active(self.pipe.transformer, lora)
        with torch.no_grad():
            state = self.pipe(
                prompt_embeds=wire["prompt_embeds"].to("cuda"), text_token_tags=wire["text_token_tags"],
                image=op(image_bytes), last_image=op(last_bytes), height=int(height), width=int(width),
                num_frames=int(num_frames), num_inference_steps=int(steps),
                generator=torch.Generator("cpu").manual_seed(int(seed)))
        frames, audio, sr = state.get("videos")[0], state.get("audio")[0].cpu(), state.get("sampling_rate")
        os.makedirs("/results", exist_ok=True)
        path = f"/results/{job_id}.mp4"
        encode_video(frames, fps=FPS, output_path=path, audio=audio, audio_sample_rate=sr)
        rvol.commit()
        return {"path": path, "seconds": round(time.time() - t0, 1), "lora": active}


def _fit_keyframe(img, current_canvas):
    """Center-crop a reference image to the closest canvas aspect ratio."""
    aspect = img.width / img.height
    fastest = {}
    for label, (h, w) in CANVASES.items():
        r = w / h
        if r not in fastest or w * h < fastest[r][1][0] * fastest[r][1][1]:
            fastest[r] = (label, (h, w))
    ratio = min(fastest, key=lambda r: abs(r - aspect))
    label, (h, w) = fastest[ratio]
    cur_h, cur_w = CANVASES[current_canvas]
    if abs(cur_w / cur_h - aspect) <= abs(ratio - aspect):
        label, h, w = current_canvas, cur_h, cur_w
    target = w / h
    if abs(img.width / img.height - target) > 1e-3:
        if img.width / img.height > target:
            nw = int(img.height * target); left = (img.width - nw) // 2
            img = img.crop((left, 0, left + nw, img.height))
        else:
            nh = int(img.width / target); top = (img.height - nh) // 2
            img = img.crop((0, top, img.width, top + nh))
    return img, label


def _last_frame_png(path):
    import av, io
    c = av.open(path)
    last = None
    for fr in c.decode(video=0):
        last = fr
    c.close()
    b = io.BytesIO(); last.to_image().save(b, "PNG")
    return b.getvalue()


def _stitch(paths, out):
    """Join segment mp4s (video + audio) into one file. Later segments drop their first frame (it is the previous last frame)."""
    import av, numpy as np
    from fractions import Fraction
    oc = av.open(out, "w")
    vo = None
    audio_parts = []
    n = 0
    pc = av.open(paths[0])
    sr = pc.streams.audio[0].rate if pc.streams.audio else 32000
    has_audio = bool(pc.streams.audio)
    pc.close()
    ao = oc.add_stream("aac", rate=sr, layout="stereo") if has_audio else None
    for i, p in enumerate(paths):
        ic = av.open(p)
        vs = ic.streams.video[0]
        if vo is None:
            vo = oc.add_stream("libx264", rate=FPS)
            vo.width, vo.height, vo.pix_fmt = vs.width, vs.height, "yuv420p"
            vo.options = {"crf": "22", "preset": "medium"}
        skip = 1 if i else 0
        for k, fr in enumerate(ic.decode(video=0)):
            if k < skip:
                continue
            nf = av.VideoFrame.from_ndarray(fr.to_ndarray(format="rgb24"), format="rgb24")
            nf.pts = n; nf.time_base = Fraction(1, FPS); n += 1
            for pk in vo.encode(nf):
                oc.mux(pk)
        ic.close()
        ic = av.open(p)
        if ic.streams.audio:
            a = ic.streams.audio[0]
            sr = a.rate
            rs = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
            chunks = []
            for af in ic.decode(audio=0):
                for r in (rs.resample(af) or []):
                    chunks.append(r.to_ndarray())
            arr = np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), np.float32)
            if i:
                arr = arr[:, int(sr / FPS):]
            f = min(int(sr * 0.02), arr.shape[1] // 2)
            if f > 0:
                ramp = np.linspace(0, 1, f, dtype=np.float32)
                arr[:, :f] *= ramp; arr[:, -f:] *= ramp[::-1]
            audio_parts.append(arr)
        ic.close()
    for pk in vo.encode():
        oc.mux(pk)
    if audio_parts:
        full = np.ascontiguousarray(np.concatenate(audio_parts, axis=1).astype(np.float32))
        pos = 0
        while pos < full.shape[1]:
            blk = full[:, pos:pos + 1024]
            frm = av.AudioFrame.from_ndarray(np.ascontiguousarray(blk), format="fltp", layout="stereo")
            frm.sample_rate = sr; frm.pts = pos; frm.time_base = Fraction(1, sr)
            for pk in ao.encode(frm):
                oc.mux(pk)
            pos += 1024
        for pk in ao.encode():
            oc.mux(pk)
    oc.close()


# ----------------------------------------------------------------------
# MiniMax H3-Image — text -> image up to 4096x4096, FULL official BF16
# weights (no quantization, no distilled substitute). The transformer
# (~66 GB) and the Qwen3-VL text encoder (~67 GB) can never be resident
# on one 80 GB GPU together, so the documented ComponentsManager auto
# CPU-offload route streams them between 256 GB of CPU RAM and the GPU.
# Sampling is fixed by the distilled model: 6 steps, guidance 1,
# no negative prompt.
# ----------------------------------------------------------------------
@app.cls(image=h3img_image, gpu="H100", timeout=30 * 60, scaledown_window=1200,
         volumes={"/weights": wvol, "/results": rvol}, secrets=[hf_secret],
         enable_memory_snapshot=True, cpu=16, memory=262144)
class H3Image:
    def _activate_gpu(self):
        # enable_auto_cpu_offload touches CUDA, so it can only run on a real
        # GPU container — snapshot-creation boots have no GPU attached.
        if getattr(self, "_gpu_active", False):
            return
        self.manager.enable_auto_cpu_offload(device="cuda")
        self._gpu_active = True
        print("[h3-image] gpu offload active", flush=True)

    @modal.enter(snap=True)
    def load(self):
        # SNAPSHOT PHASE — CPU only: build the pipeline and load components
        # into CPU RAM; the snapshot captures this loaded state.
        import datetime, torch
        from diffusers import ComponentsManager, ModularPipeline
        t0 = time.time()
        self.manager = ComponentsManager()
        self.pipe = ModularPipeline.from_pretrained(H3IMG_REPO, components_manager=self.manager)
        self.pipe.load_components(dtype=torch.bfloat16)
        if torch.cuda.is_available():
            self._activate_gpu()
        self.load_secs = round(time.time() - t0, 1)
        self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        print(f"[h3-image] loaded in {self.load_secs}s", flush=True)

    @modal.enter()
    def activate(self):
        # Runs on every real container start, including snapshot restores.
        import torch
        if torch.cuda.is_available():
            self._activate_gpu()

    @modal.method()
    def ping(self):
        return {"started_at": getattr(self, "started_at", None), "load_secs": getattr(self, "load_secs", -1),
                "gpu_active": getattr(self, "_gpu_active", False)}

    @modal.method()
    def generate(self, job_id, prompt, height=4096, width=4096, seed=42):
        import os, torch
        t0 = time.time()
        img = self.pipe(prompt=prompt, height=int(height), width=int(width),
                        generator=torch.Generator().manual_seed(int(seed)), output="images")[0]
        gen_secs = round(time.time() - t0, 1)
        os.makedirs("/results", exist_ok=True)
        path = f"/results/{job_id}.png"
        img.save(path)
        rvol.commit()
        return {"path": path, "seconds": gen_secs, "size": list(img.size),
                "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2),
                "load_secs": getattr(self, "load_secs", -1),
                "started_at": getattr(self, "started_at", None)}


# ----------------------------------------------------------------------
# Job wrappers — write status into the jobs Dict (queued -> running ->
# done/error, with a human-readable stage/report), then call the GPU class.
# ----------------------------------------------------------------------
@app.function(image=h3_image, timeout=35 * 60, volumes={"/results": rvol})
def image_job(job_id: str, p: dict):
    t0 = time.time()
    try:
        jobs[job_id] = {**jobs.get(job_id, {}), "status": "running", "stage": "h3-image"}
        _h = max(256, min(int(p.get("height") or 4096), 4096))
        _w = max(256, min(int(p.get("width") or 4096), 4096))
        out = H3Image().generate.remote(job_id, p["prompt"], _h, _w, int(p.get("seed") or 42))
        jobs[job_id] = {"status": "done", "path": out["path"], "seconds": round(time.time() - t0, 1),
                        "report": f"H3-Image {out['size'][0]}x{out['size'][1]} - gen {out['seconds']}s - peak {out['peak_gpu_gb']}GB"}
    except Exception as e:  # noqa
        jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:1500]}
        raise


@app.function(image=h3_image, timeout=3 * 3600, volumes={"/results": rvol})
def video_job(job_id: str, p: dict):
    """input: {prompt, image?, last_image?, canvas?, duration?, steps?, seed?, lora?, upsample?}

    `image` is an optional reference/first-frame image (data URL or bare
    base64) — e.g. a character reference. Prompts for multi-segment videos
    (>12 s) can be separated with a line containing only `---`.
    """
    import base64, io, math, time
    from PIL import Image, ImageOps
    t0 = time.time()
    try:
        jobs[job_id] = {**jobs.get(job_id, {}), "status": "running", "stage": "prepare"}
        _steps = max(1, min(int(p.get("steps") or 6), 20))
        canvas = p.get("canvas") if p.get("canvas") in CANVASES else DEFAULT_CANVAS

        def dec(key):
            s = p.get(key) or ""
            if not s:
                return None
            if "," in s:
                s = s.split(",", 1)[1]
            return ImageOps.exif_transpose(Image.open(io.BytesIO(base64.b64decode(s)))).convert("RGB")

        def enc(im):
            if im is None:
                return None
            b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

        first, last = dec("image"), dec("last_image")
        if first is not None:
            first, canvas = _fit_keyframe(first, canvas)
        if last is not None:
            last, canvas = _fit_keyframe(last, canvas)
        total = max(2.0, min(60.0, float(p.get("duration") or 5)))
        nseg = max(1, math.ceil(total / 12.0)) if total > 12 else 1
        if total > 12:
            nseg = max(2, round(total / SEG_SECONDS))
        seg_len = total / nseg
        raw = p.get("prompts")
        if not raw:
            raw = [x.strip() for x in (p["prompt"] or "").split("\n---") if x.strip()] or [p["prompt"]]
        prompts = [raw[min(i, len(raw) - 1)] for i in range(nseg)]
        fb, lb = enc(first), enc(last)
        frames = snap_frames(seg_len)
        seg_paths, report, refined = [], "", ""
        for i in range(nseg):
            tag = f"segment {i + 1}/{nseg} — " if nseg > 1 else ""
            jobs[job_id] = {**jobs.get(job_id, {}), "status": "running", "stage": tag + "conditioning"}
            embeds, plan = Conditioner().encode.remote(prompts[i], fb, lb if i == nseg - 1 else None, canvas, frames,
                                                       bool(p.get("upsample")))
            if i == 0:
                refined = plan.get("refined", "")
            jobs[job_id] = {**jobs.get(job_id, {}), "status": "running", "stage": tag + "generating video + audio"}
            out = Generator().generate.remote(f"{job_id}-s{i}", embeds, fb, lb if i == nseg - 1 else None,
                                              plan["height"], plan["width"], frames,
                                              _steps, int(p.get("seed") or 42) + i, p.get("lora") or "larry")
            seg_paths.append(out["path"])
            report = f"{plan['width']}x{plan['height']} · {nseg} segment(s) × {frames} frames · {_steps} steps"
            if i < nseg - 1:
                rvol.reload()
                fb = _last_frame_png(out["path"])
        rvol.reload()
        if nseg == 1:
            final = seg_paths[0]
        else:
            jobs[job_id] = {**jobs.get(job_id, {}), "status": "running", "stage": "stitching segments"}
            final = f"/results/{job_id}.mp4"
            _stitch(seg_paths, final)
            rvol.commit()
        jobs[job_id] = {"status": "done", "path": final, "seconds": round(time.time() - t0, 1),
                        "report": report, "refined": refined if nseg == 1 else ""}
    except Exception as e:  # noqa
        jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:1500]}
        raise


# ----------------------------------------------------------------------
# HTTP API (FastAPI on Modal).
# Optional token auth: if you created the `h3-studio-api` Modal secret
# with an API_TOKEN value, every request (except GET /health) must carry
# it in the `x-h3-token` header. The token is YOUR choice — this repo
# ships with no credentials of any kind.
# ----------------------------------------------------------------------
@app.function(image=api_image, volumes={"/results": rvol}, timeout=305,
              secrets=[modal.Secret.from_name("h3-studio-api")])
@modal.asgi_app()
def api():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    web = FastAPI(title="H3 Free Studio")
    _token = os.environ.get("API_TOKEN", "")

    @web.middleware("http")
    async def auth(request: Request, call_next):
        if request.method == "OPTIONS" or (request.method == "GET" and request.url.path == "/health"):
            return await call_next(request)
        if _token and request.headers.get("x-h3-token", "") != _token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @web.get("/health")
    def health():
        return {"ok": True, "app": APP_NAME, "models": [H3_REPO, H3IMG_REPO], "types": ["image", "video"]}

    @web.post("/jobs")
    def create(body: dict):
        jtype = body.get("type")
        inp = body.get("input") or {}
        if jtype not in ("image", "video"):
            raise HTTPException(400, "type must be 'image' or 'video'")
        if not (inp.get("prompt") or "").strip():
            raise HTTPException(400, "prompt is required")
        if len(inp.get("prompt") or "") > 20000:
            raise HTTPException(400, "prompt too long")
        if jtype == "image":
            job_id = "i-" + uuid.uuid4().hex
            jobs[job_id] = {"status": "queued"}
            call = image_job.spawn(job_id, inp)
        else:
            job_id = "v-" + uuid.uuid4().hex
            jobs[job_id] = {"status": "queued"}
            call = video_job.spawn(job_id, inp)
        jobs[job_id] = {"status": "queued", "call": call.object_id}
        return {"job_id": job_id}

    @web.get("/jobs/{job_id}")
    def status(job_id: str):
        j = jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "unknown job")
        if j.get("status") == "done":
            rvol.reload()
            img = job_id.startswith("i-")
            size = os.path.getsize(j["path"])
            fi = {"name": "image.png" if img else "video.mp4", "mime": "image/png" if img else "video/mp4", "size": size}
            if size > 5_000_000:
                fi["chunks"] = (size + CHUNK - 1) // CHUNK
            else:
                with open(j["path"], "rb") as f:
                    fi["b64"] = base64.b64encode(f.read()).decode()
            return {"status": "done", "result": {"files": [fi],
                    "stats": {"seconds": j.get("seconds", 0), "report": j.get("report", ""), "refined": j.get("refined", "")}}}
        if j.get("status") == "error":
            return {"status": "error", "error": j.get("error", "failed")}
        return {"status": j.get("status", "queued"), "stage": j.get("stage", "")}

    @web.get("/jobs/{job_id}/chunk/{i}")
    def chunk(job_id: str, i: int):
        j = jobs.get(job_id)
        if not j or j.get("status") != "done":
            raise HTTPException(404, "not ready")
        rvol.reload()
        with open(j["path"], "rb") as f:
            f.seek(i * CHUNK)
            return {"b64": base64.b64encode(f.read(CHUNK)).decode()}

    @web.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        j = jobs.get(job_id) or {}
        try:
            if j.get("call"):
                modal.FunctionCall.from_id(j["call"]).cancel()
        except Exception:
            pass
        jobs[job_id] = {"status": "error", "error": "cancelled"}
        return {"ok": True}

    return web
