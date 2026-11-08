#!/usr/bin/env bash
set -euo pipefail

TAG="${GITHUB_REF#refs/tags/}"
API="${GITHUB_SERVER_URL}/api/v1/repos/${GITHUB_REPOSITORY}"

release_id=$(curl -fsS -H "Authorization: token $TOKEN" \
    "$API/releases/tags/$TAG" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))' \
    2>/dev/null || true)
if [ -z "${release_id:-}" ]; then
    release_id=$(curl -fsS -X POST -H "Authorization: token $TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"tag_name\":\"$TAG\",\"name\":\"$TAG\",\"target_commitish\":\"$GITHUB_SHA\"}" \
        "$API/releases" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
fi

for asset in "$@"; do
    if [ ! -f "$asset" ]; then
        echo "Release asset is missing: $asset" >&2
        exit 1
    fi
    name="${asset##*/}"
    curl -fsS -H "Authorization: token $TOKEN" "$API/releases/$release_id/assets" \
        | python3 -c "import json,sys; [print(a['id']) for a in json.load(sys.stdin) if a['name'] == '$name']" \
        | while read -r asset_id; do
            [ -z "$asset_id" ] || curl -fsS -X DELETE -H "Authorization: token $TOKEN" \
                "$API/releases/$release_id/assets/$asset_id"
        done
    encoded_name=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$name")
    curl -fsS -X POST -H "Authorization: token $TOKEN" \
        -F "attachment=@$asset" "$API/releases/$release_id/assets?name=$encoded_name"
done
