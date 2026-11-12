# H3 Free Studio — استوديو H3 المجاني

استوديو مفتوح المصدر لتوليد **الصور والفيديوهات** بموديلات **MiniMax H3** المفتوحة الأوزان،
بيشتغل على حساب **Modal** بتاعك — وبيستخدم كريديت الـ **30 دولار المجاني شهريًا** اللي بتديه
خطة Modal Starter — بدل ما تدفع اشتراكات شهرية لمواقع توليد مدفوعة أو تدفع للصورة/المقطع الواحد.

- **إنشاء صورة** — MiniMax H3-Image: صور لحد **4096×4096 (4K)** بالأوزان الرسمية الكاملة
  بدقة BF16 (من غير تقليل أو نسخ مخفّضة)، 6 خطوات توليد، وتوجيه (guidance) = 1.
- **إنشاء فيديو** — MiniMax H3: فيديو من وصف نصي، مع صورة مرجعية اختيارية للشخصية/الإطار الأول،
  وصوت مولّد مع الفيديو، لحد 60 ثانية (بيتولد على أجزاء وبيتجمع تلقائيًا).

هذا المشروع أداة مجتمعية مستقلة ومفتوحة المصدر: أي حد يقدر يشغّل التجهيزة دي
على حسابه هو، من غير ما يعتمد على أي خدمة جاهزة.

> **الأوزان ليست في هذا المستودع.** الكود بس هو اللي هنا (رخصة MIT). أوزان الموديلات ملك
> MiniMax ومرخّصة برخصتها المجتمعية، وبتنزل من Hugging Face على حسابك أول مرة تشغّل.

---

## ليه؟

مواقع التوليد المدفوعة بتبيعك ثواني كارت شاشة بأسعار مضروبة في عشرة. الموديلات نفسها
بقت مفتوحة الأوزان: لو عندك طريقة تشغّل كارت شاشة سحابي بالثانية — وده بالظبط اللي بتعمله
Modal — تقدر تولّد بنفسك وتدفع تكلفة الكارت الفعلية بس، ولو استخدامك في حدود الكريديت
المجاني الشهري ($30) فالتكلفة صفر.

## المتطلبات

1. **حساب Modal** — [modal.com](https://modal.com) (خطة Starter فيها $30 كريديت مجاني كل شهر).
2. **حساب Hugging Face** — [huggingface.co](https://huggingface.co) عشان تقبل رخصة الموديلات
   وتنزّل الأوزان (هتحتاج توكن قراءة).
3. بايثون 3.10+ على جهازك.

## خطوات الإعداد (من الصفر)

```bash
# 1) نزّل الكود وثبّت الأدوات (استبدل الرابط برابط المستودع اللي نزّلت منه)
git clone <url-of-this-repository>
cd h3-free-studio
pip install -r requirements.txt

# 2) سجّل دخول Modal من جهازك
modal setup

# 3) حط توكن Hugging Face بتاعك كسرّ عند Modal (بتاعك انت — المستودع مفيهوش أي أسرار)
modal secret create huggingface HF_TOKEN=<your-hf-token-here>

# 4) اختر توكن حماية لأي بي آي بتاعك (أي قيمة عشوائية تختارها)
modal secret create h3-studio-api API_TOKEN=ضع-هنا-توكن-من-اختيارك

# 5) نزّل الأوزان مرة واحدة على قرص Modal الدائم (Volume) — حوالي 260 جيجا، بياخد وقت أول مرة
modal run modal_backend.py

# 6) انشر الباك إند — هيديك رابط الويب بتاع الخدمة (Web endpoint)
modal deploy modal_backend.py

# 7) شغّل الواجهة واربطها بالرابط اللي طلع من الخطوة 6
export H3_STUDIO_API_URL="https://<رابط-الخدمة-اللي-طلع-لك>"
export H3_STUDIO_API_TOKEN="نفس التوكن اللي اخترته في الخطوة 4"
python app.py
```

افتح الواجهة من المتصفح: تبويب **«إنشاء صورة»** وتبويب **«إنشاء فيديو»**.

### تقدر تستخدم الباك إند من غير الواجهة كمان (API)

```bash
curl -X POST "$H3_STUDIO_API_URL/jobs" -H "x-h3-token: $H3_STUDIO_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "image", "input": {"prompt": "a cinematic X-ray cutaway of a V8 engine, navy blueprint background", "width": 4096, "height": 4096, "seed": 42}}'
# -> {"job_id": "i-..."}
curl "$H3_STUDIO_API_URL/jobs/<job_id>" -H "x-h3-token: $H3_STUDIO_API_TOKEN"
# status: queued -> running -> done (النتيجة base64 داخل الرد، أو chunks للملفات الكبيرة)
```

مدخلات الصورة: `prompt`, `width`, `height` (256–4096), `seed`.
مدخلات الفيديو: `prompt`, `image` (صورة مرجعية — data URL أو base64), `last_image` (اختياري),
`canvas` (من قائمة الأبعاد في الكود), `duration` (2–60 ثانية), `steps`, `seed`.

## أهم فكرة في المشروع: السناب شوت وعلاج «الصحيان البطيء»

الموديلات دي تقيلة: أوزان الصور ~135 جيجا والفيديو ~124 جيجا. لو كل تشغيل بيحمّل
الأوزان من القرص من الأول، هتدفع دقائق كارت قبل ما تولّد أي حاجة — وهتحرق الكريديت
في التحميل مش في التوليد.

الحل اللي مطبّق هنا على الموديلين:

- **الأوزان بتنزل مرة واحدة** على Modal Volume دائم، وكل تشغيل بيقرأ منها.
- **`enable_memory_snapshot=True`**: Modal بيصوّر ذاكرة الحاوية والمودل محمّل وجاهز،
  والتشغيل الجديد بيستعيد الحالة الجاهزة بدل إعادة التحميل.
  - مثال مقاس على كارت H100 (H3-Image): بدء بارد كامل **224.9 ثانية** ←
    استعادة من السناب شوت **4.3 ثانية** (حوالي 52 مرة أسرع).
  - خد بالك من الفخ: بوت إنشاء السناب شوت **مفيهوش كارت شاشة** — أي أمر CUDA جوّه
    بيوقّع الإنشاء. عشان كده التحميل متقسّم مرحلتين: مرحلة السناب (`snap=True`)
    بتحمّل على المعالج بس، وأي إعداد يلمس الكارت بيحصل في `@modal.enter()` عادية
    بتشتغل مع كل بداية حقيقية.
- **`scaledown_window=1200`**: الخدمة بتفضل صاحية 20 دقيقة بعد آخر طلب — جلسة شغل
  واحدة متدفعش تمن صحيان بين كل صورة والتانية — وبعدها بتنام لوحدها وترجع التكلفة صفر.

## التكلفة بصراحة

- Modal بيحاسب **بالثانية**، والخدمة بتنام لوحدها (Scale to Zero) — وقت الخمول = صفر.
- أسعار الكروت المستخدمة هنا (منشورة من Modal): H100 حوالي **$3.95/ساعة**، وH200 أعلى منه —
  راجع [modal.com/pricing](https://modal.com/pricing) للأسعار الحالية، ولاحظ إن الذاكرة
  والمعالج بيتحاسبوا فوق تكلفة الكارت.
- زمن توليد صورة 4K وهي الخدمة دافية (مثال مقاس على H100): من ~19 ثانية في أفضل حالة
  لحوالي 2–3 دقائق بطريق نقل المكونات على كارت 80 جيجا. توليد الفيديو بياخد دقائق حسب الطول والدقة.
- التكلفة الفعلية بتختلف حسب نمط استخدامك — راجع صفحة الفوترة في Modal بعد أول جلسة
  عشان تعرف تكلفة جلساتك انت.

## حدود صادقة — اقرأ قبل ما تتحمس

- **الرخصة والجغرافيا:** أوزان MiniMax مرخّصة برخصة MiniMax المجتمعية، وهي **لا تغطي
  الاتحاد الأوروبي والمملكة المتحدة والولايات المتحدة وكوريا الجنوبية**. وكروت Modal
  موجودة في مناطق أمريكية/أوروبية. فالإطار هنا **شخصي وتجريبي** — أي استخدام تجاري
  محتاج مراجعة قانونية حقيقية للرخصة الأول.
- **الكريديت شهري ولا يتراكم** — اللي مش هتستخدمه بيضيع آخر الشهر.
- **الحجم:** التنزيلة الأولى ~260 جيجا (صور ~135.4 + فيديو ~124) على الـVolume.
- **إمتى الـAPI المدفوع يكسب؟** لو احتياجك صورة واحدة كل كام يوم، API رسمي جاهز
  ممكن يكون أرخص وأسهل من صحيان خدمة كاملة. التشغيل الذاتي بيكسب مع **الدفعات**:
  جلسة واحدة صاحية تطلع شغل كتير بتمن تحميل واحد.
- خط أنابيب الفيديو مقسوم على كلاسين (Conditioner/Generator) باستخدام أكواد تقسيم
  مجتمعية مضمّنة في مجلد `vendor/` جوه المستودع نفسه — بناء الحاوية بياخدها من
  المستودع مباشرة ومش بيحتاج ينزّل كود من أي مصدر خارجي — لأن خط الأنابيب الموحّد
  مبيكفيش على كارت واحد.

## بنية المشروع

| الملف | دوره |
|---|---|
| `modal_backend.py` | الباك إند كله: تنزيل الأوزان، كلاس الصور H3Image (H100)، كلاسا الفيديو Conditioner (H100) وGenerator (H200)، ونقطة الـAPI |
| `vendor/` | أكواد خط الأنابيب المجتمعية المضمّنة في المستودع (تقسيم المودل، محمّل الـLoRA، إعادة صياغة البرومبت) |
| `app.py` | واجهة Gradio عربية بتبويبين: إنشاء صورة / إنشاء فيديو |
| `requirements.txt` | أدوات جهازك: modal + gradio + requests |

الكود (مش الأوزان) مرخّص MIT — شوف `LICENSE`.

---

# English

**H3 Free Studio** is an open-source studio for generating **images** (MiniMax
**H3-Image**, up to 4096×4096 with the full official BF16 weights) and **videos**
(MiniMax **H3**, text-to-video with an optional character/first-frame reference
image and generated audio, up to 60 s) on **your own Modal account**, powered by
Modal Starter's **$30/month free credit** instead of paid per-generation services.

**Weights are not included.** Only code lives here (MIT). The model weights belong
to MiniMax, are covered by the MiniMax Community License, and are downloaded from
Hugging Face to your own Modal Volume on first run (~260 GB total: ~135.4 GB
image + ~124 GB video).

### Setup

```bash
git clone <url-of-this-repository>
cd h3-free-studio
pip install -r requirements.txt
modal setup
modal secret create huggingface HF_TOKEN=<your-hf-token>          # your own token
modal secret create h3-studio-api API_TOKEN=<a-token-you-choose>  # your own API token
modal run modal_backend.py        # one-off weights download to the Volume
modal deploy modal_backend.py     # prints your web endpoint URL
export H3_STUDIO_API_URL="https://<your-endpoint>"
export H3_STUDIO_API_TOKEN="<the token you chose>"
python app.py
```

API: `POST /jobs {"type": "image"|"video", "input": {...}}` → `GET /jobs/{id}`
(results inline as base64 up to 5 MB, otherwise via `GET /jobs/{id}/chunk/{i}`),
plus `GET /health`. Send your token as the `x-h3-token` header.

### The core trick — memory snapshots

Both model classes use `enable_memory_snapshot=True` with a **two-phase enter**:
the `@modal.enter(snap=True)` phase loads the model into CPU RAM only (the
snapshot-creation boot has **no GPU** — any CUDA call there crashes it), while
CUDA-touching setup runs in a plain `@modal.enter()` on every real start,
including snapshot restores. With `scaledown_window=1200` (20-minute warm
window), a working session never pays a reload between generations. Measured
example on an H100 (H3-Image): full cold start **224.9 s** → snapshot restore
**4.3 s**. Billing is per-second with scale-to-zero; idle costs nothing. The
community pipeline-split code ships inside `vendor/` in this repo — the
container build copies it from the repo and downloads no code from external
spaces.

### Honest limits

- The **MiniMax Community License does not cover the EU, UK, US, or South
  Korea**, and Modal GPUs sit in US/EU regions — treat this as a
  **personal/experimental** setup; get real legal review before any commercial use.
- The $30 credit is monthly and does not roll over.
- A warm 4K image takes ~19 s in the best observed case and ~2–3 min via CPU
  offload on an 80 GB card; video takes minutes depending on length/resolution.
- If you only need one image every few days, an official hosted API may be
  cheaper than waking a full service — self-hosting wins with **batches**.

Code license: MIT (see `LICENSE`). Model weights: MiniMax Community License.
