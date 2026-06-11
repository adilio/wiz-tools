# Wiz Sizing Scripts

This directory collects Wiz resource-discovery and sizing scripts by product area.

The scripts estimate billable units before a Wiz connector is enabled. After a connector is running, the Wiz **Settings > Licenses** page is the better source of truth.

Use [SCRIPT_STATUS.md](SCRIPT_STATUS.md) to see which scripts are straight Wiz-hosted copies and which ones have local field-hardening changes.

## Layout

| Area | Directory | Purpose |
|---|---|---|
| Code | [code/](code/) | Count active developers across version control systems and HCP Terraform. |
| Cloud | [cloud/](cloud/) | Count cloud, SaaS, data, registry, and infrastructure resources. |
| SaaS | [saas/](saas/) | SaaS-specific sizing scripts, including Microsoft 365. |

## Canonical vs Compatibility Paths

All sizing scripts live canonically under this directory. The Microsoft 365 script lives at [saas/microsoft-365/365_Sizing_Script.ps1](saas/microsoft-365/365_Sizing_Script.ps1).

The published GitHub Pages URL `docs/m365-sizing-xl/` remains as a compatibility documentation page, but there is no separate root-level source folder for Microsoft 365.

## Instruction Gists

Gists are instruction pages only. The script copies live in this repository for now.

Existing instruction gists were found for the cloud/provider scripts, HCP Terraform, and Microsoft 365. Missing Code instruction gists were created as secret gists:

| Script | Gist |
|---|---|
| Azure DevOps active developers | https://gist.github.com/adilio/34bee443582f724a88a953f2b65e1695 |
| GitHub active developers | https://gist.github.com/adilio/dcf9c6d8158f4bda24a5496e026bfa4d |
| GitLab active developers | https://gist.github.com/adilio/f2d0db7713f20fbec8e77b520d1ff0cf |

Existing gists found in the account:

| Script | Existing gist |
|---|---|
| Alibaba Cloud | https://gist.github.com/adilio/1ed5885d2e2834556c9be9c6daa6ba99 |
| AWS | https://gist.github.com/adilio/187c41935225d75100b5384eb814ec06 |
| AWS ASM | https://gist.github.com/adilio/1e0bb1617cc4eec44906b463b931daf6 |
| Azure | https://gist.github.com/adilio/e06c498f41bcc0ff4af76d1e0b3510cf |
| GCP | https://gist.github.com/adilio/f33a503a0d34d55441c2c87828bb5c66 |
| HCP Terraform | https://gist.github.com/adilio/172d8118782d96e4487ba7be70dd8cc0 |
| Linode | https://gist.github.com/adilio/8c4bc439955cff9533748bf9c0412080 |
| Microsoft 365 | https://gist.github.com/adilio/d8b512f809d4b872fd1b7dc17570f849 |
| OCI | https://gist.github.com/adilio/0b8fe0449f3a0b76ed3291c7b9303b23 |
| Snowflake | https://gist.github.com/adilio/7324ec1a7f59c211aae7593af977cc46 |
| VMware vSphere | https://gist.github.com/adilio/f846ba58dbe9d1497ca192d029d6c4f2 |

## Notes

- Review each script before running it in a production tenant or account.
- Some scripts create local output files containing contributor or resource details. Do not share those files unless you have reviewed the data.
