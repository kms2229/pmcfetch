#!/usr/bin/env bash
# Provision a throwaway Azure VM sized for a full PMC OA corpus pull.
#
#   ./provision.sh <resource-group> <region> [vm-size] [disk-gb]
#
# Defaults pull ~19k PDFs (~50 GB) comfortably. The VM is CPU/network bound,
# not compute bound — a 2-4 vCPU burstable size is plenty.
set -euo pipefail

RG="${1:?usage: provision.sh <resource-group> <region> [vm-size] [disk-gb]}"
LOC="${2:?region, e.g. eastus}"
SIZE="${3:-Standard_D4s_v5}"
DISK_GB="${4:-256}"
VM="pmcfetch-$RANDOM"

az group create --name "$RG" --location "$LOC" --output none

az vm create \
  --resource-group "$RG" \
  --name "$VM" \
  --image Ubuntu2404 \
  --size "$SIZE" \
  --os-disk-size-gb "$DISK_GB" \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --output table

echo
echo "VM:  $VM"
echo "SSH: az ssh vm --resource-group $RG --name $VM"
echo
echo "When the pull is done, delete everything with:"
echo "  az group delete --name $RG --yes --no-wait"
