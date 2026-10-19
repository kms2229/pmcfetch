#!/usr/bin/env bash
# Run a full corpus pull on an Azure VM, then sync results to Blob Storage.
#
#   ./run.sh <corpus.json> <storage-account> <container>
#
# Assumes: pmcfetch installed, `az login` done (or a managed identity attached).
set -euo pipefail

CORPUS="${1:?usage: run.sh <corpus.json> <storage-account> <container>}"
ACCOUNT="${2:?storage account name}"
CONTAINER="${3:?blob container name}"
OUT="${OUT:-/mnt/pmc/pdfs}"

mkdir -p "$OUT"

echo "==> namespaces in $CORPUS"
pmcfetch namespaces --corpus "$CORPUS"

echo
echo "==> pulling from the PMC Open Access S3 subset"
# S3 is not rate limited, so --rps 0 disables pacing and --workers goes wide.
pmcfetch download \
  --corpus "$CORPUS" \
  --out "$OUT" \
  --workers 16 \
  --rps 0 \
  --failed-log "$OUT/failed.txt" \
  || echo "(some articles failed — see $OUT/failed.txt)"

echo
echo "==> uploading to blob storage"
az storage blob upload-batch \
  --account-name "$ACCOUNT" \
  --destination "$CONTAINER" \
  --source "$OUT" \
  --overwrite \
  --output table

echo
echo "==> done. Local: $OUT   Blob: $ACCOUNT/$CONTAINER"
du -sh "$OUT"
