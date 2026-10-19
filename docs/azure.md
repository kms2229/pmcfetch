# Running pmcfetch on Azure

`pmcfetch` pulls from `s3://pmc-oa-opendata` — NLM's public PMC Open Access
Subset bucket. It's anonymous HTTPS, so **you need no AWS account and no AWS
credentials**, and it works identically from Azure, GCP, a laptop, or an HPC
login node.

Two properties make Azure a good fit:

- **Inbound transfer to Azure is free.** Pulling ~50 GB out of AWS Open Data and
  into Azure costs nothing on either side — AWS Open Data doesn't bill egress
  for this bucket, and Azure doesn't bill ingress.
- **No rate limit.** A datacenter IP is not penalised, so you can run
  `--workers 16 --rps 0`, which switches pacing off entirely; throughput is then
  bound by network bandwidth.

The cost you *do* pay is VM time, disk, and — if you later move the corpus off
Azure — outbound transfer. Plan for that before you pull 50 GB.

---

## 1. Provision

```bash
az login
./examples/azure/provision.sh pmc-rg eastus
```

`Standard_D4s_v5` with a 256 GB disk is the default and is ample: the workload is
network-bound, not compute-bound. Roughly 19k articles average ~2.7 MB, so budget
**~50 GB** plus headroom.

## 2. Install

```bash
az ssh vm --resource-group pmc-rg --name <vm-name>

sudo apt-get update && sudo apt-get install -y python3-pip python3-venv
python3 -m venv ~/.venv && source ~/.venv/bin/activate
pip install pmcfetch          # zero dependencies, stdlib only
pmcfetch --version
```

## 3. Verify before the real pull

```bash
# no network — just parses your corpus file
pmcfetch namespaces --corpus corpus.json

# one article, end to end
pmcfetch download --corpus corpus.json --out /tmp/t --limit 1 --workers 1
```

## 4. Pull

```bash
./examples/azure/run.sh corpus.json <storage-account> <container>
```

Or directly, one namespace at a time:

```bash
pmcfetch download \
  --corpus corpus.json \
  --out /mnt/pmc/pdfs \
  --namespace cardiology \
  --workers 16 --rps 0 \
  --failed-log /mnt/pmc/pdfs/failed_cardiology.txt
```

`--rps 0` disables pacing entirely, which is safe against S3.

Run it under `tmux` or `nohup` so an SSH drop doesn't kill the job.

## 5. Persist and tear down

Upload before deleting anything — the VM disk goes with the resource group:

```bash
az storage blob upload-batch \
  --account-name <acct> --destination <container> --source /mnt/pmc/pdfs --overwrite

az group delete --name pmc-rg --yes --no-wait
```

---

## Running it as a container instead

For a one-shot pull, Azure Container Instances avoids managing a VM:

```bash
az container create \
  --resource-group pmc-rg \
  --name pmcfetch-job \
  --image python:3.12-slim \
  --cpu 4 --memory 8 \
  --restart-policy Never \
  --azure-file-volume-account-name <acct> \
  --azure-file-volume-share-name pmc \
  --azure-file-volume-mount-path /mnt/pmc \
  --command-line "/bin/sh -c 'pip install pmcfetch && pmcfetch download --corpus /mnt/pmc/corpus.json --out /mnt/pmc/pdfs --workers 16 --rps 0'"
```

## Articles outside the OA subset

`pmcfetch` covers the PMC Open Access Subset only. Two things surface in the
failure log rather than silently succeeding:

- `ArticleNotInOASubsetError` — no object prefix for that PMCID.
- `PDFNotAvailableError` — the article is there, but the publisher licence does
  not permit PDF redistribution. The XML and plain text are still in the bucket
  and reachable via `client.metadata(pmcid)["xml_url"]`.

Retry or triage them from the log, which doubles as an input list:

```bash
pmcfetch download --corpus /mnt/pmc/pdfs/failed.txt --out /mnt/pmc/pdfs --flat
```

## Terms of use

PMC asks that systematic retrieval go through its sanctioned services rather than
scraping article pages. The S3 bucket *is* that route, and it is the only one
`pmcfetch` uses. The bucket README also asks
that you acknowledge NLM as the source and not imply NLM endorsement — worth
reading once at
[`pmc-oa-opendata.s3.amazonaws.com/README.txt`](https://pmc-oa-opendata.s3.amazonaws.com/README.txt)
before redistributing anything you build from it.

Per-article metadata (`client.metadata(pmcid)`) carries `license_code` and
`is_retracted`, so you can filter on licence terms and drop retracted papers
before they reach your corpus.
