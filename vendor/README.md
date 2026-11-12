# vendor/ — community pipeline code, included in-repo

The MiniMax H3 video pipeline does not fit on a single GPU in its stock
form, so this project uses community-written pipeline code that splits it
into a conditioner half and a generator half:

| File | Role |
|---|---|
| `h3_split_blocks.py` | The two pipeline halves (`MiniMaxH3ConditionerBlocks`, `MiniMaxH3GeneratorBlocks`) |
| `h3_lora.py` | Turbo-LoRA loader/applier used by the generator half |
| `prompt_rewrite.py` | Optional keyframe-prompt refinement used by the conditioner half |

These files are community pipeline code, vendored into this repository so
the container build copies them from here and needs no external code
downloads at build time. They are included as-is (apart from this note);
model weights are never vendored — they download from Hugging Face to
your own Modal Volume on first run (see the top-level README).
