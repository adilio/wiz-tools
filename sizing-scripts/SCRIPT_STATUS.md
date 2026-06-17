# Script Status

This file tracks whether each sizing script is a straight copy of the Wiz-hosted script or has local field-hardening changes.

## Status Values

| Status | Meaning |
|---|---|
| `wiz-copy` | Downloaded from the Wiz-hosted script URL and kept unchanged except for file placement. |
| `modified` | Locally changed in this repository. Review the notes before sharing or running. |
| `compatibility-copy` | Duplicate kept only to preserve older repo paths or raw-download links. |

## Canonical Scripts

| Area | Script | Status | Origin | Notes |
|---|---|---|---|---|
| Code | [code/azure-devops/active-developer-count-ado.py](code/azure-devops/active-developer-count-ado.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/ADO/active-developer-count-ado.py | Fixes repeated repository rescans across projects; adds pre-scan sizing, concurrent repository scans, project and commit pagination, smarter retry handling, optional checkpoints, partial output, and large-org guardrails while keeping the standard command shape. |
| Code | [code/github/active-developer-count-github.py](code/github/active-developer-count-github.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/GitHub/active-developer-count-github.py | Rate-limit retry crash fix; timezone-aware 90-day window; None-repo guard; elapsed progress, partial output, `--output-dir`. |
| Code | [code/gitlab/active-developer-count-gitlab.py](code/gitlab/active-developer-count-gitlab.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/GitLab/active-developer-count-gitlab.py | `--proj` output crash fix (`args.pro` typo); None group/project guard exits; elapsed progress, partial output, `--output-dir`. |
| Code | [code/hcp-terraform/active-developer-count-hcp.py](code/hcp-terraform/active-developer-count-hcp.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/HCP/active-developer-count-hcp.py | Infinite-loop fix on persistent API error; configuration-version KeyError crash fix; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/alibaba-cloud/resource-count-ali.py](cloud/alibaba-cloud/resource-count-ali.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/Alibaba/resource-count-ali.py | ACK cluster pagination fix (ceil, <=, assignment bugs); account scoping/resume flags; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/aws/resource-count-aws-v2.py](cloud/aws/resource-count-aws-v2.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/AWS/resource-count-aws-v2.py | DocumentDB pagination fix; Lightsail sensor detection fix; EKS Fargate fix; thread-safe totals; account scoping/resume flags; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/aws/asm-resource-count-aws.py](cloud/aws/asm-resource-count-aws.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/AWS/asm-resource-count-aws.py | Account scoping/resume flags; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/azure/resource-count-azure-v2.py](cloud/azure/resource-count-azure-v2.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/Azure/resource-count-azure-v2.py | AKS all-agent-pool fix; unbound variable guard; socket timeout; subscription scoping/resume flags; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/gcp/resource-count-gcp-v2.py](cloud/gcp/resource-count-gcp-v2.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/GCP/resource-count-gcp-v2.py | Project filters, bounded pagination, request timeout, optional checkpoints, partial output on stop/failure, resume controls, output directories, Cloud Asset Inventory fallback guidance. |
| Cloud | [cloud/linode/resource-count-linode.py](cloud/linode/resource-count-linode.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/Linode/resource-count-linode.py | f-string version fix; log file uncommented; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/oci/resource-count-oci.py](cloud/oci/resource-count-oci.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/OCI/resource-count-oci.py | Shared config race fix; regions error path fix; tag KeyError guard; image OS cache; SDK retry strategy; compartment scoping/resume flags; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/snowflake/resource_count_snowflake.py](cloud/snowflake/resource_count_snowflake.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/Snowflake/resource_count_snowflake.py | Account name parsing fix; database identifier quoting fix; connection timeout and reuse; account scoping/resume flags; elapsed progress, partial output, `--output-dir`. |
| Cloud | [cloud/snowflake/resource_count_snowflake_worksheet.py](cloud/snowflake/resource_count_snowflake_worksheet.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Snowflake/resource_count_snowflake_worksheet.py | No local changes. Stripped-down Snowpark version for the Snowflake worksheet UI — no CLI or CSV output. |
| Cloud | [cloud/vmware-vsphere/resource-count-vsphere.py](cloud/vmware-vsphere/resource-count-vsphere.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/vSphere/resource-count-vsphere.py | f-string version fix; `--cluster` made optional; log file uncommented; elapsed progress, partial output, `--output-dir`. |
| Defend | [defend/aws/log-volume-estimation-aws.py](defend/aws/log-volume-estimation-aws.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/AWS/log-volume-estimation-aws.py | Estimates AWS Defend log ingestion volume for CloudTrail, VPC Flow Logs, and Route 53 Resolver Query Logs. |
| Defend | [defend/azure/log-volume-estimation-azure.py](defend/azure/log-volume-estimation-azure.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Azure/log-volume-estimation-azure.py | Estimates Azure Defend log ingestion volume from discovered Log Analytics Workspaces. |
| Defend | [defend/gcp/log-volume-estimation-gcp.py](defend/gcp/log-volume-estimation-gcp.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/GCP/log-volume-estimation-gcp.py | Estimates or measures GCP Defend log ingestion volume from Monitoring metrics or log sink metrics. |
| SaaS | [saas/microsoft-365/365_Sizing_Script.ps1](saas/microsoft-365/365_Sizing_Script.ps1) | `modified` | http://downloads.wiz.io/customer-files/scripts/M365/365_Sizing_Script.ps1 | Unique temp app naming, cleanup, device-code auth, token refresh, upfront pre-scan summary, `-SummaryOnly`/`-MaxSites` pilot controls, capped retry waits, per-site drive failure isolation, licensed-user counting excluding Microsoft 365 F1, and progress output. |
