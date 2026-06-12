# Sizing Scripts — Next-Agent Implementation Plan

Scope: bring the 12 `wiz-copy` scripts up to the durability/operator standard of the 3 `modified` scripts (ADO, GCP, M365), and close remaining gaps in the modified scripts. User has authorized implementing all fixes directly (not just upstream suggestions).

Already done (do not re-suggest): everything in `SIZING_CHANGES.md` — ADO repo-rescan fix/pagination/guardrails, GCP filters/resume/checkpoints/paged helper, M365 app cleanup/token refresh/retry.

## Progress Tracker

| ID | Summary | Status |
|----|---------|--------|
| P2 | AWS DocumentDB pagination calls wrong API | ✅ Done |
| P9 | GitLab `--proj` crash + None deref | ✅ Done |
| P8 | AWS Lightsail Linux-sensor detection | ✅ Done |
| P8b | AWS EKS Fargate dead loop | ✅ Done |
| P6 | OCI shared config dict race condition | ✅ Done |
| P6b | OCI regions error path, tag KeyError, N+1 image cache | ✅ Done |
| P7 | Alibaba ACK cluster pagination bugs | ✅ Done |
| P4 | GitHub rate-limit retry crashes | ✅ Done |
| P4b | GitHub days_ago timezone + None-repo exit | ✅ Done |
| P3 | Azure AKS counts only first agent pool | ✅ Done |
| P10 | Thread-safe totals (AWS std + Azure) | ✅ Done |
| P10b | Azure unbound vars on list failure | ✅ Done |
| P1 | Log worker-thread exceptions with context | ✅ Done |
| P16 | Snowflake account parsing + identifier quoting | ✅ Done |
| P5 | HCP Terraform infinite loop + crash + overcount | ✅ Done |
| P17a | ADO --fail-fast partial output (local patch) | ✅ Done |
| P17b | GCP image cache (local patch) | ✅ Done |
| P18a | Linode/vSphere f-string + detail log fixes | ✅ Done |
| P18b | vSphere --cluster optional | ✅ Done |
| P11 | Partial output on Ctrl-C / failure | ⬜ Todo (next) |
| P12 | Elapsed-time progress status lines | ⬜ Todo |
| P13 | --output-dir everywhere | ⬜ Todo |
| P14 | Scoping/runtime caps/resume for org-scale | ⬜ Todo |
| P15 | Retry/timeout gaps (Azure, OCI, Snowflake) | ⬜ Todo |

## Session Resume Notes (2026-06-12)

All P1–P10, P16–P18 fixes have been implemented and pass `py_compile`. Remaining work is P11–P15 (larger operator-quality features). Start next session with P11 (partial output / Ctrl-C), then P12 (elapsed-time progress), P13 (--output-dir), P15 (retry timeouts), P14 (scoping/resume).

## Next-Agent Implementation Plan

### P1. Log worker-thread exceptions instead of silently counting them — 8 scripts
- **Scripts:** `cloud/aws/resource-count-aws-v2.py`, `cloud/aws/asm-resource-count-aws.py`, `cloud/azure/resource-count-azure-v2.py`, `cloud/oci/resource-count-oci.py`, `cloud/alibaba-cloud/resource-count-ali.py`, `code/github/active-developer-count-github.py`, `code/gitlab/active-developer-count-gitlab.py`, plus the (cosmetic) same pattern in linode/vsphere.
- **Where:** every `get_*_resources()` / `main()` tail loop of the form `for future in as_completed(futures): if future.exception(): exceptions += 1` (e.g. aws-v2 `get_aws_resources()` lines 1251–1253, azure `get_azure_resources()` lines 1006–1008, github `main()` lines 510–512).
- **Change:** submit futures into a dict keyed by a task label and call `error_print(future.exception(), f"{account/subscription} task={label}")` on failure; print an end-of-run failed-task count.
- **Copy from:** GCP `get_gcp_resources()` lines 1033–1066 (`futures[executor.submit(...)] = 'Cloud SQL instances'` + `error_print(future.exception(), f"{project_id} task={...}")`).
- **Why:** today a thread that raises (several known crash paths below) silently zeroes an entire resource family — the #1 silent-undercount mechanism in the repo.
- **User-facing:** errors appear on console and in `*-errors-log.txt` with account + task context; final summary says how many tasks failed.
- **Validate:** temporarily raise inside one resource function; confirm the error is printed/logged and the run completes.
- **Disposition:** upstream.

### P2. AWS standard: DocumentDB pagination calls the wrong API
- **Script:** `cloud/aws/resource-count-aws-v2.py`, `get_aws_docdb_clusters()` line 1036.
- **Change:** `client.describe_db_instances(Marker=...)` → `client.describe_db_clusters(Marker=...)`. As written, page 2+ fetches DB *instances* then reads `response['DBClusters']` → KeyError → thread dies (silently, see P1) → PaaS Database undercount for accounts with >100 DocumentDB clusters.
- **Copy from:** the correct first-page call 6 lines above.
- **User-facing:** correct DocumentDB counts in large accounts.
- **Validate:** code inspection + `python -m py_compile`; in a live account, mock `Marker` in the first response.
- **Disposition:** upstream. Severity: critical.

### P3. Azure: AKS counts only the first agent pool
- **Script:** `cloud/azure/resource-count-azure-v2.py`, `get_azure_aks_container_instances()` line 587 (`managed_cluster.agent_pool_profiles[0].count`) and the equivalent `--graph` KQL at lines 565–570 (`properties.agentPoolProfiles[0]`).
- **Change:** sum `count` across **all** `agent_pool_profiles` (and `mv-expand properties.agentPoolProfiles` in the KQL). Multi-nodepool clusters (the norm: system + user pools) are undercounted today — Container Hosts and Kubernetes Sensors are both wrong.
- **Copy from:** GCP `get_gcp_gke_clusters()` (iterates all `nodePools`).
- **Validate:** run against a subscription with a 2-pool AKS cluster; count should equal `az aks nodepool list` totals.
- **Disposition:** upstream. Severity: critical.

### P4. GitHub: rate-limit retry decorator crashes when triggered
- **Script:** `code/github/active-developer-count-github.py`, `rate_limited_retry()` lines 190–194.
- **Change:** `datetime.fromtimestamp(...)` is called on the **module** (`import datetime`) → AttributeError the moment a rate limit is hit; the generic `except` re-raises, the repo thread dies, and (P1) the failure is swallowed → silent developer undercount on any large org. Fix: `datetime.datetime.fromtimestamp(int(e.headers['X-RateLimit-Reset']))` (header is a string), clamp wait to `max(wait_seconds, 1)`, and add a small buffer.
- Also in this script: make `days_ago()` timezone-aware (`datetime.datetime.now(datetime.timezone.utc)`) so the 90-day `since=` window isn't shifted by local offset; and fix `main()` lines 487–489 / 496–498 where `if not repository: print("Exiting...")` does **not** exit and then iterates `[None]`.
- **Copy from:** ADO `days_ago_iso()` lines 329–335; ADO `call_ado_api()` retry/backoff shape.
- **Validate:** unit-test the decorator with a fabricated `RateLimitExceededException`; run `--org <big org>` until throttled.
- **Disposition:** upstream. Severity: critical (silent undercount under throttling — exactly the large-tenant case).

### P5. HCP Terraform: infinite loop on persistent API error + crash on missing config version
- **Script:** `code/hcp-terraform/active-developer-count-hcp.py`.
- **Where/what:**
  1. `paginated_api_call()` lines 129–149: on any status other than 200/429 it logs and loops on the **same** `links['next']` forever — a 401/403/500 hangs the script spamming errors. Add a retry cap with backoff, then `break` returning partial `result`. Honor `Retry-After` on 429 instead of fixed `sleep(1)`.
  2. Lines 400–401 / 450–451: `get_configuration_version()` returns `{}` on error, then `configuration_version['id']` → KeyError → whole scan crashes with no output. Guard the `{}` case and `continue`.
  3. `days_ago_iso()` line 92: naive local time string-compared against HCP's UTC `created-at`; use UTC.
  4. Both run-processing blocks (workspace lines 360–405, org lines 410–455) still **count service accounts as developers** — they are detected (`is-service-account`) and cached, but never skipped before `developers[...] = user`. Confirm intent with Wiz; if service accounts shouldn't be billable developers, `continue` after caching. Also factor the two duplicated ~80-line blocks into one `process_run()` helper while editing.
- **Copy from:** ADO `call_ado_api()` (capped retries + backoff), M365 `Get-RetryAfterDelay` (Retry-After parsing).
- **Validate:** run with a revoked token — script must exit with an error instead of hanging; run normally and diff developer counts with/without the service-account skip.
- **Disposition:** upstream. Severity: critical (hang) / high (crash, overcount).

### P6. OCI: shared config dict mutated across threads (wrong-region scans)
- **Script:** `cloud/oci/resource-count-oci.py`, `config_for_region()` lines 200–207.
- **Change:** `region_config = config` aliases the **shared** dict; the three resource functions run in parallel (`get_oci_resources()` lines 354–362), each iterating regions and writing `config['region']` — so a search client can be built against whichever region another thread set last: duplicated or missed regions, i.e. silent miscounts. Fix: `region_config = dict(config)` (copy), and pass region down.
- Also: `get_oci_regions()` line 195 prints "Exiting..." on error but never exits → `UnboundLocalError` on `regions`; add `sys.exit(1)`. Line 244 `instance['defined_tags']['Oracle-Tags']['CreatedBy']` → guard with `.get()` (KeyError kills the region thread). `get_oci_image_operating_system()` is one `get_image` call **per instance** — cache by `imageId` (images are heavily shared) to cut runtime dramatically.
- **Copy from:** GCP per-call client construction (no shared mutable config); GCP `get_disk_image_details` is the same N+1 pattern — see P17 for the shared caching fix.
- **Validate:** `--debug` vs default run on a multi-region tenancy must produce identical counts.
- **Disposition:** upstream. Severity: critical (race), medium (rest).

### P7. Alibaba: ACK cluster pagination drops pages
- **Script:** `cloud/alibaba-cloud/resource-count-ali.py`, `get_ali_cluster_instances()` lines 524–553.
- **Change:** two bugs: (a) `last_page_number = math.floor(total_count / page_size)` should be `math.ceil`, and the loop condition `if next_page_number < last_page_number` should be `<=` — together they skip the final page(s); (b) `request.page_number(next_page_number)` *calls* the attribute instead of assigning it (`request.page_number = next_page_number`) → TypeError → caught → loop ends after page 1. Net effect: any tenant with more than one page of ACK clusters silently undercounts Container Hosts / Kubernetes Sensors.
- **Validate:** tenant with >10 clusters (default page size); count must match console.
- **Disposition:** upstream. Severity: critical for multi-page ACK tenants.

### P8. AWS standard: Lightsail Linux-sensor detection never matches
- **Script:** `cloud/aws/resource-count-aws-v2.py`, `get_aws_lightsail_instances()` lines 569 and 586.
- **Change:** Lightsail instances have no `PlatformDetails` key — the field is `platform` (`LINUX_UNIX`/`WINDOWS`) — so `linux_instances_count` is always 0 for Lightsail → Virtual Machine Sensors undercount. Use `if instance.get('platform') == 'LINUX_UNIX'`.
- Also in this file while editing: `get_aws_eks_instances()` Fargate-profile pagination (lines 720–729) checks `'NextToken' in response` against the stale **list_clusters** response (and the real key is `nextToken` on the profiles response) — the loop is dead code; if "fixed" naively it would double-count because `get_aws_cluster_fargate_pod_count()` already counts all Fargate pods in the cluster. Correct fix: drop the inner while-loop and paginate `list_fargate_profiles` only to decide *whether* profiles exist.
- **Validate:** account with Lightsail Linux instances; sensor count > 0.
- **Disposition:** upstream. Severity: high (silent sensor undercount).

### P9. GitLab: `--proj` runs crash at summary; None-group deref
- **Script:** `code/gitlab/active-developer-count-gitlab.py`.
- **Change:** `output_results()` line 473 uses `args.pro` (no such attr) — every `--proj` run throws AttributeError after scanning, losing the console summary and the cross-VCS rollup. Fix to `args.proj`. Also `main()` lines 509–511 and 515–517: `if not group/project: print("Exiting...")` without `sys.exit(1)` → `None.projects` AttributeError.
- **Validate:** `--proj <name>` end-to-end; bogus `--group` exits cleanly.
- **Disposition:** upstream. Severity: high (output loss), trivially fixed.

### P10. Thread-safe totals in AWS standard and Azure
- **Scripts:** `cloud/aws/resource-count-aws-v2.py`, `cloud/azure/resource-count-azure-v2.py`.
- **Change:** worker threads do `totals['X'] += n` and `totals_log.append(...)` unlocked (`+=` on a dict entry is not atomic). Add an `add_total()` with a `threading.Lock` and a `log_lock`, replacing all direct mutations.
- **Copy from:** GCP `add_total()` lines 346–349 + `log_lock`; ASM script already does this (`increment_total()`, line 382) — reuse its shape for AWS standard.
- Azure additionally: `get_azure_vms_scale_sets()` line 505 and `get_azure_acr_images()` line 777 leave `scale_sets`/`registries` **unbound** when the list call raises → NameError in thread (silent zero, see P1). Initialize to `[]` before the `try`.
- **Validate:** repeated `--all` runs produce identical totals; inject an auth error for one subscription and confirm logged-not-lost.
- **Disposition:** upstream. Severity: high.

### P11. Partial output on Ctrl-C / failure — all 12 wiz-copy scripts
- **Scripts:** all 12; highest value in AWS (both), Azure, OCI (org-wide scans that run hours).
- **Where:** every `signal_handler()` is `print("\nExiting"); sys.exit(0)` — a 5-hour scan interrupted at hour 4 writes nothing.
- **Change:** make `output_results()` callable with `partial=True`, track `last_accounts`/`last_subscriptions` as the loop progresses, call it from the signal handler and from a `try/except` around the main loop. Optional `--checkpoint-interval` (write partial output every N accounts/subscriptions/repos) for the org-scale scripts.
- **Copy from:** GCP `signal_handler()` lines 291–295 + `main()` lines 1198–1221 (last_projects, checkpoint, except→partial→raise); ADO equivalent lines 241–245, 693–694; M365 `PARTIAL COUNTS BEFORE FAILURE` block for the console-style scripts.
- **User-facing:** Ctrl-C or a crash prints/writes "Partial results across N of M accounts" with everything collected so far.
- **Validate:** start an `--all` scan, Ctrl-C mid-way, confirm CSVs exist and summary says partial.
- **Disposition:** upstream. (Priority 3 in the rubric, but it's the single biggest field-pain fix; do immediately after the correctness items.)

### P12. Elapsed-time progress and scan-phase status — all 12
- **Change:** add `run_started_at = time.monotonic()`, `elapsed_time()`, `status_print()` and emit `+MM:SS [SCAN] Scanning account 7/52 ...` per account/subscription/compartment/project, plus a `[DONE]` line with task/exception counts. For the per-repo code scripts, print every N repos via `--progress-interval`.
- **Copy from:** GCP `elapsed_time()`/`status_print()` lines 298–311 and `[SCAN]/[DONE]` lines 995, 1066; ADO `--progress-interval` lines 685–686.
- **Why:** operators currently cannot distinguish slow from hung — the most common field complaint.
- **Validate:** visual; run any scan and confirm `+MM:SS` prefixes.
- **Disposition:** upstream.

### P13. `--output-dir` everywhere; enable the commented-out log files
- **Scripts:** all 12. Linode and vSphere additionally have their detail-log writers commented out (`output_results()` linode lines 364–369, vsphere lines 285–290) — uncomment them.
- **Change:** add `--output-dir` (default `.`), `os.makedirs(exist_ok=True)`, route all file writes through an `output_path()` helper. Keep the cross-VCS `*-developers.txt` aggregation reading from the same directory (GitHub/GitLab `output_results_across_version_control_systems()` currently hardcodes `os.listdir()` of cwd — ADO already takes the dir-aware version).
- **Copy from:** GCP `output_path()` line 352; ADO lines 284–286 and 348–366.
- **Disposition:** upstream.

### P14. Scoping, runtime caps, and resume for the org-scale cloud scripts
- **Scripts:** AWS (both), Azure, OCI; lighter version for Alibaba and Snowflake.
- **Change:** add `--max-run-minutes` (check between accounts, write partial output and exit), `--max-accounts/--max-subscriptions/--max-compartments`, `--include/--exclude-…-regex` against ID+name, and `--start-after-…` against the sorted account list. AWS/Azure already accept input files (accounts.txt/subscriptions.txt), so resume can also be documented as "edit the file", but the flags make pilots one-liners.
- **Copy from:** GCP args lines 117–161, filter fn `project_matches_filters()` lines 395–402, resume slice in `main()` lines 1189–1196, `max_runtime_reached()` lines 366–370.
- **Validate:** `--max-…-regex` pilot of 3 accounts, then `--start-after` continues from the right place.
- **Disposition:** upstream.

### P15. Retry/timeout gaps in SDK-default scripts
- **Scripts/what:**
  - **Azure:** SDK clients are built with no per-request timeout; long-hung sockets stall a subscription. Pass `retry_total`/`connection_timeout`/`read_timeout` kwargs (azure-core supports them) or wrap with a watchdog; honor the existing comment at lines 346–347.
  - **OCI:** clients use the SDK default of **no retries**. Pass `retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY` when building each client.
  - **Snowflake:** add `login_timeout`/`network_timeout` to `connection_params`; one connection per database (`get_schemas` reconnects every call) — reuse a single connection per account.
  - **HCP:** covered by P5.
  - AWS (both) already use botocore `adaptive` retries — no change.
- **Copy from:** GCP `socket.setdefaulttimeout(args.request_timeout)` + `--request-timeout` flag (lines 104–109, 283).
- **Disposition:** upstream.

### P16. Snowflake: account parsing, identifier quoting, misleading hints
- **Script:** `cloud/snowflake/resource_count_snowflake.py`.
- **Change:**
  1. Line 97 `args.account.split('-')` raises ValueError for accounts whose org/account name contains a hyphen, and line 98's `if not args_organization and args_account` is wrong boolean logic. Use `split('-', 1)` and `if not (args_organization and args_account)`. Same parse repeated in `main()` line 285.
  2. `get_schemas()` line 227 interpolates the database name unquoted — databases created with lower-case/special-character names fail the query and their schemas are silently uncounted (error is logged but count proceeds). Quote as `"{database}"` (escape embedded quotes).
  3. Lines 277/420-style hint "rerun with '--debug'" — this script has no `--debug` flag; drop the text.
  4. Note in instructions that `--warehouse` is effectively required for `ACCOUNT_USAGE` queries; fail fast with a clear message if queries return "no active warehouse".
- **Validate:** account `MY-ORG-ACCT` parses; `create database "lower_case_db"` is counted.
- **Disposition:** upstream. Severity: medium (loud crash / quiet undercount).

### P17. Modified-script local patches (ADO, GCP)
- **ADO** (`code/azure-devops/active-developer-count-ado.py`): with `--fail-fast`, the re-raise inside the `except` at `main()` lines 687–692 escapes `main()` and bypasses partial output (only KeyboardInterrupt is caught at lines 705–708). Wrap `main()` in `try/except Exception` that writes `output_results(..., partial=True)` before re-raising — same shape GCP uses at lines 1217–1219. **Local patch.**
- **GCP** (`cloud/gcp/resource-count-gcp-v2.py`): `get_disk_image_details()` (lines 664–678) issues `disks().get` + `images().get` per boot disk — ~2 API calls per VM. Cache `images().get` results keyed by `(image_project, image_name)` (module-level dict + lock); most fleets share a handful of images. Big-tenant runtime and quota win. **Local patch** (and upstream suggestion).
- **M365**: no defects found; optionally surface the user-count methodology (counts all non-F1 users, including unlicensed) in the final output so SEs can sanity-check. **Local patch, optional.**

### P18. Maintainability sweep (do last, batched per script)
- **Linode + vSphere:** `print("\nResults (script version: {version})\n")` missing `f` prefix (linode line 372, vsphere line 293) — prints the literal braces. Linode: dead `get_linode_lke_instances()` would `KeyError: 'ALT Asset Metadata'` if ever wired up — delete or fix the totals key; `--max-workers` default 100 for 3 tasks → use the shared `DEFAULT_MAX_WORKERS`; Linode `error_print()` has no `errors_log`/error file — add to match siblings; Linode `progress_print` gates on `args.debug_mode` where every other script uses `verbose_mode`.
- **vSphere:** `--cluster` is validated as required (line 105) yet the code fully supports cluster-less scans (the `else` branches) — make it optional so a whole-vCenter count works; destroy container views after use.
- **AWS standard vs ASM vs Azure:** `--max-image-tags` is implemented three different ways (per-image GCP/correct, per-*page* AWS ECR `get_aws_ecr_images()` lines 976/984, per-*repository* Azure `get_azure_acr_images()` line 802). Flag to Wiz upstream as a sizing-methodology inconsistency before changing any code — registry image counts are not comparable across clouds today.
- **AWS both:** `list_versions_by_function(MaxItems=max_lambda_versions)` then filters `$LATEST` → can return max-1 versions; request `MaxItems=max+1`.
- **ASM:** `validate_permissions()` runs only against the management-account credentials but disables resource families globally for all member accounts — note in output, or revalidate per account.
- **HCP:** version 2.5.8 vs 2.8.x elsewhere — confirm against the Wiz-hosted copy that this repo's snapshot is current.
- **Disposition:** upstream notes; trivial f-string fixes are safe local patches if the user wants them.

## Minimal Evidence

**aws/resource-count-aws-v2.py**
- DocumentDB pagination calls `describe_db_instances` then reads `DBClusters` — `get_aws_docdb_clusters()` line 1036 — **critical**.
- Lightsail uses nonexistent `PlatformDetails` (real key `platform`) → sensors never counted — lines 569/586 — **high**.
- `future.exception()` counted, never logged or reported — lines 1251–1253 — **critical** (mechanism that hides every other thread bug).
- `totals[...] += n` from threads without lock — e.g. lines 529–531 — **high**.
- EKS Fargate-profile while-loop checks stale response + wrong key case; if it ran it would double-count pods — lines 720–729 — **medium**.
- ECR caps images per *page* not per image — lines 976/984 — **medium** (methodology).
- Ctrl-C loses all output — `signal_handler()` lines 225–228 — **high**.

**aws/asm-resource-count-aws.py** (already best-hardened wiz-copy)
- Same silent `future.exception()` swallow — lines 1759–1761 — **high**.
- `validate_permissions()` checks only the management account but gates all member accounts — lines 2044–2059 — **medium**.
- No elapsed time, no partial output, no `--output-dir` — **medium**.

**azure/resource-count-azure-v2.py**
- AKS sums only `agent_pool_profiles[0]` (SDK and `--graph` paths) — lines 565–570, 587 — **critical**.
- `scale_sets`/`registries` unbound on list failure → NameError in thread — lines 505/509, 777/781 — **high**.
- Futures swallowed (lines 1006–1008), unlocked totals, exit-on-Ctrl-C — **high**.
- No request timeouts on SDK clients — **medium**.

**oci/resource-count-oci.py**
- `config_for_region()` mutates the shared config dict used concurrently by 3 worker functions — lines 200–207 vs 354–362 — **critical** (region race → miscount).
- `get_oci_regions()` error path doesn't exit → UnboundLocalError — line 195 — **medium**.
- `Oracle-Tags`→`CreatedBy` direct index can KeyError the region thread — line 244 — **medium**.
- `get_image` per instance (N+1) — lines 246, 261–269 — **medium** (performance).
- No SDK retry strategy configured — **medium**.

**alibaba-cloud/resource-count-ali.py**
- ACK pagination: floor instead of ceil, `<` instead of `<=`, and `request.page_number(n)` calls instead of assigns — `get_ali_cluster_instances()` lines 524–553 — **critical** for >1 page of clusters.
- `send_ali_request` passes the request object as the "account" context in `error_print` — line 216 — **low**.

**snowflake/resource_count_snowflake.py**
- `split('-')` unpack + inverted validation boolean — lines 97–101 — **medium**.
- Unquoted DB identifier in `INFORMATION_SCHEMA` query → quiet undercount for quoted names — line 227 — **medium**.
- Hint references nonexistent `--debug` — line 277 — **low**.
- (worksheet variant: no changes needed.)

**github/active-developer-count-github.py**
- `datetime.fromtimestamp` on the module + string timestamp → retry decorator always crashes on throttle; failure then swallowed → silent undercount — lines 190–194 — **critical**.
- Naive `days_ago()` shifts the 90-day window by local UTC offset — lines 138–144 — **medium**.
- `if not repository: print("Exiting...")` without exit → iterates `[None]` — lines 487–489, 496–498 — **medium**.

**gitlab/active-developer-count-gitlab.py**
- `args.pro` typo crashes `output_results()` on every `--proj` run — line 473 — **high**.
- `if not group/project:` prints "Exiting..." but continues into None deref — lines 509–517 — **medium**.
- Members keyed by display name vs committers matched by name → known dup-name miscount risk (documented in-code, lines 386–391) — **medium**, methodology.
- Naive `days_ago()` — **medium**.

**hcp-terraform/active-developer-count-hcp.py**
- `paginated_api_call` loops forever on persistent non-200/429 — lines 129–149 — **critical** (hang).
- `configuration_version['id']` on `{}` after API failure → unhandled KeyError kills the whole scan — lines 400–401, 450–451 — **high**.
- Service accounts detected but still added to `developers` — lines 385–394, 435–445 — **high if unintended** (overcount); confirm with Wiz.
- Two duplicated ~80-line run-processing blocks — lines 360–405 vs 410–455 — **medium**.
- Naive local-time ISO string compared to UTC `created-at` — line 92 — **medium**.

**linode / vmware-vsphere**
- Missing `f` prefix prints `{version}` literally — linode 372, vsphere 293 — **low**.
- Detail log writes commented out — linode 364–369, vsphere 285–290 — **low**.
- Linode dead `get_linode_lke_instances()` targets nonexistent totals key — lines 250–252 — **low**.
- vSphere requires `--cluster` though code supports whole-vCenter scans — lines 105–107 — **medium** ergonomics.

**Modified scripts**
- ADO: `--fail-fast` path escapes `main()` without partial output — lines 687–708 — **medium**, local patch.
- GCP: per-VM `disks().get`+`images().get` with no image cache — lines 664–678 — **medium** perf, local patch.
- M365: none.

## Pattern Library From Modified Scripts

| Pattern | Source (copy from) | Adopt in | Caveats |
|---|---|---|---|
| `elapsed_time()` + `status_print()` `+MM:SS` prefix | GCP 298–311 / ADO 269–281 / M365 `Write-Status` | all 12 | use `time.monotonic()` |
| Signal handler → partial `output_results(partial=True)` | GCP 291–295 + 1198–1221; ADO 241–245 | all 12 | needs `last_*` tracking + snapshot locks (GCP 1075–1079) |
| `--checkpoint-interval` periodic partial writes | GCP/ADO | AWS×2, Azure, OCI, GitHub, GitLab | overwrite same files, don't append |
| `--max-run-minutes` between work units | GCP `max_runtime_reached()` 366–370 | org-scale cloud scripts | check between accounts, not inside a resource fn |
| `--output-dir` + `output_path()` | GCP 352–354, ADO 284–286 | all 12 | keep cross-VCS dedup reading the same dir |
| Include/exclude regex + `--max-N` + `--start-after` resume | GCP 117–161, 395–402, 1189–1196 | AWS×2, Azure, OCI, Alibaba, Snowflake | resume requires deterministic sort of the unit list |
| Retry wrapper with capped exponential backoff + contextual error | ADO `call_ado_api()` 390–401 | GitHub (P4), HCP (P5), Snowflake | AWS keeps botocore adaptive; Azure/OCI prefer SDK-native retry config |
| Futures dict keyed by task label, exceptions logged with context | GCP 1033–1066 | all threaded scripts (P1) | — |
| Locked `add_total()` / log snapshots | GCP 346–349, 1075–1079; ASM `increment_total()` | AWS std, Azure | — |
| Paged-request helper with `--max-pages-per-request` | GCP `execute_paged_request()` 373–392 | AWS std (inline loops ×20), conceptually Alibaba | boto3 alternative: use built-in paginators instead |
| Retry-After-aware throttle handling | M365 `Get-RetryAfterDelay` 174–197 | HCP, GitHub | — |
| Token/session refresh before each major call | M365 `Get-ValidToken` 149–172 | any future OAuth-based script | AWS AssumeRole creds also expire ≥1 h — relevant for very long ASM scans |
| Unique temp resource name + cleanup in `finally` | M365 lines 11, 481–496 | (already done where applicable) | — |
| Email masking option for screen shares | ADO `mask_email()` 299–306 | GitHub, GitLab (emails print in clear) | keep default visible for parity with originals |

## Cross-Cutting Upstream Recommendations

1. **Log thread-task failures with context and report a failure count** (P1) — applies to every parallel script; the current pattern hides all downstream bugs.
2. **Partial output on interrupt/failure + elapsed-time progress + `--output-dir`** (P11–P13) as a standard library block shared across scripts — the three modified scripts prove the shape; ~80% of the code is identical and copy-paste-able.
3. **Org-scale guardrails** (`--max-run-minutes`, scoping regex, resume) for AWS, Azure, OCI (P14).
4. **Sizing-methodology questions for Wiz** (don't code first): registry-image tag capping is inconsistent across AWS/Azure/GCP (P18); HCP counts service-account runs as developers (P5.4); Azure AKS first-pool-only count (P3) is presumably just a bug but changes existing numbers; Snowflake now includes `APPLICATION` databases that an earlier (commented-out) query excluded.
5. **Timezone-aware 90-day windows** in GitHub/GitLab/HCP, matching ADO (P4/P5).
6. Trivial polish batch: f-string version prints, commented-out log files, dead code, `--debug` hints in scripts lacking the flag, HCP version bump (P18).
