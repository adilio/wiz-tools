#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines, too-many-locals

"""
Estimate or measure GCP log volumes for Wiz Defend at a project or organization level.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
import csv
import traceback
import statistics
import concurrent.futures
import inspect

try:
    import google.auth
    from google.cloud import monitoring_v3, resourcemanager_v3, logging_v2
    from google.api_core import exceptions as google_api_exceptions
except ImportError:
    print("\nERROR: Missing required GCP SDK packages. Run:")
    print("pip3 install --user --upgrade google-cloud-monitoring google-api-core google-auth google-cloud-resource-manager google-cloud-logging")
    sys.exit(1)

VERSION = '1.0.0'

####
# Command Line Arguments
####

parser = argparse.ArgumentParser(
    description=f'GCP Log Volume Estimator for Wiz Defend (v{VERSION})'
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
    default=f'gcp-defend-log-volume-{datetime.now().strftime("%Y%m%d-%H%M%S")}.csv',
    help='Name of the output CSV file.'
)
parser.add_argument(
    '--errors-log-filename',
    dest='errors_log_filename',
    default='gcp-defend-errors-log.txt',
    help='Name of the error log file.'
)

estimation_group = parser.add_argument_group('Log Volume Estimation Arguments')

scope_group = estimation_group.add_mutually_exclusive_group(required=False)
scope_group.add_argument(
    '--project-id',
    dest='project_id',
    help='GCP Project ID to analyze. If omitted, the script will attempt to auto-detect the default project.'
)
scope_group.add_argument(
    '--organization-id',
    dest='organization_id',
    help='GCP Organization ID to analyze all active projects within.'
)

estimation_group.add_argument(
    '--org-aggregate',
    action='store_true',
    help='Use organization-level aggregation for faster analysis (requires org permissions).'
)
estimation_group.add_argument(
    '--log-analysis-days',
    dest='log_analysis_days',
    type=int,
    default=30,
    help='Number of past days of logs to analyze (default: 30)'
)
estimation_group.add_argument(
    '--workers',
    type=int,
    default=10,
    help='Number of parallel threads for project analysis (default: 10).'
)

method_group = parser.add_argument_group('Measurement Method')
method_group.add_argument(
    '--use-sink-metrics',
    action='store_true',
    help='Enable direct measurement from a log sink. Will auto-discover Wiz sinks if --sink-name is not specified.'
)
method_group.add_argument(
    '--sink-name',
    help='Specify the exact name of the log sink to measure.'
)
method_group.add_argument(
    '--no-exclusion-adjustment',
    action='store_true',
    help='(Estimation Mode Only) Disable exclusion ratio adjustments for GKE and Data Access logs.'
)

args = parser.parse_args()


####
# Configuration and Globals
####

log_records = []
inaccessible_sources = set()
padding_desc = 40

# Mapping of GCP log types to a friendly name and billable category for reporting
GCP_LOG_MAPPING = {
    'admin_activity_non_gke': ('Admin Activity Logs (Non-GKE)', 'Management'),
    'gke_audit': ('GKE Audit Logs', 'Management'),
    'data_access_non_storage': ('Data Access Logs (Non-Storage)', 'Management'),
    'storage_data_access': ('Cloud Storage Data Access Logs', 'Data'),
    'workspace_audit': ('Google Workspace Audit Logs', 'Identity'),
    'measured_sink': ('Log Sink (Actual Volume)', 'Total Ingestion (Actual)')
}

# Exclusion ratios for more accurate estimation where only a subset of logs are needed
EXCLUSION_RATIOS = {
    'gke_audit': {
        'k8s_cluster': 0.14,
        'gke_cluster': 0.12,
    },
    'data_access_non_storage': {
        'cloud_function': 0.20,
        'gce_instance': 0.10,
        'default': 0.14
    }
}

####
# Helper Functions
####

def log_message(level, details, context='', debug_mode=False):
    """Log a message to the global list and optionally re-raise if it's an ERROR in debug mode."""
    try:
        function = f"{inspect.stack()[1].function}()"
    except IndexError:
        function = 'UnknownFunction'

    message = f"{level.upper()}: Context: {context} in {function} - {str(details).replace(chr(10), ' ')}"
    log_records.append(message)

    if level.upper() == 'ERROR' and debug_mode:
        print(f"\nDEBUG MODE: Re-raising exception from {function}")
        if isinstance(details, Exception):
            raise details
        else:
            raise RuntimeError(message)

def analyze_volume_trend(time_series, log_type, resource_type, project_id, verbose):
    """Analyzes a single time series for high variance to flag estimate uncertainty."""
    if len(time_series.points) < 2:
        return
    values = [p.value.double_value for p in time_series.points]
    try:
        mean = statistics.mean(values)
        if mean == 0:
            return
        std_dev = statistics.stdev(values)
        cv = std_dev / mean
        if cv > 0.5 and verbose:
            trend_msg = f"High volume variation (CV > {cv:.2f}) detected for log '{log_type}' on resource '{resource_type}'"
            log_message('INFO', trend_msg, f"TrendAnalysis:{project_id}", False)
    except statistics.StatisticsError:
        pass

####
# Discovery Functions
####

def discover_org_level_sinks(org_id, verbose, debug):
    """Discovers all sinks at organization level."""
    discovered_sinks = []
    if verbose:
        print(f"  Discovering organization-level sinks in org '{org_id}'...")

    try:
        logging_client = logging_v2.Client()
        parent = f"organizations/{org_id}"

        for sink in logging_client.list_sinks(parent=parent):
            if args.sink_name:
                if sink.name == args.sink_name:
                    discovered_sinks.append((sink.name, 'organization', org_id))
            elif 'wiz' in sink.name.lower() or 'wiz' in getattr(sink, 'destination', '').lower():
                discovered_sinks.append((sink.name, 'organization', org_id))
                if verbose:
                    print(f"    Found Wiz-related org sink: '{sink.name}'")

        return discovered_sinks
    except google_api_exceptions.PermissionDenied:
        log_message('WARNING', f"Permission denied to list org-level sinks in '{org_id}'.", f"OrgSinkDiscovery:{org_id}", debug)
        inaccessible_sources.add(f"Organization-level sinks in '{org_id}'")
        return []
    except Exception as e: # pylint: disable=broad-except
        log_message('WARNING', f"Could not list org sinks in '{org_id}': {e}", f"OrgSinkDiscovery:{org_id}", debug)
        return []

def query_org_metrics_aggregated(org_id, monitoring_client, interval, verbose, debug):
    """Query all log metrics at organization level with project breakdown."""
    if verbose:
        print(f"  Querying aggregated metrics for organization '{org_id}'...")

    org_path = f"organizations/{org_id}"
    results_by_project = {}

    try:
        filter_str = 'metric.type="logging.googleapis.com/byte_count"'
        request = {
            "name": org_path,
            "filter": filter_str,
            "interval": interval,
            "aggregation": {
                "alignment_period": {"seconds": 3600},
                "per_series_aligner": "ALIGN_RATE",
                "cross_series_reducer": "REDUCE_SUM",
                "group_by_fields": ["resource.labels.project_id", "metric.labels.log", "resource.type"]
            }
        }

        results = monitoring_client.list_time_series(request=request)

        for series in results:
            project_id = series.resource.labels.get("project_id", "unknown")
            if project_id == "unknown":
                continue

            if project_id not in results_by_project:
                results_by_project[project_id] = {
                    "cloudaudit.googleapis.com/activity": {},
                    "cloudaudit.googleapis.com/data_access": {},
                    "workspace_volume": 0.0
                }

            log_type = series.metric.labels.get("log", "")
            resource_type = series.resource.type

            total_bytes, point_count = 0, 0
            for point in series.points:
                total_bytes += point.value.double_value * 3600
                point_count += 1

            if point_count > 0:
                avg_bytes_per_day = (total_bytes / point_count) * 24
                estimated_30day_gb = (avg_bytes_per_day / (1024**3)) * 30

                if log_type in ["cloudaudit.googleapis.com/activity", "cloudaudit.googleapis.com/data_access"]:
                    if resource_type == "audited_resource" and log_type == "cloudaudit.googleapis.com/activity":
                        results_by_project[project_id]["workspace_volume"] += estimated_30day_gb
                    else:
                        results_by_project[project_id][log_type][resource_type] = estimated_30day_gb

        return results_by_project

    except google_api_exceptions.PermissionDenied:
        log_message('WARNING', f"Permission denied for org-level metrics in '{org_id}'.", f"OrgMetrics:{org_id}", debug)
        return None
    except Exception as e: # pylint: disable=broad-except
        log_message('ERROR', f"Failed to query org metrics: {e}", f"OrgMetrics:{org_id}", debug)
        return None

def query_org_sink_metrics(monitoring_client, org_path, interval, sink_name, scope_type, verbose, debug):
    """Query sink metrics at organization level."""
    if verbose:
        print(f"    Querying metrics for {scope_type}-level sink: '{sink_name}'")

    filter_str = (f'metric.type="logging.googleapis.com/exports/byte_count" '
                  f'AND resource.type="logging_sink" AND resource.labels.name="{sink_name}"')

    try:
        request = {
            "name": org_path,
            "filter": filter_str,
            "interval": interval,
            "aggregation": {
                "alignment_period": {"seconds": 3600},
                "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_RATE
            }
        }

        results = monitoring_client.list_time_series(request=request)
        total_bytes, point_count = 0, 0

        for series in results:
            for point in series.points:
                total_bytes += point.value.double_value * 3600
                point_count += 1

        if point_count == 0:
            return 0.0

        avg_bytes_per_day = (total_bytes / point_count) * 24
        return (avg_bytes_per_day / (1024**3)) * 30

    except Exception as e: # pylint: disable=broad-except
        log_message('ERROR', f"Failed to get metrics for org sink '{sink_name}': {e}", "QueryOrgSink", debug)
        return 0.0

def get_target_projects():
    """Get the list of project IDs to analyze based on user arguments."""
    if args.organization_id:
        print(f"Discovering projects in organization '{args.organization_id}'...")
        try:
            client = resourcemanager_v3.ProjectsClient()
            request = resourcemanager_v3.ListProjectsRequest(parent=f"organizations/{args.organization_id}")
            projects = client.list_projects(request=request)
            project_ids = [p.project_id for p in projects if p.state == resourcemanager_v3.Project.State.ACTIVE]
            print(f"Found {len(project_ids)} active projects.")
            return project_ids
        except google_api_exceptions.PermissionDenied:
            log_message('ERROR', f"Permission denied to list projects in org '{args.organization_id}'. Ensure you have 'resourcemanager.projects.list' permission.", "ProjectDiscovery", args.debug)
            inaccessible_sources.add(f"Organization '{args.organization_id}'")
            return []
        except google_api_exceptions.GoogleAPICallError as e:
            log_message('ERROR', f"API call failed when listing projects in org '{args.organization_id}': {e}", "ProjectDiscovery", args.debug)
            return []

    if args.project_id:
        return [args.project_id]

    try:
        _, project_id = google.auth.default()
        if not project_id:
            raise ValueError("Could not determine project ID from GCloud credentials.")
        print(f"Successfully auto-detected project ID: {project_id}")
        return [project_id]
    except (Exception, ValueError) as e: # pylint: disable=broad-except
        log_message('ERROR', f"Project auto-detection failed: {e}. Please specify --project-id or --organization-id.", "ProjectDiscovery", args.debug)
        return []

def discover_wiz_sinks(project_id, verbose, debug):
    """Discovers all sinks in a project that contain 'wiz' in their name or destination."""
    discovered_sinks = []
    if verbose:
        print(f"    Discovering sinks in project '{project_id}'...")

    try:
        logging_client = logging_v2.Client(project=project_id)
        for sink in logging_client.list_sinks(parent=f"projects/{project_id}"):
            if 'wiz' in sink.name.lower() or 'wiz' in getattr(sink, 'destination', '').lower():
                discovered_sinks.append(sink.name)
                if verbose:
                    print(f"      Found Wiz-related sink: '{sink.name}'")
        return discovered_sinks
    except google_api_exceptions.PermissionDenied:
        log_message('WARNING', f"Permission denied to list sinks in project '{project_id}'.", f"SinkDiscovery:{project_id}", debug)
        inaccessible_sources.add(f"Log Sinks in '{project_id}'")
        return []
    except Exception as e: # pylint: disable=broad-except
        log_message('WARNING', f"Could not list sinks in project '{project_id}': {e}", f"SinkDiscovery:{project_id}", debug)
        return []

####
# Query Functions (Original Core Logic Restored)
####

def query_sink_metrics(client, project_path, interval, sink_name, verbose, debug):
    """Queries 'exports/byte_count' metric for a sink, using original ALIGN_RATE logic."""
    if verbose:
        print(f"      Querying metrics for sink: '{sink_name}'")
    project_id = project_path.split('/')[-1]
    filter_str = (f'metric.type="logging.googleapis.com/exports/byte_count" '
                  f'AND resource.type="logging_sink" AND resource.labels.name="{sink_name}"')
    try:
        request = {
            "name": project_path, "filter": filter_str, "interval": interval,
            "aggregation": {"alignment_period": {"seconds": 3600}, "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_RATE}
        }
        results = client.list_time_series(request=request)
        total_bytes, point_count = 0, 0
        for series in results:
            for point in series.points:
                total_bytes += point.value.double_value * 3600
                point_count += 1
        if point_count == 0:
            return 0.0

        avg_bytes_per_day = (total_bytes / point_count) * 24
        return (avg_bytes_per_day / (1024**3)) * 30

    except google_api_exceptions.PermissionDenied:
        log_message('WARNING', f"Permission denied to read metrics for sink '{sink_name}'", f"QuerySink:{project_id}", debug)
        inaccessible_sources.add(f"Sink metrics for '{sink_name}' in '{project_id}'")
    except Exception as e: # pylint: disable=broad-except
        log_message('ERROR', f"Failed to get metrics for sink '{sink_name}': {e}", f"QuerySink:{project_id}", debug)
    return 0.0

def query_audit_logs_combined(client, project_path, interval, verbose, debug):
    """Queries Admin/Data Access logs using original ALIGN_RATE logic."""
    if verbose:
        print("      Querying combined audit log metrics (Admin Activity, Data Access)...")
    project_id = project_path.split('/')[-1]
    filter_str = 'metric.type="logging.googleapis.com/byte_count" AND (metric.labels.log="cloudaudit.googleapis.com/activity" OR metric.labels.log="cloudaudit.googleapis.com/data_access")'
    try:
        request = {
            "name": project_path, "filter": filter_str, "interval": interval,
            "aggregation": {
                "alignment_period": {"seconds": 3600}, "per_series_aligner": "ALIGN_RATE",
                "cross_series_reducer": "REDUCE_SUM",
                "group_by_fields": ["metric.labels.log", "resource.type"]
            }
        }
        results = client.list_time_series(request=request)
        volumes = {"cloudaudit.googleapis.com/activity": {}, "cloudaudit.googleapis.com/data_access": {}}
        for series in results:
            log_type = series.metric.labels.get("log")
            resource_type = series.resource.type
            if not log_type or not resource_type:
                continue

            analyze_volume_trend(series, log_type, resource_type, project_id, verbose)

            total_bytes, point_count = 0, 0
            for point in series.points:
                total_bytes += point.value.double_value * 3600
                point_count += 1
            if point_count > 0:
                avg_bytes_per_day = (total_bytes / point_count) * 24
                estimated_30day_gb = (avg_bytes_per_day / (1024**3)) * 30
                if log_type in volumes:
                    volumes[log_type][resource_type] = estimated_30day_gb
        return volumes
    except google_api_exceptions.PermissionDenied:
        log_message('WARNING', "Permission denied to read monitoring data.", f"QueryAudit:{project_id}", debug)
        inaccessible_sources.add(f"Audit log metrics for '{project_id}'")
        return None
    except Exception as e: # pylint: disable=broad-except
        log_message('ERROR', f"Error getting combined audit log metrics: {e}", f"QueryAudit:{project_id}", debug)
        return None

def query_simple_metric(client, project_path, interval, metric_filter, log_name_for_msg, verbose, debug):
    """Simple metric query using original ALIGN_RATE logic."""
    if verbose:
        print(f"      Querying metrics for {log_name_for_msg}...")
    project_id = project_path.split('/')[-1]
    filter_str = f'metric.type="logging.googleapis.com/byte_count" AND {metric_filter}'
    try:
        request = {
            "name": project_path, "filter": filter_str, "interval": interval,
            "aggregation": {
                "alignment_period": {"seconds": 3600}, "per_series_aligner": "ALIGN_RATE",
                "cross_series_reducer": "REDUCE_SUM", "group_by_fields": []
            }
        }
        results = client.list_time_series(request=request)
        total_bytes, point_count = 0, 0
        for series in results:
            analyze_volume_trend(series, log_name_for_msg, series.resource.type or "N/A", project_id, verbose)
            for point in series.points:
                total_bytes += point.value.double_value * 3600
                point_count += 1
        if point_count == 0:
            return 0.0
        avg_bytes_per_day = (total_bytes / point_count) * 24
        return (avg_bytes_per_day / (1024**3)) * 30
    except Exception as e: # pylint: disable=broad-except
        log_message('WARNING', f"Could not get metrics for '{log_name_for_msg}': {e}", f"QuerySimple:{project_id}", debug)
        return 0.0

####
# Analysis Functions
####

def get_exclusion_ratio(log_type, resource_type):
    """Get exclusion ratio based on log and resource type combination."""
    if log_type in EXCLUSION_RATIOS and isinstance(EXCLUSION_RATIOS[log_type], dict):
        return EXCLUSION_RATIOS[log_type].get(resource_type, EXCLUSION_RATIOS[log_type].get('default', 1.0))
    return 1.0

def process_estimation_volumes(combined_volumes, workspace_volume, no_exclusion_adjustment):
    """Process all estimated volumes and apply exclusion ratios."""
    results = []
    # Admin Activity
    admin_volumes = combined_volumes.get("cloudaudit.googleapis.com/activity", {})
    gke_volume, non_gke_volume = 0.0, 0.0
    gke_resource_types = EXCLUSION_RATIOS.get('gke_audit', {}).keys()
    for rtype, volume in admin_volumes.items():
        if rtype in gke_resource_types and not no_exclusion_adjustment:
            gke_volume += volume * get_exclusion_ratio('gke_audit', rtype)
        else:
            non_gke_volume += volume
    results.append({'log_key': 'admin_activity_non_gke', 'volume_gb': non_gke_volume})
    results.append({'log_key': 'gke_audit', 'volume_gb': gke_volume})

    # Data Access
    data_access_volumes = combined_volumes.get("cloudaudit.googleapis.com/data_access", {})
    storage_volume = data_access_volumes.get('gcs_bucket', 0.0)
    non_storage_volume = 0.0
    for rtype, volume in data_access_volumes.items():
        if rtype != 'gcs_bucket':
            multiplier = 1.0 if no_exclusion_adjustment else get_exclusion_ratio('data_access_non_storage', rtype)
            non_storage_volume += volume * multiplier
    results.append({'log_key': 'data_access_non_storage', 'volume_gb': non_storage_volume})
    results.append({'log_key': 'storage_data_access', 'volume_gb': storage_volume})

    # Workspace
    results.append({'log_key': 'workspace_audit', 'volume_gb': workspace_volume})
    return results

def analyze_project(project_id, monitoring_client, interval):
    """
    Analyzes a single GCP project for log volumes. Designed to be run in a worker thread.
    Returns a tuple of (project_id, list_of_results, status_message).
    """
    if args.verbose:
        print(f"  Analyzing project: {project_id}")

    project_path = f"projects/{project_id}"
    project_results = []

    try:
        if args.use_sink_metrics:
            sinks_to_measure = [args.sink_name] if args.sink_name else discover_wiz_sinks(project_id, args.verbose, args.debug)
            if not sinks_to_measure:
                status = "No Wiz-related sinks found to measure."
                if args.sink_name:
                    status = f"Specified sink '{args.sink_name}' not found or no metrics available."
                return project_id, [], status

            for sink_name in sinks_to_measure:
                sink_volume_gb = query_sink_metrics(monitoring_client, project_path, interval, sink_name, args.verbose, args.debug)
                if sink_volume_gb > 0:
                    project_results.append({
                        'log_key': 'measured_sink',
                        'volume_gb': sink_volume_gb,
                        'specific_metric': f"Log Sink: {sink_name}"
                    })
        else:
            combined_volumes = query_audit_logs_combined(monitoring_client, project_path, interval, args.verbose, args.debug)
            if combined_volumes is None:
                return project_id, [], "Permission denied or API error during audit log query."

            workspace_volume = query_simple_metric(
                monitoring_client, project_path, interval,
                'metric.labels.log="cloudaudit.googleapis.com/activity" AND resource.type="audited_resource"',
                "Google Workspace", args.verbose, args.debug
)
            processed_volumes = process_estimation_volumes(combined_volumes, workspace_volume, args.no_exclusion_adjustment)
            for result in processed_volumes:
                if result['volume_gb'] > 0:
                    project_results.append({
                        'log_key': result['log_key'],
                        'volume_gb': result['volume_gb'],
                        'specific_metric': GCP_LOG_MAPPING[result['log_key']][0]
                    })

        project_total = sum(r.get('volume_gb', 0) for r in project_results)
        status = f"Project Total: {project_total:.2f} GB" if project_total > 0 else "Project has no reportable log volume."
        return project_id, project_results, status

    except Exception as e: # pylint: disable=broad-except
        log_message('ERROR', f"An unexpected error occurred: {e}", f"AnalyzeProject:{project_id}", args.debug)
        if args.verbose:
            traceback.print_exc()
        return project_id, [], "An unexpected error occurred."


####
# Main Execution and Output
####

def output_results(all_results, total_volume, days, proj_count, successful_projs, output_filename, errors_log_filename):
    """Format and print the final summary and write output files."""
    print("\n" + "="*50)
    print("Wiz Defend Ingestion: GCP Log Volume Estimation")
    print(f"Script Version: {VERSION}")
    print("="*50 + "\n")

    print(f"Successfully analyzed {successful_projs} of {proj_count} projects.")
    print(f"Time period: Last {days} days (results extrapolated to 30-day volume)")

    if inaccessible_sources:
        print("\n--- Inaccessible Sources ---")
        print("NOTE: The following sources could not be analyzed due to permissions.")
        print("      The total volume estimate may be incomplete. See log file for details.")
        for source in sorted(list(inaccessible_sources)):
            print(f"      * {source}")

    print("\n--- Overall Summary ---")
    print(f"{'Total Estimated 30-Day Volume'.ljust(padding_desc)}: {total_volume:.2f} GB")
    print(f"{'Average Daily Volume'.ljust(padding_desc)}: {total_volume / 30:.2f} GB")

    category_totals = {}
    for result in all_results:
        cat = result['category']
        category_totals[cat] = category_totals.get(cat, 0) + result['volume_gb']

    print("\n--- Volume by Billable Category ---")
    for category, total in sorted(category_totals.items()):
        label = f"Total {category} Logs"
        print(f"  {label.ljust(padding_desc)}: {total:.2f} GB")

    print("\n" + "="*50)

    try:
        with open(output_filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                'Log Source Type', 'Billable Category', 'Specific Metric',
                'Resource/Scope Details', 'Estimated 30-Day Uncompressed Volume (GB)'
            ])
            for result in sorted(all_results, key=lambda x: x['volume_gb'], reverse=True):
                writer.writerow([
                    'GCP Monitoring Metrics',
                    result['category'],
                    result['name'],
                    f"Project: {result['project_id']}",
                    f"{result['volume_gb']:.2f}"
                ])
        print(f"\nDetailed results saved to: {output_filename}")
    except IOError as e:
        print(f"\nCRITICAL: Failed to write to CSV file {output_filename}: {e}", file=sys.stderr)

    if log_records:
        has_errors = any(rec.startswith('ERROR:') for rec in log_records)
        if has_errors:
            print("\n--- Errors Encountered ---")
            for rec in log_records:
                if rec.startswith('ERROR:'):
                    print(f"  {rec}")

        try:
            with open(errors_log_filename, 'w', encoding='utf-8') as err_file:
                for record in log_records:
                    err_file.write(record + "\n")
            print(f"All issues (warnings/errors/info) were logged to: {errors_log_filename}")
        except IOError as e:
            print(f"CRITICAL: Failed to write to error log file {errors_log_filename}: {e}", file=sys.stderr)

    print("\n--- Disclaimer and Recommendations ---")
    print("* Estimates are based on querying GCP Monitoring metrics ('logging.googleapis.com/byte_count').")
    print("* The accuracy of this script depends on the permissions of the authenticated principal.")
    print("  Ensure it has 'monitoring.timeSeries.list' on target projects.")
    print("  For organization scans, 'resourcemanager.projects.list' is also required.")
    print("* All volumes are extrapolated to a 30-day period for consistent estimation.")
    print("---")


def main():
    """Main execution flow with org-level optimization."""
    print("Starting GCP Log Volume Estimator for Wiz Defend...")

    try:
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        monitoring_client = monitoring_v3.MetricServiceClient(credentials=credentials)
    except Exception as e: # pylint: disable=broad-except
        print("\nERROR: GCP authentication failed. Have you run 'gcloud auth application-default login'?", file=sys.stderr)
        log_message('ERROR', f"Authentication failed: {e}", "Authentication", args.debug)
        sys.exit(1)

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=args.log_analysis_days)
    interval = monitoring_v3.TimeInterval({"end_time": end_time, "start_time": start_time})

    all_results = []
    total_volume = 0
    successful_projects = 0

    if args.organization_id and args.org_aggregate:
        print(f"\nUsing organization-level aggregated analysis for org '{args.organization_id}'...")

        if args.use_sink_metrics:
            org_sinks = discover_org_level_sinks(args.organization_id, args.verbose, args.debug)

            if org_sinks:
                print(f"Found {len(org_sinks)} org-level sink(s) to measure.")
                org_path = f"organizations/{args.organization_id}"

                for sink_name, scope_type, _ in org_sinks:
                    sink_volume_gb = query_org_sink_metrics(
                        monitoring_client, org_path, interval,
                        sink_name, scope_type, args.verbose, args.debug
                    )

                    if sink_volume_gb > 0:
                        all_results.append({
                            'project_id': f"org-{args.organization_id}",
                            'name': f"Org Sink: {sink_name}",
                            'category': 'Total Ingestion (Actual)',
                            'volume_gb': sink_volume_gb
                        })
                        total_volume += sink_volume_gb
                        print(f"  Org sink '{sink_name}': {sink_volume_gb:.2f} GB")

                if total_volume > 0:
                    output_results(all_results, total_volume, args.log_analysis_days,
                                 1, 1, args.output_filename, args.errors_log_filename)
                    print("\nOrg-level sink measurement complete.")
                    return
                else:
                    print("No metrics found for org-level sinks. Falling back to project iteration...")
            else:
                print("No org-level sinks found. Falling back to project iteration...")

        else:
            org_metrics = query_org_metrics_aggregated(
                args.organization_id, monitoring_client, interval,
                args.verbose, args.debug
            )

            if org_metrics:
                print(f"Successfully retrieved metrics for {len(org_metrics)} projects via org aggregation.")

                for project_id, volumes in org_metrics.items():
                    workspace_volume = volumes.get("workspace_volume", 0.0)
                    combined_volumes = {
                        "cloudaudit.googleapis.com/activity": volumes.get("cloudaudit.googleapis.com/activity", {}),
                        "cloudaudit.googleapis.com/data_access": volumes.get("cloudaudit.googleapis.com/data_access", {})
                    }

                    processed_volumes = process_estimation_volumes(
                        combined_volumes, workspace_volume, args.no_exclusion_adjustment
                    )

                    for result in processed_volumes:
                        if result['volume_gb'] > 0:
                            all_results.append({
                                'project_id': project_id,
                                'name': GCP_LOG_MAPPING[result['log_key']][0],
                                'category': GCP_LOG_MAPPING[result['log_key']][1],
                                'volume_gb': result['volume_gb']
                            })
                            total_volume += result['volume_gb']

                    if any(r['volume_gb'] > 0 for r in processed_volumes):
                        successful_projects += 1

                output_results(all_results, total_volume, args.log_analysis_days,
                             len(org_metrics), successful_projects,
                             args.output_filename, args.errors_log_filename)
                print("\nOrg-level aggregated estimation complete.")
                return
            else:
                print("Org-level aggregation failed. Falling back to project iteration...")

    project_ids = get_target_projects()
    if not project_ids:
        print("\nNo projects to analyze. Exiting.", file=sys.stderr)
        if log_records:
            output_results([], 0, 0, 0, 0, args.output_filename, args.errors_log_filename)
        sys.exit(1)

    print(f"\nTime period: last {args.log_analysis_days} days")
    print(f"Concurrency: Using up to {args.workers} parallel workers")
    if args.use_sink_metrics:
        print("Method: Direct Measurement from Log Sink")
    else:
        print("Method: Log Volume Estimation")

    failed_projects = 0
    print(f"\n--- Analyzing {len(project_ids)} Project(s) ---")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_project = {executor.submit(analyze_project, pid, monitoring_client, interval): pid for pid in project_ids}

        for i, future in enumerate(concurrent.futures.as_completed(future_to_project)):
            project_id, project_results, status_message = future.result()
            print(f"({i+1}/{len(project_ids)}) Completed '{project_id}': {status_message}")

            if project_results:
                successful_projects += 1
                for result in project_results:
                    if result.get('volume_gb', 0) > 0:
                        name, category = GCP_LOG_MAPPING[result['log_key']]
                        all_results.append({
                            'project_id': project_id,
                            'name': result.get('specific_metric', name),
                            'category': category,
                            'volume_gb': result['volume_gb']
                        })
                        total_volume += result['volume_gb']
            elif "error" in status_message.lower() or "denied" in status_message.lower():
                failed_projects += 1

    output_results(all_results, total_volume, args.log_analysis_days, len(project_ids), successful_projects, args.output_filename, args.errors_log_filename)
    print("\nEstimation complete.")

if __name__ == "__main__":
    try:
        main()
    except Exception as main_exc: # pylint: disable=broad-except
        print(f"\nFATAL ERROR: An unexpected exception occurred: {main_exc}", file=sys.stderr)
        traceback.print_exc()
        if log_records:
            try:
                with open(args.errors_log_filename, 'w', encoding='utf-8') as fatal_err_file:
                    for log_entry in log_records:
                        fatal_err_file.write(log_entry + "\n")
                print(f"Issues logged before the crash were saved to: {args.errors_log_filename}")
            except (IOError, NameError):
                pass
        sys.exit(1)
