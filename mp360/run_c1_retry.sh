#!/usr/bin/env bash
set -uo pipefail

SID=6Hv9LyTsdxw
EXPECTED_TILE_COUNT=690

rm -rf work output
mkdir -p work/logs work/discovery output
cp mp360/cyan-models.json work/discovery/models.json
printf '%s\n' "$SID" > work/discovery/models.txt

git clone --quiet --depth 1 \
  https://github.com/rebane2001/matterport-dl.git \
  work/matterport-dl
python -m pip install --quiet -r work/matterport-dl/requirements.txt

python - <<'PY'
from pathlib import Path

path = Path('work/matterport-dl/matterport-dl.py')
text = path.read_text()
replacements = {
    'MAX_CONCURRENT_REQUESTS = 20': 'MAX_CONCURRENT_REQUESTS = 2',
    'MAX_CONCURRENT_TASKS = 64': 'MAX_CONCURRENT_TASKS = 2',
    'SWEEP_DO_4K = True': 'SWEEP_DO_4K = False',
    'depths = ["512", "1k", "2k"]': 'depths = ["512", "1k"]',
    '        await downloadDAM(accessurl, modeldata["job"]["uuid"])': '        pass  # C1 panorama-only retry',
    '    await downloadAssets(staticbase, base_page_text)': '    pass  # C1 panorama-only retry',
    '    await downloadWebglVendors(base_page_text)': '    pass  # C1 panorama-only retry',
    '    patchShowcase()': '    pass  # C1 panorama-only retry',
    '    await downloadPlugins(pageid)': '    pass  # C1 panorama-only retry',
    '        await downloadPics(pageid)': '        pass  # C1 panorama-only retry',
    '    await downloadAttachments()': '    pass  # C1 panorama-only retry',
}
for old, new in replacements.items():
    if old not in text:
        raise SystemExit(f'Expected upstream code not found: {old}')
    text = text.replace(old, new, 1)
path.write_text(text)
PY

FINAL_STATUS=1
for ATTEMPT in 1 2 3 4; do
  echo "C1 download attempt $ATTEMPT with two-request concurrency"
  set +e
  timeout 70m python work/matterport-dl/matterport-dl.py "$SID" \
    --base-folder "$PWD/work/downloads" \
    --no-advanced-download \
    --no-tilde \
    --console-log \
    > "work/logs/download-${SID}-attempt-${ATTEMPT}.log" 2>&1
  STATUS=$?
  set -e

  TILE_COUNT=$(find "work/downloads/$SID" \
    -type f -name '*_face*_*.jpg' 2>/dev/null \
    | wc -l | tr -d ' ')
  COMPLETE_1K=$(python - <<'PY'
from pathlib import Path
import re
root=Path('work/downloads/6Hv9LyTsdxw')
pattern=re.compile(r'^1k_face([0-5])_([01])_([01])\.jpg$')
complete=0
candidates=set()
for path in root.rglob('1k_face*_*.jpg') if root.exists() else []:
    if pattern.match(path.name): candidates.add(path.parent)
required={(f,x,y) for f in range(6) for x in range(2) for y in range(2)}
for directory in candidates:
    found=set()
    for path in directory.iterdir():
        match=pattern.match(path.name)
        if match: found.add(tuple(map(int, match.groups())))
    if required <= found: complete += 1
print(complete)
PY
)
  printf 'attempt=%s exit=%s tile_count=%s complete_1k=%s\n' \
    "$ATTEMPT" "$STATUS" "$TILE_COUNT" "$COMPLETE_1K" \
    | tee -a work/logs/c1-attempt-summary.log

  if [[ "$TILE_COUNT" -ge "$EXPECTED_TILE_COUNT" && "$COMPLETE_1K" -ge 23 ]]; then
    FINAL_STATUS=0
    break
  fi
  sleep $((ATTEMPT * 20))
done

printf 'model_sid\tdownload_exit\ttile_count\n' > work/model-download-status.tsv
TILE_COUNT=$(find "work/downloads/$SID" \
  -type f -name '*_face*_*.jpg' 2>/dev/null \
  | wc -l | tr -d ' ')
printf '%s\t%s\t%s\n' "$SID" "$FINAL_STATUS" "$TILE_COUNT" \
  >> work/model-download-status.tsv

python mp360/convert_cyan_tours.py \
  2>&1 | tee work/logs/conversion.log
cp work/model-download-status.tsv output/
cp -r work/logs output/

python - <<'PY'
import json
from pathlib import Path
manifest=json.loads(Path('output/manifest.json').read_text())
print(json.dumps({
    'remainingModelCount': manifest['remainingModelCount'],
    'remainingPanoramaCount': manifest['remainingPanoramaCount'],
    'models': manifest['models'],
}, indent=2))
if manifest['remainingModelCount'] != 1:
    raise SystemExit('C1 retry did not produce one model')
if manifest['remainingPanoramaCount'] < 23:
    raise SystemExit(
        f"Expected 23 C1 panoramas, got {manifest['remainingPanoramaCount']}"
    )
PY

cat output/RUN_SUMMARY.md
cat output/BUNDLE_SHA256.txt
ls -lh output/cyan-pdx-remaining-matterport-panoramas.zip
