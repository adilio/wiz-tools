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
| Code | [code/azure-devops/active-developer-count-ado.py](code/azure-devops/active-developer-count-ado.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/ADO/active-developer-count-ado.py | Fixes repeated repository rescans across projects; adds project and commit pagination, retries, optional checkpoints, partial output, and large-org guardrails while keeping the standard command shape. |
| Code | [code/github/active-developer-count-github.py](code/github/active-developer-count-github.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/GitHub/active-developer-count-github.py | No local changes. |
| Code | [code/gitlab/active-developer-count-gitlab.py](code/gitlab/active-developer-count-gitlab.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/GitLab/active-developer-count-gitlab.py | No local changes. |
| Code | [code/hcp-terraform/active-developer-count-hcp.py](code/hcp-terraform/active-developer-count-hcp.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/HCP/active-developer-count-hcp.py | No local changes. |
| Cloud | [cloud/alibaba-cloud/resource-count-ali.py](cloud/alibaba-cloud/resource-count-ali.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Alibaba/resource-count-ali.py | No local changes. |
| Cloud | [cloud/aws/resource-count-aws-v2.py](cloud/aws/resource-count-aws-v2.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/AWS/resource-count-aws-v2.py | No local changes. |
| Cloud | [cloud/aws/asm-resource-count-aws.py](cloud/aws/asm-resource-count-aws.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/AWS/asm-resource-count-aws.py | No local changes. |
| Cloud | [cloud/azure/resource-count-azure-v2.py](cloud/azure/resource-count-azure-v2.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Azure/resource-count-azure-v2.py | No local changes. |
| Cloud | [cloud/gcp/resource-count-gcp-v2.py](cloud/gcp/resource-count-gcp-v2.py) | `modified` | https://downloads.wiz.io/customer-files/scripts/GCP/resource-count-gcp-v2.py | Adds project filters, bounded pagination, request timeout, optional checkpoints, partial output on stop/failure, resume controls, output directories, and Cloud Asset Inventory fallback guidance. |
| Cloud | [cloud/linode/resource-count-linode.py](cloud/linode/resource-count-linode.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Linode/resource-count-linode.py | No local changes. |
| Cloud | [cloud/oci/resource-count-oci.py](cloud/oci/resource-count-oci.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/OCI/resource-count-oci.py | No local changes. |
| Cloud | [cloud/snowflake/resource_count_snowflake.py](cloud/snowflake/resource_count_snowflake.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Snowflake/resource_count_snowflake.py | No local changes. |
| Cloud | [cloud/snowflake/resource_count_snowflake_worksheet.py](cloud/snowflake/resource_count_snowflake_worksheet.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/Snowflake/resource_count_snowflake_worksheet.py | No local changes. |
| Cloud | [cloud/vmware-vsphere/resource-count-vsphere.py](cloud/vmware-vsphere/resource-count-vsphere.py) | `wiz-copy` | https://downloads.wiz.io/customer-files/scripts/vSphere/resource-count-vsphere.py | No local changes. |
| SaaS | [saas/microsoft-365/365_Sizing_Script.ps1](saas/microsoft-365/365_Sizing_Script.ps1) | `modified` | http://downloads.wiz.io/customer-files/scripts/M365/365_Sizing_Script.ps1 | Field-hardened PowerShell version with unique temp app naming, cleanup, token refresh, retry handling, and progress output. |
