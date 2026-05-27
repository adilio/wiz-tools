#Requires -Version 7.0
#Requires -Modules Microsoft.Graph.Authentication, Microsoft.Graph.Applications, Microsoft.Graph.Identity.DirectoryManagement

[CmdletBinding()]
param(
    [string]$AppName = "Wiz-M365-Temp-Scanner-$([guid]::NewGuid().ToString('N').Substring(0, 8))",

    [switch]$KeepTemporaryApp,

    [ValidateRange(1, 10)]
    [int]$MaxRetries = 5,

    [ValidateRange(1, 300)]
    [int]$PermissionPropagationSeconds = 20,

    [ValidateRange(1, 1000)]
    [int]$ProgressInterval = 25,

    [bool]$UseDeviceCode = $true
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "Continue"
$script:UseAnsi = -not [string]::IsNullOrEmpty($PSStyle.Reset) -and -not $env:NO_COLOR
$script:RunStartedAt = [datetime]::UtcNow

function Format-ConsoleText {
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)]
        [string]$Text,

        [string]$Style
    )

    if (-not $script:UseAnsi -or [string]::IsNullOrEmpty($Style)) {
        return $Text
    }

    return "$Style$Text$($PSStyle.Reset)"
}

function Format-ElapsedTime {
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)]
        [timespan]$Elapsed
    )

    if ($Elapsed.TotalHours -ge 1) {
        return "{0:00}:{1:00}:{2:00}" -f [int]$Elapsed.TotalHours, $Elapsed.Minutes, $Elapsed.Seconds
    }

    return "{0:00}:{1:00}" -f $Elapsed.Minutes, $Elapsed.Seconds
}

function Write-Section {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Title
    )

    $line = "-" * 64
    Write-Information "" -InformationAction Continue
    Write-Information (Format-ConsoleText $line $PSStyle.Foreground.BrightBlack) -InformationAction Continue
    Write-Information (Format-ConsoleText " $Title" $PSStyle.Foreground.Cyan) -InformationAction Continue
    Write-Information (Format-ConsoleText $line $PSStyle.Foreground.BrightBlack) -InformationAction Continue
}

function Write-Status {
    [CmdletBinding()]
    param(
        [ValidateSet("INFO", "OK", "WAIT", "WARN", "SCAN", "DONE")]
        [string]$Level = "INFO",

        [Parameter(Mandatory)]
        [string]$Message
    )

    $style = switch ($Level) {
        "OK" { $PSStyle.Foreground.Green }
        "WAIT" { $PSStyle.Foreground.Yellow }
        "WARN" { $PSStyle.Foreground.Yellow }
        "SCAN" { $PSStyle.Foreground.Cyan }
        "DONE" { $PSStyle.Foreground.Green }
        default { $PSStyle.Foreground.BrightBlack }
    }

    $elapsed = Format-ElapsedTime ([datetime]::UtcNow - $script:RunStartedAt)
    $prefix = Format-ConsoleText ("[{0}]" -f $Level.PadRight(4)) $style
    $time = Format-ConsoleText ("+{0}" -f $elapsed) $PSStyle.Foreground.BrightBlack
    Write-Information "$time $prefix $Message" -InformationAction Continue
}

function Connect-RequiredGraph {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]]$Scopes,

        [bool]$UseDeviceCode
    )

    if ($UseDeviceCode) {
        Connect-MgGraph -Scopes $Scopes -UseDeviceCode -ErrorAction Stop
        return
    }

    Connect-MgGraph -Scopes $Scopes -ErrorAction Stop
}

function Get-GraphToken {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)]
        [string]$TenantId,

        [Parameter(Mandatory)]
        [string]$ClientId,

        [Parameter(Mandatory)]
        [string]$ClientSecret
    )

    $tokenBody = @{
        client_id     = $ClientId
        client_secret = $ClientSecret
        scope         = "https://graph.microsoft.com/.default"
        grant_type    = "client_credentials"
    }

    $token = Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$TenantId/oauth2/v2.0/token" -Body $tokenBody

    [PSCustomObject]@{
        AccessToken = $token.access_token
        ExpiresAt   = [datetime]::UtcNow.AddSeconds([int]$token.expires_in)
    }
}

function Get-ValidToken {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$TokenData,

        [Parameter(Mandatory)]
        [string]$TenantId,

        [Parameter(Mandatory)]
        [string]$ClientId,

        [Parameter(Mandatory)]
        [string]$ClientSecret
    )

    if ([datetime]::UtcNow -ge $TokenData.ExpiresAt.AddMinutes(-5)) {
        Write-Status -Level "INFO" -Message "Access token is near expiry. Requesting a fresh Graph token."
        return Get-GraphToken -TenantId $TenantId -ClientId $ClientId -ClientSecret $ClientSecret
    }

    return $TokenData
}

function Get-RetryAfterDelay {
    [CmdletBinding()]
    [OutputType([int])]
    param(
        $Response
    )

    $retryAfterValues = $null
    if (-not $Response -or -not $Response.Headers.TryGetValues("Retry-After", [ref]$retryAfterValues)) {
        return 10
    }

    $retryAfterValue = @($retryAfterValues)[0]
    if ($retryAfterValue -as [int]) {
        return [int]$retryAfterValue
    }

    $retryAfterDate = $retryAfterValue -as [datetime]
    if ($retryAfterDate) {
        return [Math]::Max(1, [int]($retryAfterDate.ToUniversalTime() - [datetime]::UtcNow).TotalSeconds)
    }

    return 10
}

function Invoke-GraphCollectionRequest {
    [CmdletBinding()]
    [OutputType([System.Collections.Generic.List[object]])]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseOutputTypeCorrectly", "", Justification = "Returns a generic list as a single collection object to avoid PowerShell pipeline enumeration.")]
    param(
        [Parameter(Mandatory)]
        [string]$Uri,

        [Parameter(Mandatory)]
        [string]$AccessToken,

        [Parameter(Mandatory)]
        [ValidateRange(1, 10)]
        [int]$MaxRetries,

        [string]$Activity = "Microsoft Graph request",

        [ValidateRange(1, 100000)]
        [int]$ProgressInterval = 500
    )

    $headers = @{
        Authorization = "Bearer $AccessToken"
        "Content-Type" = "application/json"
    }

    $results = [System.Collections.Generic.List[object]]::new()
    $nextLink = $Uri
    $pageCount = 0
    $lastProgressUpdate = [datetime]::UtcNow

    while ($nextLink) {
        $attempt = 0

        while ($true) {
            try {
                $response = Invoke-RestMethod -Method Get -Uri $nextLink -Headers $headers -ErrorAction Stop

                if ($null -ne $response.value) {
                    foreach ($item in @($response.value)) {
                        $results.Add($item)
                    }
                }

                $nextLink = $response.'@odata.nextLink'
                $pageCount++

                if ($results.Count -gt 0 -and (
                        $results.Count % $ProgressInterval -eq 0 -or
                        ([datetime]::UtcNow - $lastProgressUpdate).TotalSeconds -ge 30
                    )) {
                    Write-Progress -Activity $Activity -Status "$($results.Count) items fetched across $pageCount page(s)"
                    Write-Status -Level "SCAN" -Message "$Activity - $($results.Count) items fetched across $pageCount page(s)."
                    $lastProgressUpdate = [datetime]::UtcNow
                }

                break
            }
            catch {
                $attempt++
                $statusCode = if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                    [int]$_.Exception.Response.StatusCode
                }
                else {
                    $null
                }

                if ($statusCode -eq 404) {
                    return , $results
                }

                if ($attempt -ge $MaxRetries) {
                    throw "Graph request failed after $attempt attempts. Status: $($statusCode ?? 'No HTTP response'). Uri: $nextLink. Error: $($_.Exception.Message)"
                }

                if ($statusCode -in 429, 503) {
                    $sleepSeconds = Get-RetryAfterDelay -Response $_.Exception.Response
                    Write-Status -Level "WAIT" -Message "Graph throttled or unavailable. Retrying in $sleepSeconds seconds."
                    Start-Sleep -Seconds $sleepSeconds
                    continue
                }

                Write-Status -Level "WAIT" -Message "Graph API error ($($statusCode ?? 'No HTTP response')). Retrying in 5 seconds."
                Start-Sleep -Seconds 5
            }
        }
    }

    Write-Progress -Activity $Activity -Completed
    return , $results
}

function Write-SiteScanProgress {
    [CmdletBinding()]
    [OutputType([datetime])]
    param(
        [Parameter(Mandatory)]
        [int]$Current,

        [Parameter(Mandatory)]
        [int]$Total,

        [Parameter(Mandatory)]
        [int]$DriveCount,

        [Parameter(Mandatory)]
        [int]$ProgressInterval,

        [Parameter(Mandatory)]
        [datetime]$LastUpdate
    )

    $percentComplete = if ($Total -gt 0) { [Math]::Min(100, [int](($Current / $Total) * 100)) } else { 0 }
    Write-Progress -Activity "Scanning site drives" -Status "$Current / $Total sites processed; $DriveCount unique drives found" -PercentComplete $percentComplete

    if ($Current -eq 1 -or $Current -eq $Total -or $Current % $ProgressInterval -eq 0 -or ([datetime]::UtcNow - $LastUpdate).TotalSeconds -ge 30) {
        Write-Status -Level "SCAN" -Message "$Current / $Total sites processed; $DriveCount unique drives found."
        return [datetime]::UtcNow
    }

    return $LastUpdate
}

$redirectUri = "https://admin.microsoft.com"
$appObjectId = $null
$appClientId = $null
$appSecret = $null

try {
    Write-Section "Wiz Microsoft 365 Sizing"
    Write-Status -Level "INFO" -Message "Starting scan setup in tenant context."

    $requiredScopes = @("Application.ReadWrite.All", "Directory.Read.All")
    $context = Get-MgContext
    if (-not $context) {
        Write-Status -Level "INFO" -Message "Connecting to Microsoft Graph."
        Connect-RequiredGraph -Scopes $requiredScopes -UseDeviceCode:$UseDeviceCode
    }
    else {
        $missingScopes = $requiredScopes | Where-Object { $_ -notin $context.Scopes }
        if ($missingScopes) {
            Write-Status -Level "INFO" -Message "Reconnecting to Microsoft Graph with required scopes: $($missingScopes -join ', ')."
            Connect-RequiredGraph -Scopes $requiredScopes -UseDeviceCode:$UseDeviceCode
        }
        else {
            Write-Status -Level "OK" -Message "Microsoft Graph context already has the required delegated scopes."
        }
    }

    $tenantInfo = Get-MgOrganization
    $appTenantId = $tenantInfo.Id

    Write-Status -Level "INFO" -Message "Creating temporary app registration: $AppName"

    $app = New-MgApplication -DisplayName $AppName -RequiredResourceAccess @{
        ResourceAppId  = "00000003-0000-0000-c000-000000000000"
        ResourceAccess = @(
            @{ Id = "332a536c-c7ef-4017-ab91-336970924f0d"; Type = "Role" }  # Sites.Read.All
            @{ Id = "01d4889c-1287-42c6-ac1f-5d1e02578ef6"; Type = "Role" }  # Files.Read.All
            @{ Id = "df021288-bdef-4463-88db-98f22de89214"; Type = "Role" }  # User.Read.All
        )
    } -Web @{ RedirectUris = @($redirectUri) }

    $appClientId = $app.AppId
    $appObjectId = $app.Id
    Write-Status -Level "OK" -Message "Temporary app created. Client ID: $appClientId"

    Write-Status -Level "WAIT" -Message "Waiting 5 seconds for the service principal to become available."
    Start-Sleep -Seconds 5
    New-MgServicePrincipal -AppId $appClientId | Out-Null
    Write-Status -Level "OK" -Message "Service principal is ready."

    $cred = Add-MgApplicationPassword -ApplicationId $appObjectId -PasswordCredential @{ DisplayName = "Wiz M365 sizing scan secret" }
    $appSecret = $cred.SecretText
    $consentUrl = "https://login.microsoftonline.com/$appTenantId/adminconsent?client_id=$appClientId&redirect_uri=$redirectUri"

    Write-Section "Admin Consent"
    Write-Status -Level "WAIT" -Message "Open the admin consent link, accept permissions, then return here."
    Write-Information (Format-ConsoleText $consentUrl $PSStyle.Foreground.Cyan) -InformationAction Continue
    Write-Status -Level "INFO" -Message "After accepting, the browser redirects to Microsoft 365 admin center. Close it and return here."
    Write-Status -Level "INFO" -Message "Cloud Shell tip: Ctrl/Cmd-click the link if it does not open automatically."
    Read-Host "Press ENTER after acceptance"

    Write-Status -Level "WAIT" -Message "Waiting $PermissionPropagationSeconds seconds for permissions to propagate."
    Start-Sleep -Seconds $PermissionPropagationSeconds

    Write-Status -Level "INFO" -Message "Authenticating with app-only Graph permissions."
    $tokenData = Get-GraphToken -TenantId $appTenantId -ClientId $appClientId -ClientSecret $appSecret
    Write-Status -Level "OK" -Message "Graph app-only token acquired."

    Write-Section "Scanning"
    Write-Status -Level "SCAN" -Message "Collecting users, sites, and unique drives."

    $tokenData = Get-ValidToken -TokenData $tokenData -TenantId $appTenantId -ClientId $appClientId -ClientSecret $appSecret
    $allUsers = Invoke-GraphCollectionRequest -Uri "https://graph.microsoft.com/v1.0/users?`$select=id,assignedLicenses" -AccessToken $tokenData.AccessToken -MaxRetries $MaxRetries -Activity "Fetching users" -ProgressInterval 1000

    $m365F1SkuId = "44575883-256e-4a79-9da4-ebe9acabe2b2"
    $licensedUserCount = 0
    foreach ($user in $allUsers) {
        if ($user.assignedLicenses.skuId -notcontains $m365F1SkuId) {
            $licensedUserCount++
        }
    }
    Write-Status -Level "OK" -Message "$licensedUserCount users counted after excluding Microsoft 365 F1."

    $tokenData = Get-ValidToken -TokenData $tokenData -TenantId $appTenantId -ClientId $appClientId -ClientSecret $appSecret
    $allSites = Invoke-GraphCollectionRequest -Uri "https://graph.microsoft.com/v1.0/sites?`$select=id" -AccessToken $tokenData.AccessToken -MaxRetries $MaxRetries -Activity "Fetching sites" -ProgressInterval 500
    Write-Status -Level "OK" -Message "$($allSites.Count) sites found. Scanning drives next."

    $processedDriveIds = [System.Collections.Generic.HashSet[string]]::new()
    $siteCount = $allSites.Count
    $siteIndex = 0
    $lastSiteProgressUpdate = [datetime]::UtcNow
    $excludedDriveNames = [System.Collections.Generic.HashSet[string]]::new([string[]]@("PersonalCacheLibrary", "Preservation Hold Library"))

    foreach ($site in $allSites) {
        if (-not $site.id) {
            continue
        }

        $siteIndex++
        $tokenData = Get-ValidToken -TokenData $tokenData -TenantId $appTenantId -ClientId $appClientId -ClientSecret $appSecret
        $encodedSiteId = [uri]::EscapeDataString($site.id)
        $drivesUri = "https://graph.microsoft.com/v1.0/sites/$encodedSiteId/drives?`$select=id,name,driveType,webUrl"
        $drives = Invoke-GraphCollectionRequest -Uri $drivesUri -AccessToken $tokenData.AccessToken -MaxRetries $MaxRetries -Activity "Fetching drives for site $siteIndex of $siteCount" -ProgressInterval 100

        foreach ($drive in $drives) {
            if ($excludedDriveNames.Contains($drive.name)) {
                continue
            }

            [void]$processedDriveIds.Add($drive.id)
        }

        $lastSiteProgressUpdate = Write-SiteScanProgress -Current $siteIndex -Total $siteCount -DriveCount $processedDriveIds.Count -ProgressInterval $ProgressInterval -LastUpdate $lastSiteProgressUpdate
    }
    Write-Progress -Activity "Scanning site drives" -Completed

    Write-Information "`n===================================" -InformationAction Continue
    Write-Information "           FINAL COUNTS            " -InformationAction Continue
    Write-Information "===================================" -InformationAction Continue
    Write-Information " Total Users Found : $licensedUserCount" -InformationAction Continue
    Write-Information " Total Drives Found: $($processedDriveIds.Count)" -InformationAction Continue
    Write-Information "===================================" -InformationAction Continue
}
finally {
    if ($appObjectId -and -not $KeepTemporaryApp) {
        Write-Information "`n[Cleanup] Removing temporary app ($AppName)..." -InformationAction Continue

        try {
            Remove-MgApplication -ApplicationId $appObjectId -ErrorAction Stop
            Write-Information "[Cleanup] App deleted successfully." -InformationAction Continue
        }
        catch {
            Write-Warning "[Cleanup] Could not delete app automatically. Please remove '$AppName' manually from Azure Portal."
        }
    }
    elseif ($appObjectId -and $KeepTemporaryApp) {
        Write-Warning "[Cleanup] Keeping temporary app because -KeepTemporaryApp was specified: $AppName"
    }
}
