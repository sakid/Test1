#!/usr/bin/env python3
from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path.cwd()
WORK = ROOT / "work"
DOWNLOADS = WORK / "downloads"
DISCOVERY = WORK / "discovery"
OUTPUT = ROOT / "output"
PANORAMAS = OUTPUT / "panoramas"
CONTACTS = OUTPUT / "contact_sheets"
METADATA = OUTPUT / "metadata"
KNOWN_FITNESS = "i1MqSM99sWw"

for directory in (OUTPUT, PANORAMAS, CONTACTS, METADATA):
    directory.mkdir(parents=True, exist_ok=True)

TILE_PATTERN = re.compile(r"^(512|1k)_face([0-5])_(\d+)_(\d+)\.jpg$")
FACE_ORDER_C6X1 = [2, 4, 0, 5, 1, 3]  # right, left, up, down, front, back


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-_")
    return value[:80] or "unlabeled"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def label_for_model(model_sid: str, discovery_by_sid: dict[str, dict]) -> str:
    if model_sid == KNOWN_FITNESS:
        return "Fitness-Center"
    item = discovery_by_sid.get(model_sid, {})
    blob = " ".join(
        str(value)
        for key in ("actions", "contexts", "sources")
        for value in item.get(key, [])
    )
    rules = [
        (r"\bAS2\b|Studio", "Studio-AS2"),
        (r"\bAT2\b|Townhome", "Townhome-AT2"),
        (r"\bA5\b|One Bedroom", "One-Bedroom-A5"),
        (r"\bB1\b|Two Bedroom,? One Bath", "Two-Bedroom-B1"),
        (r"\bB3\b|Two Bedroom,? Two Bath", "Two-Bedroom-B3"),
        (r"\bC1\b|Three Bedroom", "Three-Bedroom-C1"),
    ]
    for pattern, label in rules:
        if re.search(pattern, blob, re.I):
            return label

    # Search downloaded public metadata for a useful model title.
    model_root = DOWNLOADS / model_sid
    text_fragments: list[str] = []
    if model_root.exists():
        for path in list(model_root.rglob("*.json"))[:300]:
            try:
                text_fragments.append(path.read_text(errors="replace")[:2_000_000])
            except Exception:
                pass
    metadata_blob = " ".join(text_fragments)
    for pattern, label in rules:
        if re.search(pattern, metadata_blob, re.I):
            return label
    return f"Matterport-{model_sid}"


def complete(files: dict[tuple[int, int, int], Path], resolution: str) -> bool:
    grid = 2 if resolution == "1k" else 1
    required = {
        (face, x, y)
        for face in range(6)
        for y in range(grid)
        for x in range(grid)
    }
    return required.issubset(files)


def assemble_face(
    files: dict[tuple[int, int, int], Path], face: int, grid: int
) -> Image.Image:
    with Image.open(files[(face, 0, 0)]) as first:
        tile_width, tile_height = first.size
    canvas = Image.new("RGB", (tile_width * grid, tile_height * grid))
    for y in range(grid):
        for x in range(grid):
            with Image.open(files[(face, x, y)]) as tile:
                canvas.paste(tile.convert("RGB"), (x * tile_width, y * tile_height))
    return canvas


def convert_sweep(
    model_sid: str,
    label: str,
    index: int,
    sweep_dir: Path,
    files: dict[tuple[int, int, int], Path],
    resolution: str,
) -> dict:
    grid = 2 if resolution == "1k" else 1
    faces = [assemble_face(files, face, grid) for face in range(6)]
    strip = Image.new("RGB", (faces[0].width * 6, faces[0].height))
    for position, raw_face in enumerate(FACE_ORDER_C6X1):
        strip.paste(faces[raw_face], (position * faces[0].width, 0))

    destination = PANORAMAS / f"{safe_name(label)}_{model_sid}"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{index:04d}_{sweep_dir.name}.jpg"
    with tempfile.TemporaryDirectory() as temporary:
        strip_path = Path(temporary) / "cubemap-c6x1.png"
        strip.save(strip_path)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(strip_path),
                "-vf",
                "v360=input=c6x1:output=equirect:in_forder=rludfb:in_frot=000000:w=4096:h=2048",
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ],
            check=True,
        )
    with Image.open(target) as check:
        if check.size != (4096, 2048):
            raise RuntimeError(f"Unexpected output dimensions for {target}: {check.size}")
        check.verify()
    return {
        "modelSid": model_sid,
        "label": label,
        "sweepSid": sweep_dir.name,
        "sourceResolution": resolution,
        "width": 4096,
        "height": 2048,
        "bytes": target.stat().st_size,
        "sha256": sha256(target),
        "image": str(target.relative_to(OUTPUT)),
    }


def create_contact_sheet(model_sid: str, label: str, records: list[dict]) -> str | None:
    if not records:
        return None
    tiles: list[Image.Image] = []
    for record in records:
        source = OUTPUT / record["image"]
        with Image.open(source) as image:
            thumb = image.convert("RGB").resize((512, 256), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (512, 300), "white")
        tile.paste(thumb, (0, 0))
        ImageDraw.Draw(tile).text(
            (8, 264),
            f"{record['sweepSid'][:24]}  {record['sourceResolution']}",
            fill="black",
        )
        tiles.append(tile)
    columns = 3
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * 512, rows * 300), "white")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * 512, (index // columns) * 300))
    destination = CONTACTS / f"{safe_name(label)}_{model_sid}.jpg"
    sheet.save(destination, quality=91, optimize=True)
    return str(destination.relative_to(OUTPUT))


def add_tree_to_zip(archive: zipfile.ZipFile, directory: Path, prefix: str = "") -> None:
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            archive.write(path, Path(prefix) / path.relative_to(directory))


def main() -> None:
    discovery_path = DISCOVERY / "models.json"
    discovery = json.loads(discovery_path.read_text()) if discovery_path.exists() else {"models": []}
    discovery_by_sid = {
        item["modelSid"]: item for item in discovery.get("models", [])
    }

    records: list[dict] = []
    model_summaries: list[dict] = []
    if DOWNLOADS.exists():
        model_dirs = [
            path
            for path in DOWNLOADS.iterdir()
            if path.is_dir() and re.fullmatch(r"[A-Za-z0-9_-]{11}", path.name)
        ]
    else:
        model_dirs = []

    for model_dir in sorted(model_dirs):
        model_sid = model_dir.name
        if model_sid == KNOWN_FITNESS:
            continue
        label = label_for_model(model_sid, discovery_by_sid)
        sweep_dirs = sorted(
            {
                tile.parent
                for tile in model_dir.rglob("*_face*_*.jpg")
                if TILE_PATTERN.match(tile.name)
            }
        )
        model_records: list[dict] = []
        for sweep_dir in sweep_dirs:
            by_resolution: dict[str, dict[tuple[int, int, int], Path]] = {
                "512": {},
                "1k": {},
            }
            for tile in sweep_dir.iterdir():
                match = TILE_PATTERN.match(tile.name)
                if not match:
                    continue
                resolution, face, x, y = match.groups()
                by_resolution[resolution][(int(face), int(x), int(y))] = tile
            resolution = (
                "1k"
                if complete(by_resolution["1k"], "1k")
                else "512"
                if complete(by_resolution["512"], "512")
                else None
            )
            if not resolution:
                continue
            record = convert_sweep(
                model_sid,
                label,
                len(model_records) + 1,
                sweep_dir,
                by_resolution[resolution],
                resolution,
            )
            records.append(record)
            model_records.append(record)
        contact_sheet = create_contact_sheet(model_sid, label, model_records)
        model_summary = {
            "modelSid": model_sid,
            "label": label,
            "panoramaCount": len(model_records),
            "contactSheet": contact_sheet,
            "sweeps": [record["sweepSid"] for record in model_records],
        }
        model_summaries.append(model_summary)
        (METADATA / f"{model_sid}.json").write_text(
            json.dumps(model_records, indent=2) + "\n"
        )

    manifest = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "projection": "equirectangular",
        "imageFormat": "JPEG",
        "width": 4096,
        "height": 2048,
        "faceOrderC6x1RightLeftUpDownFrontBack": FACE_ORDER_C6X1,
        "remainingModelCount": sum(1 for item in model_summaries if item["panoramaCount"] > 0),
        "remainingPanoramaCount": len(records),
        "models": model_summaries,
        "images": records,
        "discovery": discovery,
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    summary_lines = [
        "# Cyan PDX remaining Matterport tours",
        "",
        f"- Remaining models converted: {manifest['remainingModelCount']}",
        f"- 4096x2048 equirectangular JPEGs: {manifest['remainingPanoramaCount']}",
        "- Fitness-center model excluded because it was previously delivered.",
        "- Access source: panorama resources served to normally accessible public Matterport viewers.",
        "- Owner credentials, private authentication, MatterPak, mesh/DAM, gallery, plugin, and attachment downloads were not used.",
        "",
        "## Models",
    ]
    for model in model_summaries:
        summary_lines.append(
            f"- {model['label']} (`{model['modelSid']}`): {model['panoramaCount']} panoramas"
        )
    (OUTPUT / "RUN_SUMMARY.md").write_text("\n".join(summary_lines) + "\n")

    checksum_lines = [f"{record['sha256']}  {record['image']}" for record in records]
    (OUTPUT / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + ("\n" if checksum_lines else ""))

    bundle = OUTPUT / "cyan-pdx-remaining-matterport-panoramas.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        add_tree_to_zip(archive, PANORAMAS, "panoramas")
        add_tree_to_zip(archive, CONTACTS, "contact_sheets")
        add_tree_to_zip(archive, METADATA, "metadata")
        for name in ("manifest.json", "RUN_SUMMARY.md", "SHA256SUMS.txt"):
            path = OUTPUT / name
            if path.exists():
                archive.write(path, name)
        add_tree_to_zip(archive, DISCOVERY, "discovery")
        add_tree_to_zip(archive, WORK / "logs", "logs")
        status = WORK / "model-download-status.tsv"
        if status.exists():
            archive.write(status, "model-download-status.tsv")

    bundle_hash = sha256(bundle)
    (OUTPUT / "BUNDLE_SHA256.txt").write_text(f"{bundle_hash}  {bundle.name}\n")
    print(json.dumps({
        "bundle": str(bundle),
        "bundleBytes": bundle.stat().st_size,
        "bundleSha256": bundle_hash,
        "remainingModelCount": manifest["remainingModelCount"],
        "remainingPanoramaCount": manifest["remainingPanoramaCount"],
        "models": model_summaries,
    }, indent=2))


if __name__ == "__main__":
    main()
