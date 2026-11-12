"""
H3 Free Studio — واجهة الويب (Gradio)
=====================================
واجهة بسيطة للباك إند الموجود في modal_backend.py: تبويب لإنشاء الصور
(MiniMax H3-Image) وتبويب لإنشاء الفيديو (MiniMax H3).

التشغيل:
    pip install -r requirements.txt
    # أولًا انشر الباك إند على حساب Modal بتاعك (شوف README.md)، وبعدين:
    export H3_STUDIO_API_URL="https://<your-modal-web-endpoint>"
    export H3_STUDIO_API_TOKEN="<the API_TOKEN you chose>"   # اختياري — لو فعّلت التوكن في الباك إند
    python app.py
"""
import base64
import os
import tempfile
import time

import requests
import gradio as gr

API_URL = os.environ.get("H3_STUDIO_API_URL", "").rstrip("/")
API_TOKEN = os.environ.get("H3_STUDIO_API_TOKEN", "")
HEADERS = {"x-h3-token": API_TOKEN} if API_TOKEN else {}

# الحجم: (العرض, الارتفاع)
IMAGE_SIZES = {
    "4096 × 4096 — 4K كاملة": (4096, 4096),
    "2048 × 2048": (2048, 2048),
    "1024 × 1024": (1024, 1024),
    "2560 × 1440 — عريض": (2560, 1440),
    "1440 × 2560 — طولي": (1440, 2560),
}
CANVASES = [
    "960x544 · 16:9 fast",
    "1280x704 · 16:9",
    "1344x768 · 16:9 full",
    "768x1344 · 9:16 full",
    "1024x1024 · 1:1 max",
    "1024x768 · 4:3 full",
]


def _create_job(jtype, payload):
    r = requests.post(f"{API_URL}/jobs", json={"type": jtype, "input": payload},
                      headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()["job_id"]


def _poll_job(job_id, timeout_s):
    """Yield status dicts until done/error/timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(f"{API_URL}/jobs/{job_id}", headers=HEADERS, timeout=60)
        r.raise_for_status()
        j = r.json()
        yield j
        if j.get("status") in ("done", "error"):
            return
        time.sleep(8)
    yield {"status": "error", "error": "timeout — انتهت مهلة الانتظار"}


def _download_result(job_id, status_json, suffix):
    f = status_json["result"]["files"][0]
    if "b64" in f:
        data = base64.b64decode(f["b64"])
    else:
        parts = []
        for i in range(f["chunks"]):
            r = requests.get(f"{API_URL}/jobs/{job_id}/chunk/{i}", headers=HEADERS, timeout=180)
            r.raise_for_status()
            parts.append(r.json()["b64"])
        data = base64.b64decode("".join(parts))
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def _file_to_dataurl(path):
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()


def run_image(prompt, size_label, seed):
    if not API_URL:
        yield "⚠️ متغيّر H3_STUDIO_API_URL مش مضبوط — انشر الباك إند الأول (شوف README.md).", None
        return
    if not (prompt or "").strip():
        yield "⚠️ اكتب وصف الصورة الأول.", None
        return
    w, h = IMAGE_SIZES[size_label]
    try:
        job_id = _create_job("image", {"prompt": prompt, "width": w, "height": h, "seed": int(seed)})
    except Exception as e:  # noqa
        yield f"❌ فشل إنشاء المهمة: {e}", None
        return
    yield f"⏳ المهمة {job_id} بدأت — أول تشغيل بعد نشر الباك إند بياخد دقائق (تحميل المودل)، والتشغيلات اللي بعدها أسرع بفضل السناب شوت.", None
    try:
        for j in _poll_job(job_id, 30 * 60):
            if j.get("status") == "done":
                path = _download_result(job_id, j, ".png")
                stats = j.get("result", {}).get("stats", {})
                yield f"✅ خلصت في {stats.get('seconds', '?')} ثانية — {stats.get('report', '')}", path
                return
            if j.get("status") == "error":
                yield f"❌ {j.get('error', 'خطأ غير معروف')}", None
                return
            yield f"⏳ {j.get('status', '')} — {j.get('stage', '')}", None
    except Exception as e:  # noqa
        yield f"❌ {e}", None


def run_video(prompt, ref_image, canvas, duration, steps, seed):
    if not API_URL:
        yield "⚠️ متغيّر H3_STUDIO_API_URL مش مضبوط — انشر الباك إند الأول (شوف README.md).", None
        return
    if not (prompt or "").strip():
        yield "⚠️ اكتب وصف الفيديو الأول.", None
        return
    payload = {"prompt": prompt, "canvas": canvas, "duration": float(duration),
               "steps": int(steps), "seed": int(seed)}
    if ref_image:
        payload["image"] = _file_to_dataurl(ref_image)
    try:
        job_id = _create_job("video", payload)
    except Exception as e:  # noqa
        yield f"❌ فشل إنشاء المهمة: {e}", None
        return
    yield f"⏳ المهمة {job_id} بدأت — توليد الفيديو بياخد دقائق حسب الطول والدقة.", None
    try:
        for j in _poll_job(job_id, 60 * 60):
            if j.get("status") == "done":
                path = _download_result(job_id, j, ".mp4")
                stats = j.get("result", {}).get("stats", {})
                yield f"✅ خلص في {stats.get('seconds', '?')} ثانية — {stats.get('report', '')}", path
                return
            if j.get("status") == "error":
                yield f"❌ {j.get('error', 'خطأ غير معروف')}", None
                return
            yield f"⏳ {j.get('status', '')} — {j.get('stage', '')}", None
    except Exception as e:  # noqa
        yield f"❌ {e}", None


CSS = ".gradio-container {direction: rtl} footer {display: none !important}"

with gr.Blocks(title="H3 Free Studio — استوديو H3 المجاني", css=CSS) as demo:
    gr.Markdown(
        "# 🎬 H3 Free Studio — استوديو H3 المجاني\n"
        "ولّد **صور 4K** بمودل **MiniMax H3-Image** و**فيديوهات** بمودل **MiniMax H3** — "
        "على حساب Modal بتاعك وبكريديت الـ30 دولار المجاني شهريًا، من غير اشتراكات لمواقع توليد مدفوعة.\n\n"
        "⚠️ أول مهمة بعد النشر بطيئة (تحميل المودل من القرص)، وبعدها السناب شوت بيخلي البداية ثواني. "
        "الخدمة بتنام لوحدها بعد 20 دقيقة خمول فمش بتدفع غير وقت الشغل الفعلي."
    )
    with gr.Tabs():
        with gr.Tab("🖼️ إنشاء صورة"):
            img_prompt = gr.Textbox(label="وصف الصورة", lines=4,
                                    placeholder="اكتب وصفًا تفصيليًا للصورة… الأوصاف الطويلة المفصّلة بتدي أفضل نتيجة")
            with gr.Row():
                img_size = gr.Dropdown(label="الحجم", choices=list(IMAGE_SIZES.keys()),
                                       value="4096 × 4096 — 4K كاملة")
                img_seed = gr.Number(label="البذرة (Seed)", value=42, precision=0)
            img_btn = gr.Button("ولّد الصورة", variant="primary")
            img_status = gr.Textbox(label="الحالة", interactive=False)
            img_out = gr.Image(label="النتيجة", type="filepath")
            img_btn.click(run_image, inputs=[img_prompt, img_size, img_seed],
                          outputs=[img_status, img_out])
        with gr.Tab("🎞️ إنشاء فيديو"):
            vid_prompt = gr.Textbox(label="وصف الفيديو", lines=4,
                                    placeholder="اوصف المشهد والحركة والكاميرا… لفيديو أطول من 12 ثانية ممكن تفصل أوصاف الأجزاء بسطر فيه --- لوحده")
            vid_ref = gr.Image(label="صورة مرجعية للشخصية / إطار أول (اختياري)", type="filepath")
            with gr.Row():
                vid_canvas = gr.Dropdown(label="أبعاد الفيديو", choices=CANVASES, value="960x544 · 16:9 fast")
                vid_duration = gr.Slider(label="المدة بالثواني", minimum=2, maximum=60, step=1, value=5)
            with gr.Row():
                vid_steps = gr.Slider(label="خطوات التوليد", minimum=1, maximum=20, step=1, value=6)
                vid_seed = gr.Number(label="البذرة (Seed)", value=42, precision=0)
            vid_btn = gr.Button("ولّد الفيديو", variant="primary")
            vid_status = gr.Textbox(label="الحالة", interactive=False)
            vid_out = gr.Video(label="النتيجة")
            vid_btn.click(run_video, inputs=[vid_prompt, vid_ref, vid_canvas, vid_duration, vid_steps, vid_seed],
                          outputs=[vid_status, vid_out])
    gr.Markdown(
        "---\n"
        "الأوزان ملك MiniMax ومرخّصة برخصة MiniMax المجتمعية (ليست ضمن هذا المستودع). "
        "الاستخدام هنا شخصي/تجريبي — راجع حدود الرخصة الجغرافية في README قبل أي استخدام تجاري."
    )

if __name__ == "__main__":
    demo.queue().launch()
