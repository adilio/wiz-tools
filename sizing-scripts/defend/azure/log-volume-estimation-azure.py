#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines

"""
Estimate Azure log volumes for Wiz Defend using diagnostic settings discovery.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
import csv
import inspect
import time
import signal
from functools import wraps

try:
    from azure.identity import ChainedTokenCredential, AzureCliCredential, ManagedIdentityCredential
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.subscription import SubscriptionClient
    from azure.monitor.query import LogsQueryClient
    from azure.core.exceptions import HttpResponseError, ClientAuthenticationError
    import requests
except ImportError:
    print("\nERROR: Missing required Azure SDK packages. Run:")
    print("pip3 install --user --upgrade azure-identity azure-mgmt-resource azure-mgmt-subscription azure-monitor-query requests")
    sys.exit(1)

VERSION = '1.0.4'

####
# Command Line Arguments
####

parser = argparse.ArgumentParser(
    description=f'Azure Log Volume Estimator for Wiz Defend (v{VERSION})'
)

parser.add_argument(
    '--verbose',
    action='store_true',
    dest='verbose',
    help='Enable verbose output during discovery and analysis (default: disabled)',
    default=False
)
parser.add_argument(
    '--debug',
    action='store_true',
    dest='debug',
    help='Enable debug mode (stops on first error) (default: disabled)',
    default=False
)

parser.add_argument(
    '--output-filename',
    dest='output_filename',
    default=f'azure-defend-log-volume-{datetime.now().strftime("%Y%m%d-%H%M%S")}.csv',
    help='Name of the output CSV file.'
)
parser.add_argument(
    '--errors-log-filename',
    dest='errors_log_filename',
    default='azure-defend-errors-log.txt',
    help='Name of the error log file.'
)

estimation_group = parser.add_argument_group('Log Volume Estimation Arguments')

scope_group = estimation_group.add_mutually_exclusive_group(required=True)
scope_group.add_argument(
    '--subscription-id',
    dest='subscription_id',
    help='Azure Subscription ID to analyze.'
)
scope_group.add_argument(
    '--all-subscriptions',
    action='store_true',
    dest='all_subscriptions',
    help='Analyze all accessible subscriptions.'
)

estimation_group.add_argument(
    '--log-analysis-days',
    dest='log_analysis_days',
    type=int,
    default=30,
    help='Number of past days of logs to analyze (default: 30)'
)

args = parser.parse_args()

####
# Configuration and Globals
####

log_records = []
error_warning_counts = {'errors': 0, 'warnings': 0}
inaccessible_sources = set()
padding_desc = 38

# Log types relevant for Wiz Defend
RELEVANT_LOG_TYPES = {
    'AzureActivity': ('Azure Activity Logs', 'Management'),

    'AuditLogs': ('Entra ID Audit Logs', 'Identity'),
    'SignInLogs': ('Entra ID Signin Logs', 'Identity'),
    'AADNonInteractiveUserSignInLogs': ('Entra ID Non-Interactive Signin Logs', 'Identity'),
    'AADServicePrincipalSignInLogs': ('Entra ID Service Principal Signin Logs', 'Identity'),
    'AADManagedIdentitySignInLogs': ('Entra ID Managed Identity Signin Logs', 'Identity'),
    'AADProvisioningLogs': ('Entra ID Provisioning Logs', 'Identity'),
    'AADADFSSignInLogs': ('Entra ID ADFS Signin Logs', 'Identity'),
    'AADRiskyUsers': ('Entra ID Risky Users', 'Identity'),
    'AADUserRiskEvents': ('Entra ID Identity Protection', 'Identity'),
    'AADRiskyServicePrincipals': ('Entra ID Risky Service Principals', 'Identity'),
    'AADServicePrincipalRiskEvents': ('Entra ID Service Principal Risk Events', 'Identity'),

    'KubeAudit': ('AKS Audit Logs', 'Management'),
    'KubeAuditAdmin': ('AKS Audit Logs (Admin)', 'Management'),

    'AZMSKeyVaultAuditLogs': ('Azure Key Vault Logs', 'Data'),
    'AZKVAuditLogs': ('Azure Key Vault Audit Logs', 'Data'),

    'StorageBlobLogs': ('Azure Storage Blob Logs', 'Data'),
}

####
# Helper Functions
####

def signal_handler(_signal_received, _frame):
    """Control-C handler"""
    print("\nExiting")
    sys.exit(0)

def log_message(level, details, context='', debug_mode=False):
    """
    Log a message to the global errors list and optionally exit if it's an ERROR in debug mode.

    Args:
        level: 'ERROR', 'WARNING', 'INFO', etc.
        details: The error message or exception object
        context: Context string (e.g., 'DiagnosticSettings')
        debug_mode: If True and level is ERROR, will exit immediately
    """
    try:
        function = f"{inspect.stack()[1].function}()"
    except IndexError:
        function = 'UnknownFunction'

    context_str = f"Context: {context} " if context else ""
    details_str = str(details).replace("\n", " ").replace("\r", " ")

    message = f"{level.upper()}: {context_str}{function} {details_str}"

    if level.upper() == 'ERROR':
        print(f"\n{message}\n")
        log_records.append(message)
        error_warning_counts['errors'] += 1

        if debug_mode:
            print(f"\nDEBUG MODE: Exiting on first error from {function}")
            sys.exit(1)
    elif level.upper() == 'WARNING':
        print(f"\nWARNING: {context_str}{function} {details_str}")
        log_records.append(message)
        error_warning_counts['warnings'] += 1
    else:
        log_records.append(message)

def rate_limit(calls_per_second=10):
    """Rate limiting decorator to prevent API throttling."""
    min_interval = 1.0 / calls_per_second
    last_called = [0.0]

    def decorator(func):
        @wraps(func)
        def wrapper(*func_args, **kwargs):
            elapsed = time.time() - last_called[0]
            left_to_wait = min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            last_called[0] = time.time()
            ret = func(*func_args, **kwargs)
            return ret
        return wrapper
    return decorator

def get_column_index_map(table):
    """Build a column name to index mapping, handling different column object types."""
    col_map = {}
    for i, col in enumerate(table.columns):
        if hasattr(col, 'name'):
            col_map[col.name] = i
        else:
            col_map[str(col)] = i
    return col_map

def safe_get_column_value(row, col_map, col_name, default=None):
    """Safely get column value with fallback."""
    if col_name in col_map:
        try:
            return row[col_map[col_name]]
        except (IndexError, TypeError):
            return default
    return default

def get_auth_headers(credential):
    """Get authorization headers for Azure API calls."""
    token = credential.get_token("https://management.azure.com/.default")
    return {'Authorization': f'Bearer {token.token}', 'Content-Type': 'application/json'}

####
# Discovery Functions
####

@rate_limit(calls_per_second=10)
def get_diagnostic_workspaces(credential, url, api_version, setting_type, verbose=False, debug=False):
    """Query diagnostic settings and extract workspace IDs."""
    workspaces = set()
    if verbose:
        print(f"    Checking {setting_type} diagnostic settings...")

    try:
        headers = get_auth_headers(credential)
        params = {'api-version': api_version}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()

        for setting in response.json().get('value', []):
            if workspace_id := setting.get('properties', {}).get('workspaceId'):
                workspaces.add(workspace_id)
                if verbose:
                    print(f"      Found workspace: {workspace_id.split('/')[-1]}")

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 403:
            inaccessible_sources.add(f"{setting_type.title()} Settings")
            log_message('WARNING', f"Insufficient permissions for {setting_type} settings (403 Forbidden).", "DiagnosticSettings", debug)
        elif status_code != 404:
            log_message('ERROR', e, "DiagnosticSettings", debug)
    except requests.exceptions.RequestException as e:
        log_message('ERROR', e, "DiagnosticSettings", debug)

    return workspaces

def get_management_groups(credential, verbose=False, debug=False):
    """Get all accessible management groups."""
    try:
        headers = get_auth_headers(credential)
        url = "https://management.azure.com/providers/Microsoft.Management/managementGroups"
        params = {'api-version': '2020-05-01'}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()

        mg_ids = [mg['name'] for mg in response.json().get('value', [])]
        if verbose and mg_ids:
            print(f"    Found {len(mg_ids)} management groups")
        return mg_ids
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 403:
            inaccessible_sources.add("Management Groups")
            log_message('WARNING', "Insufficient permissions to list management groups (403 Forbidden).", "ManagementGroups", debug)
        else:
            log_message('ERROR', e, "ManagementGroups", debug)
        return []
    except requests.exceptions.RequestException as e:
        log_message('ERROR', e, "ManagementGroups", debug)
        return []

def get_workspace_guid_and_name(credential, workspace_resource_id, verbose=False, debug=False):
    """Get the workspace GUID (customerId) and name from a resource ID."""
    if verbose:
        print(f"      Resolving workspace details for: {workspace_resource_id.split('/')[-1]}")
    try:
        parts = workspace_resource_id.split('/')
        if len(parts) < 9:
            return None, None

        subscription_id = parts[2]
        resource_client = ResourceManagementClient(credential, subscription_id)
        workspace = resource_client.resources.get_by_id(
            workspace_resource_id,
            api_version='2020-08-01'
        )

        guid = workspace.properties.get('customerId')
        name = workspace.name

        if verbose and guid:
            print(f"        -> GUID: {guid}, Name: {name}")
        return guid, name

    except HttpResponseError as e:
        log_message('ERROR', f"Could not resolve workspace {workspace_resource_id}: {e}", "WorkspaceDetails", debug)
        return None, None

def discover_tenant_workspaces(credential, verbose=False, debug=False):
    """Discover workspaces from tenant-level diagnostic settings (Entra ID).

    This should only be called once per script run, not per subscription.
    """
    if verbose:
        print("  Discovering tenant-level (Entra ID) workspaces...")

    workspace_resources = set()

    tenant_url = "https://management.azure.com/providers/microsoft.aadiam/providers/Microsoft.Insights/diagnosticSettings"
    workspace_resources.update(
        get_diagnostic_workspaces(credential, tenant_url, '2017-04-01-preview', 'tenant', verbose, debug)
    )

    workspaces = {}
    for resource_id in workspace_resources:
        guid, name = get_workspace_guid_and_name(credential, resource_id, verbose, debug)
        if guid and name:
            workspaces[guid] = name

    if verbose:
        print(f"  Found {len(workspaces)} tenant-level workspaces")
    return workspaces


def discover_management_group_workspaces(credential, verbose=False, debug=False):
    """Discover workspaces from management group diagnostic settings.

    This is tenant-level and should only be called once per script run.
    Returns a dict mapping workspace GUID to workspace name.
    """
    workspace_resources = set()

    mg_ids = get_management_groups(credential, verbose, debug)
    for mg_id in mg_ids:
        mg_url = (
            f"https://management.azure.com/providers/Microsoft.Management/"
            f"managementGroups/{mg_id}/providers/Microsoft.Insights/diagnosticSettings"
        )
        workspace_resources.update(
            get_diagnostic_workspaces(
                credential, mg_url, '2021-05-01-preview',
                f'management group {mg_id}', verbose, debug
            )
        )

    workspaces = {}
    for resource_id in workspace_resources:
        guid, name = get_workspace_guid_and_name(credential, resource_id, verbose, debug)
        if guid and name:
            workspaces[guid] = name

    if verbose:
        print(f"  Found {len(workspaces)} management group workspaces")
    return workspaces


def discover_subscription_workspaces(
    credential, subscription_id, mg_workspaces=None, verbose=False, debug=False
):
    """Discover workspaces from subscription diagnostic settings.

    Args:
        credential: Azure credential
        subscription_id: Subscription ID to analyze
        mg_workspaces: Pre-discovered management group workspaces (dict of GUID -> name)
        verbose: Enable verbose output
        debug: Enable debug mode
    """
    if verbose:
        print("  Discovering subscription-level workspaces...")

    workspace_resources = set()

    sub_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Insights/diagnosticSettings"
    )
    workspace_resources.update(
        get_diagnostic_workspaces(
            credential, sub_url, '2021-05-01-preview', 'subscription', verbose, debug
        )
    )

    workspaces = {}
    for resource_id in workspace_resources:
        guid, name = get_workspace_guid_and_name(credential, resource_id, verbose, debug)
        if guid and name:
            workspaces[guid] = name

    if mg_workspaces:
        workspaces.update(mg_workspaces)

    if verbose:
        print(f"  Found {len(workspaces)} subscription-level workspaces")
    return workspaces

####
# Query Functions
####

def query_workspace_usage(logs_client, workspace_id, start_time, end_time, days, verbose, debug):
    """Query workspace Usage table for billable data volumes.

    Returns:
        tuple: (results list, set of DataTypes found in Usage table)
    """
    if verbose:
        print("        Querying 'Usage' table for relevant log types...")
    relevant_types = list(RELEVANT_LOG_TYPES.keys())
    types_filter = ', '.join([f'"{dt}"' for dt in relevant_types])

    kql_query = f"""
    Usage
    | where TimeGenerated >= datetime({start_time.isoformat()}) and TimeGenerated < datetime({end_time.isoformat()})
    | where IsBillable == true and DataType in ({types_filter})
    | summarize IngestedMB = sum(Quantity) by DataType
    """
    results = []
    found_data_types = set()
    try:
        response = logs_client.query_workspace(workspace_id, kql_query, timespan=(start_time, end_time))
        if response.status == 'Success' and response.tables and response.tables[0].rows:
            if verbose:
                print(f"          -> Found {len(response.tables[0].rows)} billable data types.")
            table = response.tables[0]
            col_map = get_column_index_map(table)
            for row in table.rows:
                data_type = safe_get_column_value(row, col_map, 'DataType')
                ingested_mb = safe_get_column_value(row, col_map, 'IngestedMB', 0)
                if data_type in RELEVANT_LOG_TYPES:
                    found_data_types.add(data_type)
                    ingested_gb = ingested_mb / 1024
                    estimated_30day_gb = (ingested_gb / days) * 30 if days > 0 else 0
                    name, category = RELEVANT_LOG_TYPES[data_type]
                    results.append({'name': name, 'category': category, 'volume_gb': estimated_30day_gb, 'data_type': data_type})
        elif verbose:
            print("          -> No relevant billable data found in 'Usage' table.")
    except HttpResponseError as e:
        log_message('ERROR', f"Query failed on Usage table in {workspace_id}: {e}", "QueryUsage", debug)
    except (KeyError, IndexError) as e:
        log_message('ERROR', f"Could not parse Usage table response in {workspace_id}: {e}", "QueryUsage", debug)
    return results, found_data_types

def query_diagnostics_table(logs_client, workspace_id, start_time, end_time, days, target_providers, verbose, debug):
    """Check AzureDiagnostics for specific missing providers."""
    if not target_providers:
        return []

    if verbose:
        print(f"        Querying 'AzureDiagnostics' table for {', '.join(target_providers)}...")

    providers_filter = ', '.join([f'"{p}"' for p in target_providers])

    kql_query = f"""
    AzureDiagnostics
    | where TimeGenerated >= datetime({start_time.isoformat()}) and TimeGenerated < datetime({end_time.isoformat()})
    | where ResourceProvider in ({providers_filter})
    | summarize EstimatedBytes = sum(estimate_data_size(*)) by ResourceProvider
    """
    results = []
    try:
        response = logs_client.query_workspace(workspace_id, kql_query, timespan=(start_time, end_time))
        if response.status == 'Success' and response.tables and response.tables[0].rows:
            if verbose:
                print(f"          -> Found log data for {len(response.tables[0].rows)} provider(s).")
            table = response.tables[0]
            col_map = get_column_index_map(table)
            for row in table.rows:
                provider = safe_get_column_value(row, col_map, 'ResourceProvider')
                estimated_bytes = safe_get_column_value(row, col_map, 'EstimatedBytes', 0)
                if estimated_bytes > 0:
                    ingested_gb = estimated_bytes / (1024**3)
                    estimated_30day_gb = (ingested_gb / days) * 30 if days > 0 else 0
                    name = f"{provider.replace('MICROSOFT.', '')} Logs (from AzureDiagnostics)"
                    results.append({
                        'name': name,
                        'category': 'Data',
                        'volume_gb': estimated_30day_gb,
                        'data_type': f"AzureDiagnostics:{provider}"
                    })
        elif verbose:
            print("          -> No data found in 'AzureDiagnostics' for requested providers.")
    except HttpResponseError as e:
        if "PathNotFoundError" not in str(e):
            log_message('ERROR', f"Query failed on AzureDiagnostics table in {workspace_id}: {e}", "QueryDiagnostics", debug)
    except (KeyError, IndexError, TypeError) as e:
        log_message('ERROR', f"Could not parse AzureDiagnostics table response in {workspace_id}: {e}", "QueryDiagnostics", debug)
    return results

def query_activity_direct(logs_client, workspace_id, start_time, end_time, days, verbose, debug):
    """Query AzureActivity table directly as fallback."""
    if verbose:
        print("        Querying 'AzureActivity' table directly as a fallback...")
    kql_query = f"""
    AzureActivity
    | where TimeGenerated >= datetime({start_time.isoformat()}) and TimeGenerated < datetime({end_time.isoformat()})
    | summarize EstimatedBytes = sum(estimate_data_size(*))
    """
    try:
        response = logs_client.query_workspace(workspace_id, kql_query, timespan=(start_time, end_time))
        if response.status == 'Success' and response.tables and response.tables[0].rows:
            estimated_bytes = response.tables[0].rows[0][0] or 0
            if estimated_bytes > 0:
                if verbose:
                    print("          -> Found Azure Activity log data.")
                ingested_gb = estimated_bytes / (1024**3)
                estimated_30day_gb = (ingested_gb / days) * 30 if days > 0 else 0
                return [{
                    'name': 'Azure Activity Logs',
                    'category': 'Management',
                    'volume_gb': estimated_30day_gb,
                    'data_type': 'AzureActivity'
                }]
        elif verbose:
            print("          -> No data found from direct 'AzureActivity' query.")
    except HttpResponseError as e:
        if "PathNotFoundError" not in str(e):
            log_message('ERROR', f"Direct query failed on AzureActivity table in {workspace_id}: {e}", "QueryActivity", debug)
    except (IndexError, TypeError) as e:
        log_message('ERROR', f"Could not parse AzureActivity direct query response in {workspace_id}: {e}", "QueryActivity", debug)
    return []

def query_storage_blob_logs_direct(logs_client, workspace_id, start_time, end_time, days, verbose, debug):
    """Query StorageBlobLogs table directly as fallback."""
    if verbose:
        print("        Querying 'StorageBlobLogs' table directly as a fallback...")
    kql_query = f"""
    StorageBlobLogs
    | where TimeGenerated >= datetime({start_time.isoformat()}) and TimeGenerated < datetime({end_time.isoformat()})
    | summarize EstimatedBytes = sum(estimate_data_size(*))
    """
    try:
        response = logs_client.query_workspace(workspace_id, kql_query, timespan=(start_time, end_time))
        if response.status == 'Success' and response.tables and response.tables[0].rows:
            estimated_bytes = response.tables[0].rows[0][0] or 0
            if estimated_bytes > 0:
                if verbose:
                    print("          -> Found Azure Storage Blob log data.")
                ingested_gb = estimated_bytes / (1024**3)
                estimated_30day_gb = (ingested_gb / days) * 30 if days > 0 else 0
                return [{
                    'name': 'Azure Storage Blob Logs',
                    'category': 'Data',
                    'volume_gb': estimated_30day_gb,
                    'data_type': 'StorageBlobLogs'
                }]
        elif verbose:
            print("          -> No data found from direct 'StorageBlobLogs' query.")
    except HttpResponseError as e:
        if "PathNotFoundError" not in str(e):
            log_message('ERROR', f"Direct query failed on StorageBlobLogs table in {workspace_id}: {e}", "QueryStorageBlobLogs", debug)
    except (IndexError, TypeError) as e:
        log_message('ERROR', f"Could not parse StorageBlobLogs direct query response in {workspace_id}: {e}", "QueryStorageBlobLogs", debug)
    return []

def analyze_workspace(credential, workspace_id, workspace_name, days, verbose, debug):
    """Analyze a single workspace for log volumes.

    Returns:
        tuple: (results list, bool indicating if any data was found)
    """
    if verbose:
        print(f"    Analyzing workspace: {workspace_name} ({workspace_id})")

    logs_client = LogsQueryClient(credential)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)

    results, found_types = query_workspace_usage(logs_client, workspace_id, start_time, end_time, days, verbose, debug)

    if 'AzureActivity' not in found_types:
        activity_results = query_activity_direct(logs_client, workspace_id, start_time, end_time, days, verbose, debug)
        results.extend(activity_results)

    has_storage_blob_logs = 'StorageBlobLogs' in found_types
    if not has_storage_blob_logs:
        storage_blob_results = query_storage_blob_logs_direct(logs_client, workspace_id, start_time, end_time, days, verbose, debug)
        if storage_blob_results:
            results.extend(storage_blob_results)
            has_storage_blob_logs = True

    target_providers = []

    has_keyvault_logs = 'AZMSKeyVaultAuditLogs' in found_types or 'AZKVAuditLogs' in found_types
    if not has_keyvault_logs:
        target_providers.append('MICROSOFT.KEYVAULT')

    if not has_storage_blob_logs:
        target_providers.append('MICROSOFT.STORAGE')

    if target_providers:
        diag_results = query_diagnostics_table(
            logs_client, workspace_id, start_time, end_time, days, target_providers, verbose, debug
        )
        results.extend(diag_results)

    analysis_succeeded = len(results) > 0 or len(found_types) > 0
    return results, analysis_succeeded

####
# Main Execution and Output
####

def get_accessible_subscriptions(credential, debug=False):
    """Get all subscriptions accessible by the credential."""
    try:
        sub_client = SubscriptionClient(credential)
        return [sub.subscription_id for sub in sub_client.subscriptions.list() if sub.state == 'Enabled']
    except (HttpResponseError, ClientAuthenticationError) as e:
        log_message('ERROR', f"Could not list subscriptions: {e}", "ListSubscriptions", debug)
        print("\nFATAL: Could not list subscriptions. Check permissions.", file=sys.stderr)
        sys.exit(1)

def output_results(all_results, total_volume, days, sub_count, successful_subs, output_filename, errors_log_filename):
    """Format and print the final summary and write output files."""

    category_results = {}
    for result in all_results:
        cat = result['category']
        if cat not in category_results:
            category_results[cat] = []
        category_results[cat].append(result)

    category_totals = {}
    for result in all_results:
        cat = result['category']
        category_totals[cat] = category_totals.get(cat, 0) + result['volume_gb']

    try:
        with open(output_filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                'Log Source Type', 'Billable Category', 'Specific Metric',
                'Resource/Scope Details', 'Estimated 30-Day Uncompressed Volume (GB)'
            ])
            for result in sorted(all_results, key=lambda x: x['volume_gb'], reverse=True):
                is_entra_log = result['name'].startswith('Entra ID')
                scope = "Tenant-wide" if is_entra_log else f"Subscription: {result['subscription_id']}"

                writer.writerow([
                    'Azure Log Analytics',
                    f"{result['category']} Logs Ingestion GB",
                    result['name'],
                    f"{scope} / Workspace: {result['workspace_name']}",
                    f"{result['volume_gb']:.2f}"
                ])
    except IOError as e:
        print(f"\nCRITICAL: Failed to write to CSV file {output_filename}: {e}", file=sys.stderr)

    if log_records:
        try:
            with open(errors_log_filename, 'w', encoding='utf-8') as err_file:
                for record in log_records:
                    err_file.write(record + "\n")
        except IOError as e:
            print(f"CRITICAL: Failed to write to error log file {errors_log_filename}: {e}", file=sys.stderr)

    print(f"\nAzure Log Volume Estimation Results (script version: {VERSION})\n")
    print("Wiz Defend Ingestion: Azure Log Volume Estimation (Uncompressed, Normalized to 30 days)\n")

    print(f"Successfully analyzed {successful_subs} of {sub_count} subscriptions.")
    print(f"Time period: Last {days} days (results extrapolated to 30-day volume)\n")

    if inaccessible_sources:
        print("--- Inaccessible Sources ---")
        print("NOTE: The following sources could not be analyzed due to permissions.")
        print("      The total volume estimate may be incomplete. See log file for details.")
        for source in sorted(inaccessible_sources):
            print(f"      * {source}")
        print("")

    category_order = ['Management', 'Identity', 'Data']
    for category in category_order:
        if category not in category_results:
            continue

        print("Log Source: Azure Log Analytics")
        print(f"  Billable Category: {category} Logs Ingestion GB")

        sorted_results = sorted(category_results[category], key=lambda x: x['volume_gb'], reverse=True)
        for result in sorted_results:
            if result['volume_gb'] > 0:
                print(f"    {result['name'].ljust(padding_desc)}: {result['volume_gb']:.2f} GB")

        print(f"  {'Total ' + category + ' Logs'.ljust(padding_desc + 2)}: {category_totals.get(category, 0):.2f} GB")
        print("")

    print("--- Overall Summary ---")
    print(f"{'Total Estimated 30-Day Volume'.ljust(padding_desc)}: {total_volume:.2f} GB")
    print(f"{'Average Daily Volume'.ljust(padding_desc)}: {total_volume / 30:.2f} GB\n")

    print("---")
    print("Disclaimer and Recommendations")
    print("* Estimates are based on querying Azure Log Analytics workspaces discovered via diagnostic settings.")
    print("* The accuracy of this script depends on the permissions of the authenticated principal.")
    print("  Ensure it has 'Log Analytics Reader' on target workspaces and 'Reader' on subscriptions.")
    print("* All volumes are extrapolated to a 30-day period for consistent estimation.")
    print("* Volumes shown are uncompressed data sizes in GB.")
    print("---\n")

    print(f"\nDetails written to {output_filename}")

    if error_warning_counts['errors'] > 0:
        if args.debug:
            print(f"\n{error_warning_counts['errors']} error(s) occurred. Review {errors_log_filename} for details.")
        else:
            print(f"\n{error_warning_counts['errors']} error(s) occurred. Review {errors_log_filename} or rerun with '--debug' to stop on first error.")

    if error_warning_counts['warnings'] > 0:
        print(f"{error_warning_counts['warnings']} warning(s) logged. Review {errors_log_filename} for details.")

    if error_warning_counts['errors'] == 0 and error_warning_counts['warnings'] == 0:
        print(f"No errors or warnings reported to {errors_log_filename}.")


def main():
    """Main execution flow."""
    print("Starting Azure Log Volume Estimator for Wiz Defend...")

    try:
        credential = ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential())
        credential.get_token("https://management.azure.com/.default")
    except ClientAuthenticationError as e:
        print("\nERROR: Azure authentication failed. Have you run 'az login'?", file=sys.stderr)
        log_message('ERROR', f"Authentication failed: {e}", "Authentication", args.debug)
        sys.exit(1)

    if args.all_subscriptions:
        if args.verbose:
            print("Discovering all accessible subscriptions...")
        sub_ids = get_accessible_subscriptions(credential, args.debug)
        print(f"Found {len(sub_ids)} enabled subscriptions to analyze.")
    else:
        sub_ids = [args.subscription_id]

    all_results = []
    total_volume = 0
    processed_workspaces = set()
    subs_with_workspaces = set()

    if args.verbose:
        print("\n--- Discovering Tenant-Level Workspaces (Entra ID) ---")
    tenant_workspaces = discover_tenant_workspaces(credential, args.verbose, args.debug)

    for workspace_id, workspace_name in tenant_workspaces.items():
        processed_workspaces.add(workspace_id)

        workspace_results, _ = analyze_workspace(
            credential, workspace_id, workspace_name, args.log_analysis_days, args.verbose, args.debug
        )
        for result in workspace_results:
            if result.get('volume_gb', 0) > 0:
                all_results.append({
                    'subscription_id': 'tenant',
                    'workspace_name': workspace_name,
                    **result
                })
                total_volume += result['volume_gb']

    if args.verbose:
        print("\n--- Discovering Management Group Workspaces ---")
    mg_workspaces = discover_management_group_workspaces(
        credential, args.verbose, args.debug
    )

    for sub_id in sub_ids:
        if args.verbose:
            print(f"\n--- Processing Subscription: {sub_id} ---")

        workspaces = discover_subscription_workspaces(
            credential, sub_id, mg_workspaces, args.verbose, args.debug
        )
        if not workspaces:
            if args.verbose:
                print("  No configured Log Analytics workspaces found for this subscription's diagnostic settings.")
            continue

        for workspace_id, workspace_name in workspaces.items():
            if workspace_id in processed_workspaces:
                if args.verbose:
                    print(f"    Skipping workspace {workspace_name} ({workspace_id}) - already analyzed")
                subs_with_workspaces.add(sub_id)
                continue

            processed_workspaces.add(workspace_id)

            workspace_results, analysis_succeeded = analyze_workspace(
                credential, workspace_id, workspace_name, args.log_analysis_days, args.verbose, args.debug
            )
            if analysis_succeeded:
                subs_with_workspaces.add(sub_id)

            for result in workspace_results:
                if result.get('volume_gb', 0) > 0:
                    all_results.append({
                        'subscription_id': sub_id,
                        'workspace_name': workspace_name,
                        **result
                    })
                    total_volume += result['volume_gb']

    successful_subs = len(subs_with_workspaces)
    output_results(all_results, total_volume, args.log_analysis_days, len(sub_ids), successful_subs, args.output_filename, args.errors_log_filename)
    print("\nEstimation complete.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
