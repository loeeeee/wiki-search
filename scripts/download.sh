#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUBMODULE_REL="data/raw/hotpotqa"
SUBMODULE_PATH="${REPO_ROOT}/${SUBMODULE_REL}"
VENV_DIR="${SUBMODULE_PATH}/.venv"

SPACY_VERSION="${SPACY_VERSION:-3.7.2}"
SPACY_MODEL="${SPACY_MODEL:-en_core_web_sm}"
UPDATE_SUBMODULE=1
INSTALL_DEPENDENCIES=1
RUN_DOWNLOAD=1

usage() {
  cat <<'EOF'
Usage: scripts/download-hotpotqa.sh [options]

Ensures the HotpotQA upstream repository is present as a git submodule,
sets up a local virtual environment, installs dependencies, and invokes
the upstream download.sh script.

Options:
  --no-update-submodule   Skip pulling the latest commit from upstream
  --no-venv               Skip creating/updating the Python virtual environment
  --no-download           Skip running the upstream download.sh script
  -h, --help              Show this help message and exit

Environment variables:
  SPACY_VERSION           Version spec passed to pip (default: 3.7.2)
  SPACY_MODEL             spaCy model to download (default: en_core_web_sm)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-update-submodule)
      UPDATE_SUBMODULE=0
      shift
      ;;
    --no-venv)
      INSTALL_DEPENDENCIES=0
      shift
      ;;
    --no-download)
      RUN_DOWNLOAD=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ $UPDATE_SUBMODULE -eq 1 ]]; then
  echo "[INFO] Ensuring git submodule ${SUBMODULE_REL} (shallow clone)"
  git -C "${REPO_ROOT}" submodule update --init --depth 1 --recursive "${SUBMODULE_REL}"
  git -C "${SUBMODULE_PATH}" fetch --depth 1 origin master
  git -C "${SUBMODULE_PATH}" reset --hard origin/master
else
  if [[ ! -d "${SUBMODULE_PATH}" ]]; then
    echo "[ERROR] Submodule directory ${SUBMODULE_PATH} missing. Re-run without --no-update-submodule." >&2
    exit 1
  fi
fi

REQUIRED_FILES=(
  "hotpot_train_v1.1.json"
  "hotpot_dev_distractor_v1.json"
  "hotpot_dev_fullwiki_v1.json"
  "glove.840B.300d.zip"
  "glove.840B.300d.txt"
)

missing_items=()
if [[ $RUN_DOWNLOAD -eq 1 ]]; then
  for rel_path in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "${SUBMODULE_PATH}/${rel_path}" ]]; then
      missing_items+=("${rel_path}")
    fi
  done
  if [[ ${#missing_items[@]} -eq 0 ]]; then
    echo "[INFO] All required HotpotQA artifacts already present; skipping upstream download.sh"
    RUN_DOWNLOAD=0
  fi
fi

if [[ $RUN_DOWNLOAD -eq 1 ]]; then
  if [[ $INSTALL_DEPENDENCIES -eq 1 ]]; then
    echo "[INFO] Creating/updating virtual environment at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
    if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
      echo "[ERROR] Failed to create virtual environment at ${VENV_DIR}" >&2
      exit 1
    fi
  else
    if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
      echo "[ERROR] Virtual environment not found at ${VENV_DIR}. Re-run without --no-venv." >&2
      exit 1
    fi
  fi

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  PYTHON="${VENV_DIR}/bin/python"

  if [[ $INSTALL_DEPENDENCIES -eq 1 ]]; then
    if "${PYTHON}" - <<PY
import sys
try:
    import spacy
except ImportError:
    sys.exit(1)
if spacy.__version__ == "${SPACY_VERSION}":
    sys.exit(0)
sys.exit(1)
PY
    then
      echo "[INFO] spaCy ${SPACY_VERSION} already installed; skipping pip install"
    else
      python -m pip install --upgrade pip setuptools wheel
      python -m pip install --upgrade "spacy==${SPACY_VERSION}"
    fi

    if "${PYTHON}" - <<PY
import sys
try:
    __import__("${SPACY_MODEL}")
except Exception:
    sys.exit(1)
sys.exit(0)
PY
    then
      echo "[INFO] spaCy model ${SPACY_MODEL} already available; skipping download"
    else
      echo "[INFO] Downloading spaCy model ${SPACY_MODEL}"
      python -m spacy download "${SPACY_MODEL}"
    fi
  fi

  echo "[INFO] Missing artifacts detected: ${missing_items[*]}"
  pushd "${SUBMODULE_PATH}" >/dev/null
  bash ./download.sh
  popd >/dev/null

  deactivate
fi

# Remove any duplicate files produced by wget re-runs (e.g., *.1).
find "${SUBMODULE_PATH}" -maxdepth 1 -type f -name '*.1' -print -delete || true

MANIFEST_PATH="${SUBMODULE_PATH}/manifest.json"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
CANDIDATES=(
  "hotpot_train_v1.1.json"
  "hotpot_dev_distractor_v1.json"
  "hotpot_dev_fullwiki_v1.json"
  "hotpot_test_fullwiki_v1.json"
  "enwiki-20171001-pages-meta-current-withlinks-processed.tar.bz2"
  "enwiki-20171001-pages-meta-current-withlinks-abstracts.tar.bz2"
  "glove.840B.300d.zip"
)

entries=()
for candidate in "${CANDIDATES[@]}"; do
  file_path="${SUBMODULE_PATH}/${candidate}"
  if [[ -f "${file_path}" ]]; then
    size_bytes=$(stat -c%s "${file_path}")
    if [[ "${candidate}" == "glove.840B.300d.zip" ]]; then
      sha256="skipped-too-large"
      md5="skipped-too-large"
    else
      sha256=$(sha256sum "${file_path}" | awk '{print $1}')
      md5=$(md5sum "${file_path}" | awk '{print $1}')
    fi
    entries+=("    {
      \"filename\": \"${candidate}\",
      \"size_bytes\": ${size_bytes},
      \"sha256\": \"${sha256}\",
      \"md5\": \"${md5}\"
    }")
  fi
done

{
  echo "{"
  echo "  \"generated_at\": \"${TIMESTAMP}\","
  echo "  \"submodule\": \"${SUBMODULE_REL}\","
  echo "  \"files\": ["
  for i in "${!entries[@]}"; do
    if [[ $i -lt $((${#entries[@]} - 1)) ]]; then
      echo "${entries[$i]},"
    else
      echo "${entries[$i]}"
    fi
  done
  echo "  ]"
  echo "}"
} > "${MANIFEST_PATH}"

echo "[DONE] Manifest written to ${MANIFEST_PATH}"
