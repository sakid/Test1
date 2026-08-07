#!/usr/bin/env bash
set -uo pipefail

# Public-reference extraction only. This script never supplies credentials,
# cookies, access tokens, MatterPak access, or private-model authentication.
rm -rf work output
mkdir -p work/logs work/discovery output

cp mp360/cyan-models.json work/discovery/models.json
python - <<'PY'
import json
from pathlib import Path

data = json.loads(Path('work/discovery/models.json').read_text())
models = [item['modelSid'] for item in data['models']]
if len(models) != 7 or len(set(models)) != 7:
    raise SystemExit(f'Expected seven distinct Cyan models, got {models}')
Path('work/discovery/models.txt').write_text('\n'.join(models) + '\n')
print(json.dumps({
    'resolvedModels': [
        {
            'modelSid': item['modelSid'],
            'label': item.get('label'),
            'contexts': item.get('contexts', []),
        }
        for item in data['models']
    ]
}, indent=2))
PY

cat work/discovery/models.txt

git clone --quiet --depth 1 \
  https://github.com/rebane2001/matterport-dl.git \
  work/matterport-dl
python -m pip install --quiet -r work/matterport-dl/requirements.txt

python - <<'PY'
from pathlib import Path

path = Path('work/matterport-dl/matterport-dl.py')
text = path.read_text()
replacements = {
    'SWEEP_DO_4K = True': 'SWEEP_DO_4K = False',
    'depths = ["512", "1k", "2k"]': 'depths = ["512", "1k"]',
    '        await downloadDAM(accessurl, modeldata["job"]["uuid"])': '        pass  # panorama-only public reference run',
    '    await downloadAssets(staticbase, base_page_text)': '    pass  # skip viewer assets',
    '    await downloadWebglVendors(base_page_text)': '    pass  # skip viewer vendors',
    '    patchShowcase()': '    pass  # no offline viewer patch',
    '    await downloadPlugins(pageid)': '    pass  # skip plugins',
    '        await downloadPics(pageid)': '        pass  # skip gallery images',
    '    await downloadAttachments()': '    pass  # skip attachments',
}
for old, new in replacements.items():
    if old not in text:
        raise SystemExit(f'Expected upstream code not found: {old}')
    text = text.replace(old, new, 1)
path.write_text(text)
PY

printf 'model_sid\tdownload_exit\ttile_count\n' > work/model-download-status.tsv
while IFS= read -r SID; do
  [[ "$SID" =~ ^[A-Za-z0-9_-]{11}$ ]] || continue
  if [[ "$SID" == "i1MqSM99sWw" ]]; then
    echo "Skipping previously delivered fitness-center model $SID"
    continue
  fi
  echo "Downloading public panorama tiles for $SID"
  set +e
  timeout 55m python work/matterport-dl/matterport-dl.py "$SID" \
    --base-folder "$PWD/work/downloads" \
    --no-advanced-download \
    --no-tilde \
    --console-log \
    > "work/logs/download-$SID.log" 2>&1
  STATUS=$?
  set -e
  TILE_COUNT=$(find "work/downloads/$SID" \
    -type f -name '*_face*_*.jpg' 2>/dev/null \
    | wc -l | tr -d ' ')
  printf '%s\t%s\t%s\n' "$SID" "$STATUS" "$TILE_COUNT" \
    | tee -a work/model-download-status.tsv
done < work/discovery/models.txt

python mp360/convert_cyan_tours.py \
  2>&1 | tee work/logs/conversion.log
cp work/model-download-status.tsv output/
cp -r work/logs output/

cat output/RUN_SUMMARY.md
cat output/BUNDLE_SHA256.txt
ls -lh output/cyan-pdx-remaining-matterport-panoramas.zip
