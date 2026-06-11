#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Resource Count : GCP """

# Local status: modified from the Wiz-hosted script.
# Origin: https://downloads.wiz.io/customer-files/scripts/GCP/resource-count-gcp-v2.py
# Local changes: adds project filters, bounded pagination, request timeout,
# optional checkpoints, partial output on stop/failure, resume controls,
# output directories, and Cloud Asset Inventory fallback guidance.

import argparse
import concurrent.futures
import csv
import inspect
import os
import re
import signal
import socket
import sys
import threading
import time

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import googleapiclient.discovery
    import google.auth
except ImportError:
    print("\nERROR: Missing required GCP SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade google-api-python-client")
    sys.exit(1)


version='2.8.3'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count GCP Resources')
parser.add_argument(
    '--all',
    action = 'store_true',
    dest = 'all',
    help = 'Count resources in all GCP Projects (default: disabled)',
    default = False
)
parser.add_argument(
    '--id',
    dest = 'id',
    help = 'Count resources in the specified GCP Project',
    default = None
)
parser.add_argument(
    '--projects',
    action = 'store_true',
    dest = 'input_projects',
    help = 'Count resources in the list of GCP projects (one ID per line) in a file named projects.txt (default: disabled)',
    default = False
)
parser.add_argument(
    '--exclude',
    action = 'store_true',
    dest = 'input_excluded_folders',
    help = 'Exclude folders in the list of GCP Folders (one ID per line) in a file named excluded-folders.txt (default: disabled)',
    default = False
)
parser.add_argument(
    '--data',
    action = 'store_true',
    dest = 'data_mode',
    help = 'Count Wiz Cloud Data Security (Buckets, Databases, etc) resources (default: disabled)',
    default = False
)
parser.add_argument(
    '--images',
    action = 'store_true',
    dest = 'images_mode',
    help = 'Count Wiz Cloud Registry Container Images (default: disabled)',
    default = False
)
parser.add_argument(
    '--max-image-tags',
    action = 'store',
    dest = 'max_image_tags',
    help = 'Number of image tags to count per registry image (default: 5, range 1 to 1000)',
    type = int,
    default = 5
)
parser.add_argument(
    '--max-workers',
    dest = 'max_workers',
    help = f'Maximum parallel processing requests (default: {DEFAULT_MAX_WORKERS}, range 1 to 255)',
    type = int,
    default = DEFAULT_MAX_WORKERS
)
parser.add_argument(
    '--request-timeout',
    dest = 'request_timeout',
    help = 'Socket timeout in seconds for Google API requests (default: 120)',
    type = int,
    default = 120
)
parser.add_argument(
    '--max-pages-per-request',
    dest = 'max_pages_per_request',
    help = 'Maximum paginated API pages to read per resource request (default: 1000, 0 for unlimited)',
    type = int,
    default = 1000
)
parser.add_argument(
    '--max-projects',
    dest = 'max_projects',
    help = 'Stop after scanning N projects (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--max-run-minutes',
    dest = 'max_run_minutes',
    help = 'Stop scanning after N minutes and write partial results (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--checkpoint-interval',
    dest = 'checkpoint_interval',
    help = 'Write partial output every N completed projects (default: 0, disabled)',
    type = int,
    default = 0
)
parser.add_argument(
    '--output-dir',
    dest = 'output_dir',
    help = 'Directory for output CSV and error log files (default: current directory)',
    default = '.'
)
parser.add_argument(
    '--start-after-project',
    dest = 'start_after_project',
    help = 'Skip projects until after this project ID, useful for resuming sorted --all scans',
    default = None
)
parser.add_argument(
    '--include-project-regex',
    dest = 'include_project_regex',
    help = 'Only scan projects whose ID or name matches this regular expression',
    default = None
)
parser.add_argument(
    '--exclude-project-regex',
    dest = 'exclude_project_regex',
    help = 'Skip projects whose ID or name matches this regular expression',
    default = None
)
parser.add_argument(
    '--inventory-instructions',
    action = 'store_true',
    dest = 'inventory_instructions',
    help = 'Print Cloud Asset Inventory collection suggestions and exit',
    default = False
)
parser.add_argument(
    '--debug',
    action = 'store_true',
    dest = 'debug_mode',
    help = 'Disable parallel processing and exit upon first error (default: disabled)',
    default = False
)
parser.add_argument(
    '--verbose',
    action = 'store_true',
    dest = 'verbose_mode',
    help = 'Output verbose debugging information (default: disabled)',
    default = False
)
args = parser.parse_args()

if args.max_image_tags < 1 or args.max_image_tags > 1000:
    print(f"ERROR: --max-image-tags {args.max_image_tags} out of range: [1 .. 1000]")
    sys.exit(1)
if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)
if args.request_timeout < 1:
    print("ERROR: --request-timeout must be at least 1")
    sys.exit(1)
if args.max_pages_per_request < 0:
    print("ERROR: --max-pages-per-request must be 0 or greater")
    sys.exit(1)
if args.max_projects < 0:
    print("ERROR: --max-projects must be 0 or greater")
    sys.exit(1)
if args.max_run_minutes < 0:
    print("ERROR: --max-run-minutes must be 0 or greater")
    sys.exit(1)
if args.checkpoint_interval < 0:
    print("ERROR: --checkpoint-interval must be 0 or greater")
    sys.exit(1)
try:
    include_project_pattern = re.compile(args.include_project_regex) if args.include_project_regex else None
    exclude_project_pattern = re.compile(args.exclude_project_regex) if args.exclude_project_regex else None
except re.error as ex:
    print(f"ERROR: Invalid project regex: {ex}")
    sys.exit(1)

####
# Configuration and Globals
####


excluded_folders_file = 'excluded-folders.txt'
input_file            = 'projects.txt'
output_file           = 'gcp-resources.csv'
output_file_log       = 'gcp-resources-log.csv'
error_log_file        = 'gcp-errors-log.txt'
padding = 6
run_started_at = time.monotonic()
projects_attempted = 0
projects_completed = 0

# Map command-line arguments to counts to execute and display.
enabled = {
    'Virtual Machines':             True,
    'Container Hosts':              True,
    'Serverless Functions':         True,
    'Serverless Containers':        True,

    'Data Buckets':                 args.data_mode,
    'PaaS Databases':               args.data_mode,
    'Data Warehouses':              args.data_mode,

    'Non-OS Disks':                 args.data_mode,

    'Registry Container Images':    args.images_mode,

    'Kubernetes Sensors':           True,
    'Virtual Machine Sensors':      True,
    'Serverless Container Sensors': True,
}

totals = {
    'Virtual Machines':             0,
    'Container Hosts':              0,
    'Serverless Functions':         0,
    'Serverless Containers':        0,

    'Data Buckets':                 0,
    'PaaS Databases':               0,
    'Data Warehouses':              0,

    'Non-OS Disks':                 0,
    'Registry Container Images':    0,

    'Kubernetes Sensors':           0,
    'Virtual Machine Sensors':      0,
    'Serverless Container Sensors': 0,
}

totals_log = []
errors_log = []
log_lock = threading.Lock()
totals_lock = threading.Lock()

try:
    google_auth_credential, _ = google.auth.default()
except Exception:  # pylint: disable=broad-exception-caught
    google_auth_credential = None

google_api_config = {
    'credentials': google_auth_credential,
    'num_retries': 3,
    'static_discovery': True
}

socket.setdefaulttimeout(args.request_timeout)


####
# Common Library Code
####


def signal_handler(_signal_received, _frame):
    """ Control-C """
    print("\nInterrupted. Writing partial results before exiting.")
    output_results(last_projects, partial=True)
    sys.exit(0)


def elapsed_time():
    """ Return elapsed run time """
    elapsed = int(time.monotonic() - run_started_at)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def status_print(message):
    """ Status output """
    print(f"+{elapsed_time()} {message}")


def progress_print(resource_count, resource_type, project='', region='', details=''):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type} in {project} {region} {details}".split()))
    with log_lock:
        totals_log.append([resource_type, resource_count, project, region])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details, project = ''):
    """ Error output """
    project  = f"Project: {project} " if project else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        error_type = type(details).__name__
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        error_type = 'Error'
    message = f"+{elapsed_time()} ERROR: {project}{function} {error_type}: {details}"
    print(f"\n{message}\n")
    with log_lock:
        errors_log.append(message)


def add_total(resource_type, resource_count):
    """ Add to a global total safely across worker threads """
    with totals_lock:
        totals[resource_type] += resource_count


def output_path(filename):
    """ Build an output path while keeping current-directory defaults unchanged """
    return os.path.join(args.output_dir, filename)


def close_client(client):
    """ Close a Google API client without masking the original scan result """
    try:
        if client:
            client.close()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        verbose_print(f"Error closing client: {ex}")


def max_runtime_reached():
    """ Return whether the optional runtime limit has been reached """
    if not args.max_run_minutes:
        return False
    return (time.monotonic() - run_started_at) >= args.max_run_minutes * 60


def execute_paged_request(request, list_next, item_key, project_id='', request_label='Google API request'):
    """ Execute a paginated Google API request with page guardrails """
    items = []
    page_count = 0
    while request is not None:
        page_count += 1
        if args.max_pages_per_request and page_count > args.max_pages_per_request:
            error_print(f"{request_label} exceeded --max-pages-per-request={args.max_pages_per_request}; partial data collected.", project_id)
            break
        response = request.execute(num_retries=google_api_config['num_retries'])
        page_items = response.get(item_key, [])
        if isinstance(page_items, dict):
            items.extend(page_items.values())
        else:
            items.extend(page_items)
        if 'nextPageToken' in response:
            request = list_next(previous_request=request, previous_response=response)
        else:
            request = None
    return items


def project_matches_filters(project_id, project_name):
    """ Return whether a project should be scanned """
    haystack = f"{project_id} {project_name}"
    if include_project_pattern and not include_project_pattern.search(haystack):
        return False
    if exclude_project_pattern and exclude_project_pattern.search(haystack):
        return False
    return True


def print_inventory_instructions():
    """ Print Cloud Asset Inventory fallback guidance """
    print("Cloud Asset Inventory fallback")
    print()
    print("If a full API scan is too expensive for the tenant, provide exported counts from Cloud Asset Inventory for these asset families where available:")
    print()
    print("- compute.googleapis.com/Instance")
    print("- container.googleapis.com/Cluster")
    print("- cloudfunctions.googleapis.com/CloudFunction")
    print("- run.googleapis.com/Service and run.googleapis.com/Revision")
    print("- storage.googleapis.com/Bucket")
    print("- sqladmin.googleapis.com/Instance")
    print("- spanner.googleapis.com/Instance and Spanner databases if available")
    print("- bigquery.googleapis.com/Dataset")
    print("- artifactregistry.googleapis.com/Repository and Docker images if registry images are in scope")
    print()
    print("Useful gcloud starting point:")
    print()
    print("gcloud asset search-all-resources --scope=organizations/ORG_ID --asset-types=compute.googleapis.com/Instance --format='csv(assetType,project,location,name)'")


####
# Customized Library Code
####


def tag_in_tags(tag_key, tag_value, tags):
    """ Check for tag key and value """
    if not tags:
        return False
    return tags.get(tag_key) == tag_value


def label_in_labels(label, labels):
    """ Check for label in list """
    if not labels:
        return False
    return label in labels


def get_excluded_folders_from_file():
    """ Get the list of Excluded GCP Folders """
    excluded_folders = []
    if os.path.isfile(excluded_folders_file):
        with open(excluded_folders_file, encoding='utf-8') as f:
            excluded_folders = f.read().splitlines()
    else:
        error_print(excluded_folders_file + " does not exist.")
        error_print(f"Create a file named {excluded_folders_file} and add each GCP Folder ID to exclude, one per line.")
        error_print("Exiting...")
        sys.exit()
    excluded_folders.sort()
    verbose_print(f"excluded_folders: {excluded_folders}")
    return excluded_folders


def get_gcp_enabled_services(project_id):
    """ Get the list of enabled services for the specified Project """
    gcp_enabled_services = []
    client = None
    try:
        client = googleapiclient.discovery.build('serviceusage', 'v1', **google_api_config)
        request = client.services().list(parent='projects/' + project_id, filter='state:ENABLED')
        services = execute_paged_request(
            request,
            client.services().list_next,
            'services',
            project_id,
            'Enabled services list'
        )
        for item in services:
            gcp_enabled_services.append(item['config']['name'])
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
    finally:
        close_client(client)
    gcp_enabled_services.sort()
    verbose_print(f"gcp_enabled_services: {gcp_enabled_services}")
    return gcp_enabled_services


def get_gcp_regions(project_id):
    """ Get GCP Regions for the specified Project """
    gcp_regions = []
    client = None
    try:
        client = googleapiclient.discovery.build('compute', 'v1', **google_api_config)
        request = client.regions().list(project=project_id)
        regions = execute_paged_request(
            request,
            client.regions().list_next,
            'items',
            project_id,
            'Compute regions list'
        )
        for region in regions:
            gcp_regions.append(region['name'])
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
    finally:
        close_client(client)
    #if not gcp_regions:
    #    error_print(f"No enabled regions found for Project {project_id}")
    gcp_regions.sort()
    verbose_print(f"gcp_regions: {gcp_regions}")
    return gcp_regions


# Subscriptions (aka GCP Projects)


def get_gcp_projects(excluded_folders):
    """ Get Active GCP Projects (ID, NAME) """
    gcp_projects = []
    client = None
    try:
        client = googleapiclient.discovery.build('cloudresourcemanager', 'v1', **google_api_config)
        request = client.projects().list()
        projects = execute_paged_request(
            request,
            client.projects().list_next,
            'projects',
            request_label='Projects list'
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting GCP Projects.")
        return gcp_projects
    try:
        for project in projects:
            if project['lifecycleState'] != 'ACTIVE':
                verbose_print(f"- Skipping Inactive Project {project['projectId']}")
                continue
            if 'parent' in project:
                parent_folder = project['parent']['id']
                if parent_folder in excluded_folders:
                    verbose_print(f"- Skipping Project {project['projectId']} in Excluded Folder {parent_folder}")
                    continue
            project_id = project['projectId']
            project_name = project.get('name', 'UNNAMED')
            if not project_matches_filters(project_id, project_name):
                verbose_print(f"- Skipping Project {project_id} due to project filters")
                continue
            gcp_projects.append([project_id, project_name])
    finally:
        close_client(client)
    gcp_projects = sorted(gcp_projects, key=lambda p: p[0])
    verbose_print(f"gcp_projects: {gcp_projects}")
    return gcp_projects


# Subscriptions (aka GCP Projects) from local projects.txt file


def get_gcp_projects_from_file():
    """ Get the list of GCP Projects (ID) from a file named projects.txt """
    projects_ids = []
    gcp_projects = []
    if os.path.isfile(input_file):
        with open(input_file, encoding='utf-8') as f:
            for line in f:
                if len(line.strip()) > 0:
                    projects_ids.append(line.strip())
    else:
        error_print(input_file + " does not exist.")
        error_print(f"Create a file named {input_file} and add each GCP Project ID to scan, one per line.")
        error_print("Exiting...")
        sys.exit()

    # get project names
    for project_id in projects_ids:
        client = None
        try:
            client = googleapiclient.discovery.build('cloudresourcemanager', 'v1', **google_api_config)
            request = client.projects().get(projectId=project_id)
            response = request.execute(num_retries=google_api_config['num_retries'])
            project_name = response.get('name', 'UNNAMED')
            if project_matches_filters(project_id, project_name):
                gcp_projects.append([project_id, project_name])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, project_id)
        finally:
            close_client(client)
    gcp_projects = sorted(gcp_projects, key=lambda p: p[0])
    verbose_print(f"gcp_projects: {gcp_projects}")
    return gcp_projects


# Virtual Machines: Compute Instances and Container Hosts: GKE

# pylint: disable=too-many-locals, too-many-nested-blocks, too-many-statements
def get_gce_instances_and_gke_instances(project_id, project_name):
    """ Get GCP Compute and GKE Kubernetes Instances for the specified Project """
    instances_count = 0
    gke_instances_count = 0
    non_os_disks_count = 0
    linux_instances_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('compute', 'v1', **google_api_config)
        request = client.instances().aggregatedList(project=project_id, maxResults=500)
        items = execute_paged_request(
            request,
            client.instances().aggregatedList_next,
            'items',
            project_id,
            'Compute instances aggregated list'
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    try:
        for zone_details in items:
            if 'instances' in zone_details:
                for instance in zone_details['instances']:
                    verbose_print(f"virtual_machine: {instance}")
                    if tag_in_tags('Vendor', 'Databricks', instance.get('tags', {})):
                        verbose_print(f"Skipping Databricks virtual_machine by tag: {instance['tags']}")
                        continue
                    if label_in_labels('databricks', instance.get('labels', [])):
                        verbose_print(f"Skipping Databricks virtual_machine by labels: {instance['labels']}")
                        continue
                    instances_count += 1
                    is_compute_instance = True
                    if 'labels' in instance:
                        for label in instance['labels']:
                            if label == 'goog-gke-node':
                                gke_instances_count += 1
                                is_compute_instance = False
                                break
                    # Linux Sensor and Non-OS Disks are not applicable to GKE Instances.
                    if is_compute_instance and 'disks' in instance:
                        for disk in instance['disks']:
                            verbose_print(f"disk: {disk}")
                            if disk['boot']:
                                disk_image_details = get_disk_image_details(client, project_id, disk)
                                if 'description' not in disk_image_details:
                                    disk_image_details['description'] = 'UNKNOWN'
                                if 'family' not in disk_image_details:
                                    disk_image_details['family'] = 'UNKNOWN'
                                if 'win' not in disk_image_details['description'].lower() and 'win' not in disk_image_details['family'].lower():
                                    linux_instances_count += 1
                            else:
                                non_os_disks_count += 1
    finally:
        close_client(client)

    if instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_count, resource_type='Virtual Machines [Compute]', project=project_name, details=f"with {non_os_disks_count} Non-OS Disks")
        add_total('Virtual Machines', instances_count)
        add_total('Non-OS Disks', non_os_disks_count)
        add_total('Virtual Machine Sensors', linux_instances_count)

    if gke_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=gke_instances_count, resource_type='Container Hosts [GKE]', project=project_name)
        add_total('Container Hosts', gke_instances_count)
        add_total('Kubernetes Sensors', gke_instances_count)


def get_disk_image_details(client, project_id, disk):
    """ Get Compute Disk Image Details """
    image_detail = {}
    disk_zone = disk['source'].split('/')[-3]
    disk_name = disk['source'].split('/')[-1]
    try:
        disk_detail = client.disks().get(project=project_id, zone=disk_zone, disk=disk_name).execute(num_retries=google_api_config['num_retries'])
        verbose_print(f"disk detail: {disk_detail}")
        image_name = disk_detail['sourceImage'].split('/')[-1]
        image_project = disk_detail['sourceImage'].split('/')[-4]
        image_detail = client.images().get(project=image_project, image=image_name).execute(num_retries=google_api_config['num_retries'])
        verbose_print(f"disk image: {image_detail}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
    return image_detail


# Serverless Functions: Cloud Functions


def get_gcp_cloud_functions(project_id, project_name):
    """ Get GCP Cloud Functions for the specified Project """
    serverless_functions_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('cloudfunctions', 'v2', **google_api_config)
        request = client.projects().locations().functions().list(parent='projects/' + project_id + '/locations/-')
        functions = execute_paged_request(
            request,
            client.projects().locations().functions().list_next,
            'functions',
            project_id,
            'Cloud Functions list'
        )
        serverless_functions_count = len(functions)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    finally:
        close_client(client)

    if serverless_functions_count > 0 or args.verbose_mode:
        progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [Cloud Functions]', project=project_name)
        add_total('Serverless Functions', serverless_functions_count)


# Serverless Containers: Cloud Run Revisions


def get_gcp_cloudrun_revisions(project_id, project_name):
    """ Get GCP Cloud Run Revisions for the specified Project """
    serverless_containers_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('run', 'v1', **google_api_config)
        request = client.namespaces().revisions().list(parent='namespaces/' + project_id, labelSelector='serving.knative.dev/revisionStatus=active')
        revisions = execute_paged_request(
            request,
            client.namespaces().revisions().list_next,
            'items',
            project_id,
            'Cloud Run revisions list'
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    finally:
        close_client(client)
    for item in revisions:
        for container in item.get('status', {}).get('conditions', []):
            if container.get('type') == 'ContainerHealthy' and container.get('status') == 'True':
                serverless_containers_count += 1

    if serverless_containers_count > 0 or args.verbose_mode:
        progress_print(resource_count=serverless_containers_count, resource_type='Serverless Containers [Cloud Run Revisions]', project=project_name)
        add_total('Serverless Containers', serverless_containers_count)


# Serverless Containers: GKE Autopilot

def get_gcp_gke_clusters(project_id, project_name):
    """ Get GCP Clusters for the specified Project """
    gke_nodes_count = 0
    gke_containers_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('container', 'v1', **google_api_config)
        request = client.projects().zones().clusters().list(projectId=project_id, zone='-')
        clusters = execute_paged_request(
            request,
            client.projects().zones().clusters().list_next,
            'clusters',
            project_id,
            'GKE clusters list'
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    finally:
        close_client(client)
    for cluster in clusters:
        verbose_print(f"gke_cluster: {cluster}")
        if 'autopilot' in cluster and 'enabled' in cluster['autopilot']:
            if cluster['autopilot']['enabled'] is True:
                node_pools = cluster.get('nodePools', [])
                for node_pool in node_pools:
                    node_count    = node_pool.get('currentNodeCount', node_pool.get('initialNodeCount', 0))
                    pods_per_node = node_pool.get('config', {}).get('maxPodsPerNode', 0)
                    gke_nodes_count      += node_count
                    gke_containers_count += node_count * pods_per_node

    if gke_nodes_count > 0 or args.verbose_mode:
        progress_print(resource_count=gke_nodes_count, resource_type='Kubernetes Sensors [GKE Autopilot]', project=project_name)
        add_total('Kubernetes Sensors', gke_nodes_count)

    if gke_containers_count > 0 or args.verbose_mode:
        progress_print(resource_count=gke_containers_count, resource_type='Serverless Containers [GKE Autopilot]', project=project_name)
        add_total('Serverless Containers', gke_containers_count)


# Registry Container Images: GAR
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 1000 Container Images per Container Registry


def get_gcp_gcr_images(project_id, project_name, region):
    """ Get GAR Container Images for the specified Project and Region """
    repositories = []
    container_registry_images = 0
    client = None
    try:
        client = googleapiclient.discovery.build('artifactregistry', 'v1', **google_api_config)
        request = client.projects().locations().repositories().list(parent='projects/' + project_id + '/locations/' + region)
        repository_items = execute_paged_request(
            request,
            client.projects().locations().repositories().list_next,
            'repositories',
            project_id,
            'Artifact Registry repositories list'
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    try:
        for item in repository_items:
            verbose_print(f"repository: {item}")
            if item['format'] == 'DOCKER':
                repository = item['name'].split('/')[-1]
                repositories.append(repository)
        for repository in repositories:
            container_registry_images_in_repository = 0
            try:
                request = client.projects().locations().repositories().dockerImages().list(parent='projects/' + project_id + '/locations/' + region + '/repositories/' + repository)
                docker_images = execute_paged_request(
                    request,
                    client.projects().locations().repositories().dockerImages().list_next,
                    'dockerImages',
                    project_id,
                    f'Artifact Registry docker images list for {repository}'
                )
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex, f"{project_id} region={region} repository={repository}")
                continue
            for image in docker_images:
                verbose_print(f"image: {image}")
                if 'tags' in image:
                    container_registry_images_in_repository += min(args.max_image_tags, len(image['tags']))
                else:
                    container_registry_images_in_repository += 1
            container_registry_images_in_repository = min(container_registry_images_in_repository, 10000)
            container_registry_images += container_registry_images_in_repository
    finally:
        close_client(client)

    if container_registry_images > 0 or args.verbose_mode:
        progress_print(resource_count=container_registry_images, resource_type='Registry Container Images [GAR]', project=project_name, region=region)
        add_total('Registry Container Images', container_registry_images)


# Data Buckets: Buckets
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 10000 Storage Buckets per GCP Project


def get_gcp_buckets(project_id, project_name):
    """ Get GCP Buckets for the specified Project """
    buckets_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('storage', 'v1', **google_api_config)
        request = client.buckets().list(project=project_id)
        buckets = execute_paged_request(
            request,
            client.buckets().list_next,
            'items',
            project_id,
            'Storage buckets list'
        )
        buckets_count = len(buckets)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    finally:
        close_client(client)
    buckets_count = min(buckets_count, 10000)

    if buckets_count> 0 or args.verbose_mode:
        progress_print(resource_count=buckets_count, resource_type='Data Buckets', project=project_name)
        add_total('Data Buckets', buckets_count)


# Data: PaaS Databases: Cloud SQL


def get_gcp_cloudsql_instances(project_id, project_name):
    """ Get GCP Cloud SQL Instances for the specified Project"""
    database_instances_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('sqladmin', 'v1', **google_api_config)
        request = client.instances().list(project=project_id)
        instances = execute_paged_request(
            request,
            client.instances().list_next,
            'items',
            project_id,
            'Cloud SQL instances list'
        )
        database_instances_count = len(instances)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    finally:
        close_client(client)

    if database_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=database_instances_count, resource_type='PaaS Databases [Cloud SQL]', project=project_name)
        add_total('PaaS Databases', database_instances_count)


# Data: PaaS Databases: Spanner


def get_gcp_spanner_instances(project_id, project_name):
    """ Get GCP Spanner Instances for the specified Project"""
    instances_databases_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('spanner', 'v1', **google_api_config)
        request = client.projects().instances().list(parent=f'projects/{project_id}')
        instances = execute_paged_request(
            request,
            client.projects().instances().list_next,
            'instances',
            project_id,
            'Spanner instances list'
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    try:
        for instance in instances:
            instances_databases_count += get_gcp_spanner_databases(client, instance['name'])
    finally:
        close_client(client)

    if instances_databases_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_databases_count, resource_type='PaaS Databases [Spanner]', project=project_name)
        add_total('PaaS Databases', instances_databases_count)


##


def get_gcp_spanner_databases(client, instance_id):
    """ Get GCP Spanner Databases for the specified Instance"""
    database_count = 0
    try:
        request = client.projects().instances().databases().list(parent=instance_id)
        databases = execute_paged_request(
            request,
            client.projects().instances().databases().list_next,
            'databases',
            instance_id,
            'Spanner databases list'
        )
        database_count = len(databases)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, instance_id)
        return database_count
    return database_count


# Data: Data Warehouses: BigQuery


def get_gcp_bigquery_datasets(project_id, project_name):
    """ Get GCP BigQuery Tables for the specified Project"""
    data_warehouses_count = 0
    client = None
    try:
        client = googleapiclient.discovery.build('bigquery', 'v2', **google_api_config)
        request = client.datasets().list(projectId=project_id)
        datasets = execute_paged_request(
            request,
            client.datasets().list_next,
            'datasets',
            project_id,
            'BigQuery datasets list'
        )
        data_warehouses_count = len(datasets)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    finally:
        close_client(client)

    if data_warehouses_count > 0 or args.verbose_mode:
        progress_print(resource_count=data_warehouses_count, resource_type='Data Warehouses [BigQuery]', project=project_name)
        add_total('Data Warehouses', data_warehouses_count)


####
# Main
####

# pylint: disable=too-many-branches
def get_gcp_resources(project_id, project_name):
    """ Get billable resources for the specified Project """
    exceptions = 0
    regions_list = []
    status_print(f"[SCAN] Project start: {project_id} ({project_name})")
    service_list = get_gcp_enabled_services(project_id)
    if 'compute.googleapis.com' in service_list:
        regions_list = get_gcp_regions(project_id=project_id)
    if not service_list:
        print(f"Skipping GCP Project: {project_id} no services enabled.")
        return
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        if enabled['Virtual Machines'] or enabled['Container Hosts']:
            if 'compute.googleapis.com' in service_list:
                get_gce_instances_and_gke_instances(project_id=project_id, project_name=project_name)
        if enabled['Container Hosts'] or enabled['Serverless Containers']:
            if 'container.googleapis.com' in service_list:
                get_gcp_gke_clusters(project_id=project_id, project_name=project_name)
        if enabled['Serverless Functions']:
            if 'cloudfunctions.googleapis.com' in service_list:
                get_gcp_cloud_functions(project_id=project_id, project_name=project_name)
        if enabled['Serverless Containers']:
            if 'run.googleapis.com' in service_list:
                get_gcp_cloudrun_revisions(project_id=project_id, project_name=project_name)
        if enabled['Data Buckets']:
            if 'storage.googleapis.com' in service_list:
                get_gcp_buckets(project_id=project_id, project_name=project_name)
        if enabled['PaaS Databases']:
            if 'sqladmin.googleapis.com' in service_list:
                get_gcp_cloudsql_instances(project_id=project_id, project_name=project_name)
            if 'spanner.googleapis.com' in service_list:
                get_gcp_spanner_instances(project_id=project_id, project_name=project_name)
        if enabled['Data Warehouses']:
            if 'bigquery.googleapis.com' in service_list:
                get_gcp_bigquery_datasets(project_id=project_id, project_name=project_name)
        if enabled['Registry Container Images']:
            if 'artifactregistry.googleapis.com' in service_list:
                for region in regions_list:
                    get_gcp_gcr_images(project_id=project_id, project_name=project_name, region=region)
    else:
        futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            if enabled['Virtual Machines'] or enabled['Container Hosts']:
                if 'compute.googleapis.com' in service_list:
                    futures[executor.submit(get_gce_instances_and_gke_instances, project_id=project_id, project_name=project_name)] = 'Compute instances and GKE nodes'
            if enabled['Container Hosts'] or enabled['Serverless Containers']:
                if 'container.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_gke_clusters, project_id=project_id, project_name=project_name)] = 'GKE clusters'
            if enabled['Serverless Functions']:
                if 'cloudfunctions.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_cloud_functions, project_id=project_id, project_name=project_name)] = 'Cloud Functions'
            if enabled['Serverless Containers']:
                if 'run.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_cloudrun_revisions, project_id=project_id, project_name=project_name)] = 'Cloud Run revisions'
            if enabled['Data Buckets']:
                if 'storage.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_buckets, project_id=project_id, project_name=project_name)] = 'Storage buckets'
            if enabled['PaaS Databases']:
                if 'sqladmin.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_cloudsql_instances, project_id=project_id, project_name=project_name)] = 'Cloud SQL instances'
                if 'spanner.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_spanner_instances, project_id=project_id, project_name=project_name)] = 'Spanner instances and databases'
            if enabled['Data Warehouses']:
                if 'bigquery.googleapis.com' in service_list:
                    futures[executor.submit(get_gcp_bigquery_datasets, project_id=project_id, project_name=project_name)] = 'BigQuery datasets'
            if enabled['Registry Container Images']:
                if 'artifactregistry.googleapis.com' in service_list:
                    for region in regions_list:
                        futures[executor.submit(get_gcp_gcr_images, project_id=project_id, project_name=project_name, region=region)] = f'Artifact Registry images region={region}'
            for future in concurrent.futures.as_completed(futures):
                if future.exception():
                    exceptions += 1
                    error_print(future.exception(), f"{project_id} task={futures[future]}")
    status_print(f"[DONE] Project complete: {project_id} ({len(futures) if not args.debug_mode else 'sequential'} task(s), {exceptions} exception(s))")


def output_results(projects, partial=False):
    """ Output results """
    os.makedirs(args.output_dir, exist_ok=True)
    summary_output_file = output_path(output_file)
    details_output_file = output_path(output_file_log)
    errors_output_file = output_path(error_log_file)
    with totals_lock:
        totals_snapshot = dict(totals)
    with log_lock:
        totals_log_snapshot = list(totals_log)
        errors_log_snapshot = list(errors_log)

    # Summary File
    with open(summary_output_file, 'w', encoding='utf-8', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals_snapshot.items():
            csv_writer.writerow([resource_type, resource_count])
    # Log File
    with open(details_output_file, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Project', 'Region'])
        for item in totals_log_snapshot:
            csv_writer.writerow(item)

    # Error File
    if errors_log_snapshot:
        with open(errors_output_file, 'w', encoding='utf-8') as err_file:
            for error in errors_log_snapshot:
                err_file.write(error + "\n")

    # Summary
    label = "Partial results" if partial else "Results"
    print(f"\n{label} across {len(projects)} GCP Projects (script version: {version})\n")
    if partial:
        print(f"{projects_completed} projects completed; {projects_attempted} projects attempted before interruption or failure.\n")

    if enabled['Virtual Machines']:
        print(f"{str(totals_snapshot['Virtual Machines']).rjust(padding)} Virtual Machines [Compute Instances]")
    if enabled['Container Hosts']:
        print(f"{str(totals_snapshot['Container Hosts']).rjust(padding)} Container Hosts [GKE]")
    if enabled['Serverless Functions']:
        print(f"{str(totals_snapshot['Serverless Functions']).rjust(padding)} Serverless Functions [Cloud Functions]")
    if enabled['Serverless Containers']:
        print(f"{str(totals_snapshot['Serverless Containers']).rjust(padding)} Serverless Containers [Cloud Run Revisions, GKE Autopilot]")

    if enabled['Data Buckets']:
        print()
        print(f"{str(totals_snapshot['Data Buckets']).rjust(padding)} Data Buckets (Public and Private) [Buckets]")
    if enabled['PaaS Databases']:
        print(f"{str(totals_snapshot['PaaS Databases']).rjust(padding)} PaaS Databases [Cloud SQL, Spanner]")
    if enabled['Data Warehouses']:
        print(f"{str(totals_snapshot['Data Warehouses']).rjust(padding)} Data Warehouses [BigQuery]")

    if enabled['Non-OS Disks']:
        print()
        print(f"{str(totals_snapshot['Non-OS Disks']).rjust(padding)} Non-OS Disks [Compute Instances]")
    if enabled['Registry Container Images']:
        print()
        print(f"{str(totals_snapshot['Registry Container Images']).rjust(padding)} Registry Container Images [GAR]")

    if enabled['Kubernetes Sensors']:
        print()
        print(f"{str(totals_snapshot['Kubernetes Sensors']).rjust(padding)} Kubernetes Sensors")
    if enabled['Virtual Machine Sensors']:
        print(f"{str(totals_snapshot['Virtual Machine Sensors']).rjust(padding)} Virtual Machine Sensors [Estimated from Virtual Machine Disk Image *]")

    if enabled['Virtual Machine Sensors']:
        print()
        print("* Linux Sensor counts may be lower, depending upon kernel and operating system versions")

    if not args.data_mode:
        print()
        print("To count Data Security (Buckets, Databases, etc) resources, rerun with '--data'")
    if not args.images_mode:
        print()
        print("To count Registry Container Images, rerun with '--images'")

    print(f"\nDetails written to {summary_output_file} and {details_output_file}")

    if errors_log_snapshot:
        print("\nExceptions occurred.")
        print(f"Review {errors_output_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


def main():
    """ Calculon Compute! """
    global last_projects, projects_attempted, projects_completed  # pylint: disable=global-statement
    projects = []

    if args.inventory_instructions:
        print_inventory_instructions()
        return

    excluded_folders = []
    if args.input_excluded_folders:
        print(f"Getting GCP Excluded Folders from {excluded_folders_file}\n")
        excluded_folders = get_excluded_folders_from_file()

    if args.all:
        print("Getting GCP Projects")
        projects = get_gcp_projects(excluded_folders)
        print(f"\n- Found {len(projects)} GCP Projects")
        for project in projects:
            print(f"-- {project[1]}")
        print('')
    elif args.input_projects:
        print(f"Getting GCP Projects from file: {input_file}")
        projects = get_gcp_projects_from_file()
    else:
        if args.id:
            print(f"Getting GCP Project {args.id}")
            projects = [[args.id, args.id]]
        else:
            project_id = input("Enter the GCP Project ID to scan: ")
            print('')
            projects = [[project_id, project_id]]

    if args.start_after_project:
        original_count = len(projects)
        projects = [project for project in projects if project[0] > args.start_after_project]
        print(f"\n- Resuming after Project ID {args.start_after_project}; skipped {original_count - len(projects)} projects")

    if args.max_projects:
        print(f"\n- Limiting scan to {args.max_projects} projects")
        projects = projects[:args.max_projects]

    last_projects = []
    print("\nGetting Billable Resources for each GCP Project ...")
    try:
        for index, (project_id, project_name) in enumerate(projects, start=1):
            if max_runtime_reached():
                print(f"\nReached --max-run-minutes={args.max_run_minutes}. Writing partial results before exiting.")
                output_results(last_projects, partial=True)
                return
            projects_attempted += 1
            print(f"\nScanning {project_id} ({index}/{len(projects)}) ...")
            get_gcp_resources(project_id, project_name)
            projects_completed += 1
            last_projects = projects[:index]
            if args.checkpoint_interval and projects_completed % args.checkpoint_interval == 0:
                output_results(last_projects, partial=True)
    except Exception:
        output_results(last_projects, partial=True)
        raise

    output_results(projects)


if __name__ == '__main__':
    last_projects = []
    signal.signal(signal.SIGINT,signal_handler)
    try:
        main()
    except KeyboardInterrupt:
        signal_handler(None, None)
