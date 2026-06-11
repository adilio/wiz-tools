# Wiz Code Sizing Scripts

These scripts estimate active developers for Wiz Code sizing by scanning commits in the last 90 days.

| Platform | Script | Original download |
|---|---|---|
| Azure DevOps | [azure-devops/active-developer-count-ado.py](azure-devops/active-developer-count-ado.py) | https://downloads.wiz.io/customer-files/scripts/ADO/active-developer-count-ado.py |
| GitHub | [github/active-developer-count-github.py](github/active-developer-count-github.py) | https://downloads.wiz.io/customer-files/scripts/GitHub/active-developer-count-github.py |
| GitLab | [gitlab/active-developer-count-gitlab.py](gitlab/active-developer-count-gitlab.py) | https://downloads.wiz.io/customer-files/scripts/GitLab/active-developer-count-gitlab.py |
| HCP Terraform | [hcp-terraform/active-developer-count-hcp.py](hcp-terraform/active-developer-count-hcp.py) | https://downloads.wiz.io/customer-files/scripts/HCP/active-developer-count-hcp.py |

If you run more than one active-developer script in the same directory, the scripts use their output files to deduplicate contributors across systems.

## Azure DevOps Long-Running Scans

The Azure DevOps script in this repo includes operational hardening for large organizations:

- Scans only the repositories in the current project instead of rescanning previously discovered repositories under later projects.
- Paginates project and commit queries with `--project-page-size` and `--commit-page-size`.
- Prints elapsed-time progress with `--progress-interval`.
- Writes checkpoint and partial output on interruption.
- Supports guardrails such as `--max-repositories`, `--max-commits-per-repo`, and `--max-run-minutes`.
- Retries Azure DevOps API calls with `--max-retries` and `--retry-delay`.

Examples:

```shell
# Pilot one project first
python3 active-developer-count-ado.py --token ${ADO_TOKEN} --org EXAMPLE --proj EXAMPLE_PROJECT

# Standard org run, same shape as the original command
python3 active-developer-count-ado.py --token ${ADO_TOKEN} --org EXAMPLE

# Bound a large org scan while validating behavior
python3 active-developer-count-ado.py --token ${ADO_TOKEN} --org EXAMPLE --max-repositories 100 --progress-interval 10

# Cap very large repositories during troubleshooting
python3 active-developer-count-ado.py --token ${ADO_TOKEN} --org EXAMPLE --max-commits-per-repo 50000

# Keep a customer run bounded by wall clock time
python3 active-developer-count-ado.py --token ${ADO_TOKEN} --org EXAMPLE --max-run-minutes 240 --checkpoint-interval 10
```
