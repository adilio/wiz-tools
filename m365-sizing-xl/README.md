# Microsoft 365 Sizing Script

Use this PowerShell script to estimate Wiz billable units for Microsoft 365 SaaS users and virtual drives.

The script is optimized for Azure Cloud Shell and long-running Microsoft Graph scans.

## What It Counts

- Microsoft 365 users, excluding Microsoft 365 F1 users
- Unique SharePoint and OneDrive drives
- System drives such as `PersonalCacheLibrary` and `Preservation Hold Library` are excluded

## Prerequisites

Run the script as an Entra ID admin user with permission to:

- Create app registrations
- Create service principals
- Grant Microsoft Graph application permissions through admin consent

The script creates a temporary Entra ID application with these Microsoft Graph application permissions:

| Permission | Purpose |
|---|---|
| `User.Read.All` | List users and assigned licenses |
| `Sites.Read.All` | List SharePoint and OneDrive sites |
| `Files.Read.All` | List drives under sites |

The temporary application is automatically removed when the script finishes.

## Run from Azure Cloud Shell

1. Log in to the Microsoft 365 Admin Portal.
2. Open Azure Cloud Shell.
3. Upload, clone, or download `365_Sizing_Script.ps1`.
4. Run the script.

```powershell
./365_Sizing_Script.ps1
```

The script uses device-code authentication by default, which works cleanly in Azure Cloud Shell.

## Download and Run

If you are running the published Wiz-hosted version:

```powershell
curl -fLO http://downloads.wiz.io/customer-files/scripts/M365/365_Sizing_Script.ps1
./365_Sizing_Script.ps1
```

If you are running from this repo:

```powershell
cd m365-sizing-xl
./365_Sizing_Script.ps1
```

## Script Flow

1. Connects to Microsoft Graph with delegated admin permissions.
2. Creates a uniquely named temporary Entra ID application.
3. Creates the service principal and client secret.
4. Prints an admin consent URL.
5. Waits for permission propagation.
6. Uses app-only Microsoft Graph access tokens for the scan.
7. Refreshes app-only access tokens before expiry during long scans.
8. Fetches users, sites, and drives with retry handling for throttling.
9. Prints final counts.
10. Removes the temporary Entra ID application.

## Differences from the Original Script

This version keeps the same high-level discovery workflow as the original Wiz Microsoft 365 sizing script, but adds operational hardening for Azure Cloud Shell and larger tenants.

| Area | Original behavior | Updated behavior | Why it changed |
|---|---|---|---|
| Temporary app naming | Reused an app named `Wiz-M365-Temp-Scanner` if one already existed | Creates a unique temporary app name by default | Avoids accidentally reusing or deleting an unrelated app with the same display name |
| Cleanup | Cleanup ran only after a successful scan path | Cleanup runs from a `finally` block | Ensures the temporary app is removed even if scanning fails midway |
| Authentication UX | Used normal `Connect-MgGraph` sign-in | Uses device-code auth by default | Device-code auth is more reliable in Azure Cloud Shell |
| Access token lifetime | Requested one app-only access token before scanning | Tracks token expiry and reacquires a new app-only token before expiry | Large tenants can scan for more than an hour; this prevents failures from expired access tokens |
| Graph retries | Retried throttling and API errors, then silently stopped after max retries | Retries throttling and transient errors, then throws a clear failure after max retries | Prevents partial counts from looking like successful final results |
| Progress output | Minimal terminal output | Adds clean section headers, elapsed time, `Write-Progress`, and periodic status lines | Gives users confidence during long Cloud Shell runs |
| Performance | Used PowerShell array appends with `+=` inside loops | Uses generic collections and hash sets | Avoids slow array copying in larger tenants |
| Drive scan payload | Requested default drive fields | Uses `$select` for only needed drive fields | Reduces Microsoft Graph response size |
| Final result | Printed the `FINAL COUNTS` block | Keeps the same `FINAL COUNTS` output format | Preserves compatibility with existing runbooks and customer instructions |

## Parameters

| Parameter | Default | Description |
|---|---:|---|
| `-AppName` | Unique generated name | Temporary Entra ID app display name |
| `-KeepTemporaryApp` | `false` | Keep the temporary app after the scan for troubleshooting |
| `-MaxRetries` | `5` | Retry attempts for Microsoft Graph calls |
| `-PermissionPropagationSeconds` | `20` | Wait time after admin consent |
| `-ProgressInterval` | `25` | Number of sites between progress log lines |
| `-UseDeviceCode` | `true` | Use device-code auth for Cloud Shell-friendly sign-in |

Examples:

```powershell
# Default Azure Cloud Shell run
./365_Sizing_Script.ps1

# Reduce progress output for very large tenants
./365_Sizing_Script.ps1 -ProgressInterval 100

# Keep the temporary app if troubleshooting consent or permissions
./365_Sizing_Script.ps1 -KeepTemporaryApp

# Use browser-based Graph authentication instead of device-code auth
./365_Sizing_Script.ps1 -UseDeviceCode:$false
```

## Progress Output

The script prints clean progress updates while it runs:

```text
+00:12 [OK]   Temporary app created. Client ID: ...
+03:41 [SCAN] 250 / 1200 sites processed; 430 unique drives found.
+08:14 [DONE] Sizing scan complete.
```

It also uses `Write-Progress` for Cloud Shell progress bars and periodic terminal log lines so users can tell the scan is still moving during long runs.

## Results

Results are printed to the terminal using the same final output format as the original script:

```text
===================================
           FINAL COUNTS
===================================
 Total Users Found : <count>
 Total Drives Found: <count>
===================================
```

## Cleanup

By default, the temporary Entra ID application is deleted in a `finally` block even if the scan fails.

If cleanup fails, the script prints the app name so it can be removed manually from Entra ID.

## Notes

- Large tenants can take more than an hour to scan. The script reacquires app-only Graph access tokens before they expire.
- Microsoft Graph throttling is expected in large tenants. The script honors `Retry-After` when Graph returns it.
- Review the script before running it in production tenants.
