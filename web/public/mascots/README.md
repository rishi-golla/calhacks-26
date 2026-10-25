# Finance dashboard mascots

The PNG assets were made with Codex's native `image_gen.imagegen` tool, with
`transparent_background=true`. The native tool does not disclose its underlying
model identifier; the manifest records `native-image_gen` rather than guessing.
Idle images use a shared clay-render prompt and the intake image as the style
and body-shape reference for the other nine characters. Each working and alert image is an
image edit using that character's own idle PNG as its source. Working poses add
a white laptop and sparkles; alert poses raise both arms and add a blue sweat
drop. Wearable identity accessories stay in place; held props may move beside
the character to make the pose possible.

Native outputs may be larger than requested. Final files are normalized to
1024 × 1024 using macOS AppKit through Swift, fitting the full image within a
transparent square without stretching or cropping. See `manifest.json` for
the files currently present and their browser paths. Each 1024-square original
also has a 320-square `<state>@320.png` companion, made with high-quality
resampling and preserved alpha. Use the manifest's `<state>_small` paths for
small dashboard placements.

## Regenerate

`generate.py` contains the shared prompt, all ten character specifications, and
both edit prompts. Print prompts without calling a service:

```sh
python3 web/public/mascots/generate.py
python3 web/public/mascots/generate.py --character risk --variant alert
```

To use native generation again, use the printed idle prompt with
`image_gen.imagegen(transparent_background=true)`, save the output as
`intake/idle.png`, inspect it, then use it as the style-reference input for the
other idle prompts. Use each character's own idle PNG as the input image for
its variants' printed edit prompts. Copy native output files into this directory.

For an API-based regeneration, install the Codex `imagegen` skill and its
`openai` dependency, and set `OPENAI_API_KEY` locally. The wrapper invokes the
skill's supported CLI, using `gpt-image-1.5`, transparent PNG output, high
quality, and high input fidelity for edits:

```sh
python3 web/public/mascots/generate.py --run-api
python3 web/public/mascots/generate.py --run-api --character risk --variant alert --force
```

Existing images are preserved unless `--force` is passed. Variants always edit
their character's idle PNG, never another variant. `--cli /path/to/image_gen.py`
overrides the installed skill location. API reruns incur image-generation charges
and may differ from the native originals.

Rebuild the manifest from files on disk and optionally normalize native output:

```sh
python3 web/public/mascots/generate.py --manifest-only --normalize --model native-image_gen
python3 web/public/mascots/generate.py --small --model native-image_gen
python3 web/public/mascots/generate.py --validate
```

The manifest contains only files that exist. Its `transparent` field reflects
PNG alpha-channel presence; final visual inspection additionally checks the
cutouts. `--validate` requires all 60 files and verifies the 1024/320-square dimensions,
an alpha channel, and both fully transparent and visible pixels in every image.
This directory contains no SVG substitutes or generated UI code.

## Visual notes

The set shares the same bean silhouette, material, black eyes, and mouthless,
legless design. Held props may be moved aside or omitted for laptop and raised-arm
poses. Native cutouts can retain small light or colored edge fringes, particularly
around white props and sparkles; review them against the dashboard's final
background if they are displayed at large sizes.
