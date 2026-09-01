#!/bin/sh
set -eu

usage() {
    echo "usage: install-preview.sh --manifest <immutable-https-url> [--ca-file <pem>]" >&2
    exit 2
}

manifest_url=""
ca_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest)
            [ "$#" -ge 2 ] || usage
            manifest_url=$2
            shift 2
            ;;
        --ca-file)
            [ "$#" -ge 2 ] || usage
            ca_file=$2
            shift 2
            ;;
        *) usage ;;
    esac
done
[ -n "$manifest_url" ] || usage

case "$manifest_url" in
    https://*) ;;
    *) echo "preview manifest URL must use HTTPS" >&2; exit 2 ;;
esac
[ -z "$ca_file" ] || [ -f "$ca_file" ] || {
    echo "Preview artifact CA file does not exist" >&2
    exit 2
}

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required" >&2; exit 1; }

preview_tmp=$(mktemp -d "${TMPDIR:-/tmp}/research-preview.XXXXXX")
cleanup() { rm -rf "$preview_tmp"; }
trap cleanup EXIT HUP INT TERM

manifest=$preview_tmp/manifest.json
fetch() {
    fetch_url=$1
    fetch_output=$2
    if [ -n "$ca_file" ]; then
        curl --fail --silent --show-error --location \
            --proto '=https' --tlsv1.2 --cacert "$ca_file" \
            "$fetch_url" --output "$fetch_output"
    else
        curl --fail --silent --show-error --location \
            --proto '=https' --tlsv1.2 "$fetch_url" --output "$fetch_output"
    fi
}

fetch "$manifest_url" "$manifest"

python3 - "$manifest" "$preview_tmp" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest_path, output_root = Path(sys.argv[1]), Path(sys.argv[2])
payload = json.loads(manifest_path.read_text(encoding="utf-8"))
required = {
    "manifest_version", "channel", "version", "preview_commit",
    "source_candidate", "wheel"
}
if set(payload) != required or payload["manifest_version"] != 1:
    raise SystemExit("unsupported or non-canonical Preview manifest")
if payload["channel"] != "preview":
    raise SystemExit("installer accepts only an explicit Preview channel")
version = payload["version"]
if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+rc[0-9]+\.dev[0-9]+", version) is None:
    raise SystemExit("invalid Preview version")
source = payload["source_candidate"]
preview_commit = payload["preview_commit"]
if not isinstance(preview_commit, str) or re.fullmatch(r"[0-9a-f]{40}", preview_commit) is None:
    raise SystemExit("invalid Preview implementation commit")
if (
    not isinstance(source, dict)
    or set(source) != {"commit", "tag"}
    or re.fullmatch(r"[0-9a-f]{40}", source.get("commit", "")) is None
    or re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+", source.get("tag", "")) is None
):
    raise SystemExit("invalid immutable source candidate")
wheel = payload["wheel"]
if (
    not isinstance(wheel, dict)
    or set(wheel) != {"filename", "sha256", "url"}
    or re.fullmatch(r"[0-9a-f]{64}", wheel.get("sha256", "")) is None
    or not wheel.get("url", "").startswith("https://")
    or re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", wheel.get("filename", "")) is None
):
    raise SystemExit("invalid immutable wheel record")
for name, value in {
    "version": version,
    "candidate_commit": source["commit"],
    "candidate_tag": source["tag"],
    "preview_commit": preview_commit,
    "wheel_sha256": wheel["sha256"],
    "wheel_url": wheel["url"],
    "wheel_filename": wheel["filename"],
}.items():
    (output_root / name).write_text(value, encoding="utf-8")
PY

version=$(cat "$preview_tmp/version")
candidate_commit=$(cat "$preview_tmp/candidate_commit")
candidate_tag=$(cat "$preview_tmp/candidate_tag")
preview_commit=$(cat "$preview_tmp/preview_commit")
wheel_sha256=$(cat "$preview_tmp/wheel_sha256")
wheel_url=$(cat "$preview_tmp/wheel_url")
wheel_filename=$(cat "$preview_tmp/wheel_filename")
wheel=$preview_tmp/$wheel_filename

fetch "$wheel_url" "$wheel"
actual_sha256=$(python3 - "$wheel" <<'PY'
import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
with Path(sys.argv[1]).open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
print(digest.hexdigest())
PY
)
[ "$actual_sha256" = "$wheel_sha256" ] || {
    echo "wheel SHA-256 does not match the immutable Preview manifest" >&2
    exit 1
}

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_home=${XDG_BIN_HOME:-"$HOME/.local/bin"}
install_root=$data_home/research-agent-system/preview/$version
[ ! -e "$install_root" ] || {
    echo "refusing to replace existing Preview installation: $install_root" >&2
    exit 1
}
mkdir -p "$(dirname "$install_root")" "$bin_home"
[ ! -e "$bin_home/research" ] && [ ! -L "$bin_home/research" ] || {
    echo "refusing to replace existing executable: $bin_home/research" >&2
    exit 1
}
python3 -m venv "$install_root"
"$install_root/bin/python" -m pip install --disable-pip-version-check \
    "$wheel[tui]"
ln -s "$install_root/bin/research" "$bin_home/research"

echo "Installed Research Developer Preview $version"
echo "Preview implementation: $preview_commit"
echo "Source candidate: $candidate_tag ($candidate_commit)"
echo "Executable: $bin_home/research"
echo "Run: research"
