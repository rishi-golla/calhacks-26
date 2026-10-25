#!/usr/bin/env python3
"""Reproduce mascot prompts with Codex imagegen's supported API CLI.

Original assets were generated/edited using the native image_gen tool.
This optional regeneration path requires OPENAI_API_KEY and the imagegen skill.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
STYLE = (
    "Use case: stylized-concept. Asset: a finance operations dashboard mascot, "
    "a premium coherent 3D clay / soft plastic render. Square 1024x1024 PNG "
    "with actual transparent alpha background. Exactly one rounded bean/teardrop "
    "body, no neck, two small stubby nub arms, no legs or feet, NO MOUTH, only "
    "two simple vertical black oval eyes. Matte smooth slightly subsurface "
    "material, soft studio light from upper left, faint soft contact shadow "
    "beneath. Centered front-facing full body, generous clean margins, "
    "straight-on camera. Cute minimal Pixar / Duolingo-like physical toy quality. "
    "No words, letters, logos, extra characters, floor or scenery. Props must "
    "be rendered in the same soft clay style. Keep mascot body silhouette and "
    "eyes consistently minimal."
)
CHARACTERS = {
    "intake": ("#1FB37A", "One nub arm raised waving. A small white speech bubble with exactly three gray dots floats beside its head. Two tiny motion marks near the waving hand."),
    "carrier_ap": ("#2F6BFF", "wearing a blue-and-white trucker cap. A tiny white box-truck sits beside the character, with gray speed lines behind it."),
    "documents": ("#F0405A", "holding up a white document sheet with gray text lines and a green checkmark on it."),
    "risk": ("#F0338F", "holding a large gold-rimmed magnifying glass up to one eye. An orange warning triangle with the punctuation symbol ! floats above its head. This symbol is the only permitted text."),
    "verification": ("#8B5CF6", "hugging a white shield with a green checkmark."),
    "controller": ("#5A6B85", "pressing a wooden rubber stamp with a red handle onto an invisible ground plane, a red checkmark stamp mark below it. No actual floor surface."),
    "voice_desk": ("#FF7A1A", "wearing a dark headset with microphone boom, orange sound-wave arcs beside its head."),
    "cfo": ("#FFC627", "wearing a small black top hat, hugging a white bar chart with a rising orange arrow."),
    "validator": ("#17B3A6", "hugging a clipboard with three green checkmarks."),
    "shipper_ar": ("#A86B3C", "hugging a tan manila folder stuffed with white invoice papers."),
}
VARIATIONS = {
    "working": "Eyes slightly narrowed in concentration. Place a small white laptop open in front of the character, with tiny floating sparkles. Adapt the arm pose naturally to the laptop. Keep identifying wearable accessories and place held props beside the character if necessary.",
    "alert": "Eyes wide, a blue sweat drop beside its head, both nub arms raised. Keep identifying wearable accessories; place held props beside the character so both arms can be raised.",
}
NORMALIZE_SWIFT = r'''
import AppKit
let dimension = Int(CommandLine.arguments[1])!
let suffix = CommandLine.arguments[2]
for name in CommandLine.arguments.dropFirst(3) {
    let url = URL(fileURLWithPath: name)
    let output = suffix.isEmpty ? url : url.deletingLastPathComponent()
        .appendingPathComponent(url.deletingPathExtension().lastPathComponent + suffix + ".png")
    guard let input = NSImage(contentsOf: url),
          let rep = NSBitmapImageRep(data: try Data(contentsOf: url)),
          let canvas = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: dimension,
              pixelsHigh: dimension, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
              isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: canvas) else { fatalError(name) }
    let side = Double(dimension)
    let scale = min(side / Double(rep.pixelsWide), side / Double(rep.pixelsHigh))
    let width = Double(rep.pixelsWide) * scale
    let height = Double(rep.pixelsHigh) * scale
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.imageInterpolation = .high
    input.draw(in: NSRect(x: (side - width) / 2, y: (side - height) / 2,
                         width: width, height: height),
               from: .zero, operation: .copy, fraction: 1.0)
    NSGraphicsContext.restoreGraphicsState()
    guard let data = canvas.representation(using: .png, properties: [:]) else { fatalError(name) }
    try data.write(to: output, options: .atomic)
}
'''
VALIDATE_SWIFT = r'''
import AppKit
for name in CommandLine.arguments.dropFirst() {
    let dimension = name.hasSuffix("@320.png") ? 320 : 1024
    guard let rep = NSBitmapImageRep(data: try Data(contentsOf: URL(fileURLWithPath: name))),
          let bytes = rep.bitmapData, rep.hasAlpha, rep.bitsPerSample == 8,
          rep.samplesPerPixel == 4, rep.pixelsWide == dimension, rep.pixelsHigh == dimension
          else { fatalError("Invalid dimensions or alpha format: " + name) }
    let offset = rep.bitmapFormat.contains(.alphaFirst) ? 0 : 3
    var transparent = 0
    var opaque = 0
    var visible = 0
    for y in 0..<rep.pixelsHigh {
        for x in 0..<rep.pixelsWide {
            let alpha = bytes[y * rep.bytesPerRow + x * 4 + offset]
            if alpha == 0 { transparent += 1 }
            if alpha == 255 { opaque += 1 }
            if alpha > 0 { visible += 1 }
        }
    }
    guard transparent > 0 && visible > 0 else { fatalError("No real transparent cutout: " + name) }
    let item: [String: Any] = ["file": name, "width": rep.pixelsWide,
        "height": rep.pixelsHigh, "transparent_pixels": transparent,
        "visible_pixels": visible, "opaque_pixels": opaque]
    let result = try JSONSerialization.data(withJSONObject: item, options: [.sortedKeys])
    print(String(data: result, encoding: .utf8)!)
}
'''


def prompt(character: str, variant: str) -> str:
    color, prop = CHARACTERS[character]
    if variant == "idle":
        if character == "intake":
            return f"{STYLE} Character: intake. Bright green body {color}. {prop} Deliver just one isolated character image."
        return (
            f"{STYLE} INPUT IMAGE: style and body-shape reference ONLY, intake mascot. "
            "Create another character in EXACTLY this same mascot family: same simple "
            "teardrop bean silhouette, vertical black oval eyes, no mouth, no legs, stubby "
            "nub arms, matte clay render, frontal framing. "
            f"Change body color to {color}. Remove the intake speech bubble and waving marks. "
            f"Character: {character}, {prop} The entire image MUST be square. "
            "Deliver one isolated full-body transparent PNG. Do not add eyebrows, blush, "
            "white eye highlights, a mouth or legs."
        )
    return (
        f"Use case: identity-preserve. Input image 1 is the EDIT TARGET: {character} idle mascot. "
        "Create a pose/expression variant of this exact same character. Preserve its exact "
        "body silhouette, body color, two black oval eyes, material, lighting, camera, "
        "rendering style and relative scale. No mouth, neck, legs, feet or extra characters. "
        f"{VARIATIONS[variant]} Actual transparent alpha background, square 1024x1024 PNG, "
        "full body and all props visible with generous margins, faint contact shadow only. "
        "No text or logos. Return just the edited image."
    )


def png_info(path: Path) -> tuple[int, int, bool]:
    with path.open("rb") as stream:
        header = stream.read(33)
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"Not a PNG: {path}")
        width, height, _depth, color = struct.unpack(">IIBB", header[16:26])
        alpha = color in (4, 6)
        while not alpha:
            chunk = stream.read(8)
            if len(chunk) != 8:
                break
            length, kind = struct.unpack(">I4s", chunk)
            if kind == b"tRNS":
                alpha = True
                break
            stream.seek(length + 4, 1)
    return width, height, alpha


def manifest(model: str, normalize: bool = False) -> None:
    entries: dict[str, dict[str, str]] = {}
    transparent = True
    for character in CHARACTERS:
        variants = {}
        for variant in ("idle", "working", "alert"):
            path = ROOT / character / f"{variant}.png"
            if not path.is_file():
                continue
            width, height, alpha = png_info(path)
            if (width, height) != (1024, 1024):
                if not normalize or not shutil.which("swift"):
                    raise ValueError(f"Expected 1024x1024: {path} is {width}x{height}")
                subprocess.run(["swift", "-e", NORMALIZE_SWIFT, "1024", "", str(path)], check=True, capture_output=True)
                width, height, alpha = png_info(path)
            transparent = transparent and alpha
            variants[variant] = f"/mascots/{character}/{variant}.png"
            small = path.with_name(f"{variant}@320.png")
            if small.is_file():
                small_width, small_height, small_alpha = png_info(small)
                if (small_width, small_height) != (320, 320):
                    raise ValueError(f"Expected 320x320: {small}")
                transparent = transparent and small_alpha
                variants[f"{variant}_small"] = f"/mascots/{character}/{variant}@320.png"
        if variants:
            entries[character] = variants
    (ROOT / "manifest.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "transparent": transparent if entries else False,
        "characters": entries,
    }, indent=2) + "\n")
    print(f"Manifest: {sum(map(len, entries.values()))} images, {len(entries)} characters, alpha={transparent}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character", choices=CHARACTERS, action="append")
    parser.add_argument("--variant", choices=("idle", "working", "alert", "all"), default="all")
    parser.add_argument("--model", default="gpt-image-1.5", help="API regeneration model; native tool does not expose a model id")
    parser.add_argument("--cli", type=Path, default=Path.home() / ".codex/skills/.system/imagegen/scripts/image_gen.py")
    parser.add_argument("--run-api", action="store_true", help="Make paid API requests; otherwise print prompts only")
    parser.add_argument("--force", action="store_true", help="Replace existing images")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--validate", action="store_true", help="Verify complete 60-image set, dimensions and actual alpha pixels (macOS)")
    parser.add_argument("--small", action="store_true", help="Write 320-square companions for existing originals and rebuild the manifest")
    parser.add_argument("--normalize", action="store_true", help="Fit native outputs into a transparent 1024 square with macOS AppKit")
    args = parser.parse_args()
    if args.validate:
        paths = [ROOT / character / f"{variant}{suffix}.png"
                 for character in CHARACTERS for variant in ("idle", "working", "alert")
                 for suffix in ("", "@320")]
        missing = [str(path.relative_to(ROOT)) for path in paths if not path.is_file()]
        if missing:
            parser.error("Missing images: " + ", ".join(missing))
        subprocess.run(["swift", "-e", VALIDATE_SWIFT, *map(str, paths)], check=True)
        return
    if args.small:
        paths = [ROOT / character / f"{variant}.png"
                 for character in CHARACTERS for variant in ("idle", "working", "alert")
                 if (ROOT / character / f"{variant}.png").is_file()]
        if paths:
            subprocess.run(["swift", "-e", NORMALIZE_SWIFT, "320", "@320", *map(str, paths)], check=True)
        manifest(args.model, args.normalize)
        return
    if args.manifest_only:
        manifest(args.model, args.normalize)
        return
    if args.run_api and (not args.cli.is_file() or not os.environ.get("OPENAI_API_KEY")):
        parser.error("API generation requires the Codex imagegen CLI and OPENAI_API_KEY")
    variants = ("idle", "working", "alert") if args.variant == "all" else (args.variant,)
    for variant in variants:
        for character in args.character or CHARACTERS:
            text = prompt(character, variant)
            target = ROOT / character / f"{variant}.png"
            if not args.run_api:
                print(json.dumps({"character": character, "variant": variant, "prompt": text}))
                continue
            if target.exists() and not args.force:
                print(f"Keeping {target.relative_to(ROOT)}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            is_edit = variant != "idle" or character != "intake"
            command = [sys.executable, str(args.cli), "edit" if is_edit else "generate",
                       "--model", args.model, "--prompt", text, "--size", "1024x1024",
                       "--quality", "high", "--background", "transparent", "--output-format", "png",
                       "--out", str(target), "--no-augment"]
            if is_edit:
                source = ROOT / ("intake" if variant == "idle" else character) / "idle.png"
                if not source.is_file():
                    parser.error(f"Generate {character}/idle.png before its variants")
                command += ["--image", str(source), "--input-fidelity", "high"]
            if args.force:
                command += ["--force"]
            subprocess.run(command, check=True)
    if args.run_api:
        manifest(args.model, args.normalize)


if __name__ == "__main__":
    main()
