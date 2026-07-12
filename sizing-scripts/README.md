# Sizing scripts have moved → [wiz-sizing](https://github.com/adilio/wiz-sizing)

All Wiz sizing tooling now lives in its own repository:

**https://github.com/adilio/wiz-sizing**

That repo ships one curl-able script per cloud (`wiz-azure.sh`, `wiz-aws.sh`,
`wiz-gcp.sh`), plus `wiz-code.sh` for repo/developer counting and
`wiz-365.ps1` for Microsoft 365 — no install, no modules, run straight from a
cloud shell.

The official and field-hardened source scripts that used to live in this
directory (including their provenance ledger, `SCRIPT_STATUS.md`) are
preserved verbatim at
[`wiz-sizing/reference/`](https://github.com/adilio/wiz-sizing/tree/main/reference),
and remain in this repo's git history.

Nothing else in `wiz-tools` moved — the
[SHI Report Viewer](../wiz-shi-report-viewer/) and the published
[docs pages](../docs/) are unchanged.
