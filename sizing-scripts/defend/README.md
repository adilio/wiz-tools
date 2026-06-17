# Wiz Defend Log Volume Discovery Scripts

This directory contains Wiz Defend ingestion log-volume estimation scripts.

These scripts estimate log ingestion volume before Wiz Defend is connected. After Wiz is connected, use the Wiz **Settings > Licenses** page for more accurate license data.

## Scripts

| Platform | Script | Origin | Instruction gist |
|---|---|---|---|
| AWS | [aws/log-volume-estimation-aws.py](aws/log-volume-estimation-aws.py) | https://downloads.wiz.io/customer-files/scripts/AWS/log-volume-estimation-aws.py | https://gist.github.com/adilio/af9f797cf220586bb7d6ae204b7a64b4 |
| Azure | [azure/log-volume-estimation-azure.py](azure/log-volume-estimation-azure.py) | https://downloads.wiz.io/customer-files/scripts/Azure/log-volume-estimation-azure.py | https://gist.github.com/adilio/5fbc7ae00184bd0c44b04888af76cea3 |
| GCP | [gcp/log-volume-estimation-gcp.py](gcp/log-volume-estimation-gcp.py) | https://downloads.wiz.io/customer-files/scripts/GCP/log-volume-estimation-gcp.py | https://gist.github.com/adilio/1cd3922c546c601a0614d7ef62e0dc7f |

## What They Measure

- AWS estimates CloudTrail, VPC Flow Logs, and Route 53 Resolver Query Logs from S3/CloudWatch data, with optional detailed CloudTrail sampling.
- Azure discovers Log Analytics Workspaces receiving supported logs and queries usage volume.
- GCP estimates log volume from Monitoring metrics or directly measures a log sink.

Current status: these are `wiz-copy` scripts downloaded from the Wiz-hosted URLs and organized into the repo without local hardening changes.
