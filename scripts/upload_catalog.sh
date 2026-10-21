#!/usr/bin/env bash
# Upload dist/index.json (+ .sig) and dist/assets/* to the catalog release.
# Images are named by content hash, so uploading never changes a file the
# current index points at; only index.json and its signature are replaced.
set -euo pipefail

: "${CATALOG_TAG:=catalog}"

if ! gh release view "$CATALOG_TAG" >/dev/null 2>&1; then
  gh release create "$CATALOG_TAG" \
    --latest=false \
    --title "Plugin catalog" \
    --notes "Machine-readable plugin index. This release is updated in place. Do not use /releases/latest."
fi
gh release edit "$CATALOG_TAG" --latest=false

existing="$(gh release view "$CATALOG_TAG" --json assets --jq '.assets[].name')"
shopt -s nullglob
images=()
for path in dist/assets/*; do
  if ! grep -qxF "$(basename "$path")" <<< "$existing"; then
    images+=("$path")
  fi
done
if [ "${#images[@]}" -gt 0 ]; then
  gh release upload "$CATALOG_TAG" "${images[@]}"
fi

# Images first, then the index that references them.
if [ ! -f dist/index.json.sig ]; then
  echo "dist/index.json.sig 不存在：MovieClaw 只接受签名的索引" >&2
  exit 1
fi
gh release upload "$CATALOG_TAG" dist/index.json dist/index.json.sig --clobber
