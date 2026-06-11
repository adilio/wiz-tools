# Wiz Cloud Sizing Scripts

These scripts estimate cloud and infrastructure billable units before Wiz is connected.

| Platform | Scripts | Original download |
|---|---|---|
| Alibaba Cloud | [alibaba-cloud/resource-count-ali.py](alibaba-cloud/resource-count-ali.py) | https://downloads.wiz.io/customer-files/scripts/Alibaba/resource-count-ali.py |
| AWS | [aws/resource-count-aws-v2.py](aws/resource-count-aws-v2.py), [aws/asm-resource-count-aws.py](aws/asm-resource-count-aws.py) | https://downloads.wiz.io/customer-files/scripts/AWS/resource-count-aws-v2.py |
| Azure | [azure/resource-count-azure-v2.py](azure/resource-count-azure-v2.py) | https://downloads.wiz.io/customer-files/scripts/Azure/resource-count-azure-v2.py |
| GCP | [gcp/resource-count-gcp-v2.py](gcp/resource-count-gcp-v2.py) | https://downloads.wiz.io/customer-files/scripts/GCP/resource-count-gcp-v2.py |
| Linode | [linode/resource-count-linode.py](linode/resource-count-linode.py) | https://downloads.wiz.io/customer-files/scripts/Linode/resource-count-linode.py |
| OCI | [oci/resource-count-oci.py](oci/resource-count-oci.py) | https://downloads.wiz.io/customer-files/scripts/OCI/resource-count-oci.py |
| Snowflake | [snowflake/resource_count_snowflake.py](snowflake/resource_count_snowflake.py), [snowflake/resource_count_snowflake_worksheet.py](snowflake/resource_count_snowflake_worksheet.py) | https://downloads.wiz.io/customer-files/scripts/Snowflake/resource_count_snowflake.py |
| VMware vSphere | [vmware-vsphere/resource-count-vsphere.py](vmware-vsphere/resource-count-vsphere.py) | https://downloads.wiz.io/customer-files/scripts/vSphere/resource-count-vsphere.py |

Many cloud scripts support optional flags such as `--all`, `--data`, or `--images`.

## GCP Large Organization Scans

The GCP script in this repo includes additional guardrails for very large `--all` scans:

- Data resources are only counted when `--data` is supplied.
- API requests use a default socket timeout via `--request-timeout`.
- Paginated calls can be bounded with `--max-pages-per-request`.
- Long scans can be piloted with `--max-projects`, bounded with `--max-run-minutes`, and resumed with `--start-after-project`.
- Project IDs or names can be scoped with `--include-project-regex` and `--exclude-project-regex`.
- Partial CSV output is written on interruption, bounded-run exit, and optional `--checkpoint-interval` checkpoints.
- Output files can be directed to a run folder with `--output-dir`.
- Project and resource failures are logged with context, and the scan continues with remaining projects and resource types where possible.
- `--inventory-instructions` prints a Cloud Asset Inventory fallback outline.

Examples:

```shell
# Pilot the first 25 projects without data or image counting
python3 resource-count-gcp-v2.py --all --max-projects 25

# Skip Apps Script-associated projects if they follow a recognizable naming pattern
python3 resource-count-gcp-v2.py --all --exclude-project-regex '(?i)apps.?script'

# Resume after the last completed project ID printed in the scan
python3 resource-count-gcp-v2.py --all --start-after-project LAST_PROJECT_ID

# Bound a large run and write a partial checkpoint every 25 completed projects
python3 resource-count-gcp-v2.py --all --max-run-minutes 240 --checkpoint-interval 25 --output-dir ./gcp-sizing-run

# Show Cloud Asset Inventory fallback guidance
python3 resource-count-gcp-v2.py --inventory-instructions
```
