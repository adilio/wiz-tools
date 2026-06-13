#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines

""" Wiz : Resource Count : Azure """

import argparse
import concurrent.futures
import csv
import inspect
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import azure.mgmt.resourcegraph as az_rg
    from azure.containerregistry import ContainerRegistryClient
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.appcontainers import ContainerAppsAPIClient
    from azure.mgmt.azurestackhci import AzureStackHCIClient
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient
    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.mgmt.hybridcompute import HybridComputeManagementClient
    from azure.mgmt.sql import SqlManagementClient
    from azure.mgmt.storage import StorageManagementClient
    from azure.mgmt.subscription import SubscriptionClient
    from azure.mgmt.web import WebSiteManagementClient
    # Retained for reference - was used for Data Plane container enumeration (blocked by firewalls)
    # from azure.storage.blob import BlobServiceClient
    from msrestazure.azure_cloud import AZURE_PUBLIC_CLOUD, AZURE_US_GOV_CLOUD, AZURE_GERMAN_CLOUD, AZURE_CHINA_CLOUD

except ImportError:
    print("\nERROR: Missing required Azure SDK packages. Run the following command to install/upgrade:\n")
    print("""pip3 install --upgrade \\
    azure-mgmt-resourcegraph \\
    azure-containerregistry \\
    azure-identity \\
    azure-mgmt-appcontainers \\
    azure-mgmt-azurestackhci \\
    azure-mgmt-compute \\
    azure-mgmt-containerinstance \\
    azure-mgmt-containerregistry \\
    azure-mgmt-containerservice \\
    azure-mgmt-hybridcompute \\
    azure-mgmt-sql \\
    azure-mgmt-storage \\
    azure-mgmt-subscription \\
    azure-mgmt-web \\
    azure-storage-blob \\
    msrestazure
""")
    sys.exit(1)


version='2.8.4'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count Azure Resources')
parser.add_argument(
    '--all',
    action = 'store_true',
    dest = 'all',
    help = 'Count resources in all Azure Subscriptions in the current Management Group (default: disabled)',
    default = False
)
parser.add_argument(
    '--id',
    dest = 'id',
    help = 'Count resources in the specified Azure Subscription',
    default = None
)
parser.add_argument(
    '--subscriptions',
    action = 'store_true',
    dest = 'input_subscriptions',
    help = 'Count resources in the list of Azure subscriptions (one ID per line) in a file named subscriptions.txt (default: disabled)',
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
pgroup = parser.add_mutually_exclusive_group()
pgroup.add_argument(
    '--china',
    action = 'store_true',
    dest = 'china_mode',
    help = 'Enable AZURE_CHINA_CLOUD Mode (default: disabled)',
)
pgroup.add_argument(
    '--germany',
    action = 'store_true',
    dest = 'ger_mode',
    help = 'Enable (Experimental) AZURE_GERMAN_CLOUD Mode (default: disabled)',
)
pgroup.add_argument(
    '--gov',
    action = 'store_true',
    dest = 'gov_mode',
    help = 'Enable AZURE_US_GOV_CLOUD Mode (default: disabled)',
)
parser.add_argument(
    '--graph',
    action = 'store_true',
    dest = 'graph_mode',
    help = 'Enable (experimental) Azure Resource Graph Mode (default: disabled)',
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
parser.add_argument(
    '--output-dir',
    dest = 'output_dir',
    help = 'Directory for output CSV and error log files (default: current directory)',
    default = '.'
)
parser.add_argument(
    '--request-timeout',
    dest = 'request_timeout',
    help = 'Socket timeout in seconds for SDK requests (default: 60)',
    type = int,
    default = 60
)
parser.add_argument(
    '--max-run-minutes',
    dest = 'max_run_minutes',
    help = 'Stop scanning after N minutes and write partial results (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--max-subscriptions',
    dest = 'max_subscriptions',
    help = 'Stop after scanning N subscriptions (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--checkpoint-interval',
    dest = 'checkpoint_interval',
    help = 'Write partial output every N completed subscriptions (default: 0, disabled)',
    type = int,
    default = 0
)
parser.add_argument(
    '--start-after-subscription',
    dest = 'start_after_subscription',
    help = 'Skip subscriptions until after this subscription ID, useful for resuming sorted --all scans',
    default = None
)
parser.add_argument(
    '--include-subscription-regex',
    dest = 'include_subscription_regex',
    help = 'Only scan subscriptions whose ID or name matches this regular expression',
    default = None
)
parser.add_argument(
    '--exclude-subscription-regex',
    dest = 'exclude_subscription_regex',
    help = 'Skip subscriptions whose ID or name matches this regular expression',
    default = None
)
args = parser.parse_args()

if args.max_image_tags < 1 or args.max_image_tags > 1000:
    print(f"ERROR: --max-image-tags {args.max_image_tags} out of range: [1 .. 1000]")
    sys.exit(1)
if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)

include_subscription_pattern = re.compile(args.include_subscription_regex) if args.include_subscription_regex else None
exclude_subscription_pattern = re.compile(args.exclude_subscription_regex) if args.exclude_subscription_regex else None

####
# Configuration and Globals
####

subscriptions_file   = 'subscriptions.txt'
output_file          = 'azure-resources.csv'
output_file_log      = 'azure-resources-log.csv'
error_log_file       = 'azure-errors-log.txt'
padding = 6
sub_process_timeout  = 360

# Map command-line arguments to counts to execute and display.
enabled = {
    'Virtual Machines':             True,
    'Container Hosts':              True,
    'Serverless Functions':         True,
    'Serverless Containers':        True,
    'Asset Metadata':               True,

    'Data Buckets':                 args.data_mode,
    'PaaS Databases':               args.data_mode,
    'Data Warehouses':              args.data_mode,

    'Non-OS Disks':                 True,

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
    'Asset Metadata':               0,

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
totals_lock = threading.Lock()
log_lock = threading.Lock()
run_started_at = time.monotonic()


def add_total(key, value):
    with totals_lock:
        totals[key] += value

try:
    if args.china_mode:
        azure_credential        = DefaultAzureCredential(authority=AZURE_CHINA_CLOUD.endpoints.active_directory)
        azure_base_url          = AZURE_CHINA_CLOUD.endpoints.resource_manager
        azure_credential_scopes = [AZURE_CHINA_CLOUD.endpoints.resource_manager + '/.default']
        azure_storage_endpoint  = AZURE_CHINA_CLOUD.suffixes.storage_endpoint
    elif args.ger_mode:
        azure_credential        = DefaultAzureCredential(authority=AZURE_GERMAN_CLOUD.endpoints.active_directory)
        azure_base_url          = AZURE_GERMAN_CLOUD.endpoints.resource_manager
        azure_credential_scopes = [AZURE_GERMAN_CLOUD.endpoints.resource_manager + '/.default']
        azure_storage_endpoint  = AZURE_GERMAN_CLOUD.suffixes.storage_endpoint
    elif args.gov_mode:
        azure_credential        = DefaultAzureCredential(authority=AZURE_US_GOV_CLOUD.endpoints.active_directory)
        azure_base_url          = AZURE_US_GOV_CLOUD.endpoints.resource_manager
        azure_credential_scopes = [AZURE_US_GOV_CLOUD.endpoints.resource_manager + '/.default']
        azure_storage_endpoint  = AZURE_US_GOV_CLOUD.suffixes.storage_endpoint
    else:
        azure_credential        = DefaultAzureCredential()
        azure_base_url          = AZURE_PUBLIC_CLOUD.endpoints.resource_manager
        azure_credential_scopes = [AZURE_PUBLIC_CLOUD.endpoints.resource_manager + '/.default']
        azure_storage_endpoint  = AZURE_PUBLIC_CLOUD.suffixes.storage_endpoint
except Exception as ex0:  # pylint: disable=broad-exception-caught
    print("\nERROR: ")
    print(ex0)
    print("Unable to authenticate. Please verify your configuration")
    sys.exit(0)


####
# Common Library Code
####


def elapsed_time():
    elapsed = int(time.monotonic() - run_started_at)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def status_print(message):
    print(f"+{elapsed_time()} {message}")


def output_path(filename):
    return os.path.join(args.output_dir, filename)


def max_runtime_reached():
    if not args.max_run_minutes:
        return False
    return (time.monotonic() - run_started_at) >= args.max_run_minutes * 60


def subscription_matches_filters(subscription_id, subscription_name):
    haystack = f"{subscription_id} {subscription_name}"
    if include_subscription_pattern and not include_subscription_pattern.search(haystack):
        return False
    if exclude_subscription_pattern and exclude_subscription_pattern.search(haystack):
        return False
    return True


def signal_handler(_signal_received, _frame):
    """ Control-C """
    status_print("[INTERRUPTED] Writing partial results before exiting.")
    output_results(last_subscriptions, partial=True)
    sys.exit(0)


def progress_print(resource_count, resource_type, subscription='', details=''):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type} in {subscription} {details}".split()))
    with log_lock:
        totals_log.append([resource_type, resource_count, subscription])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details, subscription = ''):
    """ Error output """
    subscription  = f"Subscription: {subscription} " if subscription else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {subscription}{function} {details}\n")
    with log_lock:
        errors_log.append(f"ERROR: {subscription}{function} {details}")


####
# Customized Library Code
####


# pylint: disable=too-few-public-methods
class obj():
    """ Convert a dictionary to an object """
    def __init__(self, d):
        for k, v in d.items():
            if isinstance(k, (list, tuple)):
                setattr(self, k, [obj(x) if isinstance(x, dict) else x for x in v])
            else:
                setattr(self, k, obj(v) if isinstance(v, dict) else v)


# Azure Resource Graph Query
#
# Example Result: {'total_records': 1, 'count': 1, 'result_truncated': 'false', 'data': [{'example': 9}], 'facets': []}


def query_azure_resource_graph(subscription, query_string):
    """ Query the Azure Resource Graph: ARG! """
    result = []
    try:
        resource_graph_client = az_rg.ResourceGraphClient(credential=azure_credential, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        query_options = az_rg.models.QueryRequestOptions(skip_token=None)
        if subscription:
            query = az_rg.models.QueryRequest(subscriptions=[subscription.subscription_id], query=query_string, options=query_options)
        else:
            query = az_rg.models.QueryRequest(query=query_string, options=query_options)
        results = resource_graph_client.resources(query).as_dict()
        verbose_print(f"azure resource graph query: {query_string}")
        verbose_print(f"azure resource graph query: total records: {results['total_records']} count: {results['count']} result truncated: {results['result_truncated']}")
        result = results['data']
        while 'skip_token' in results:
            query_options = az_rg.models.QueryRequestOptions(skip_token=results['skip_token'])
            if subscription:
                query = az_rg.models.QueryRequest(subscriptions=[subscription.subscription_id], query=query_string, options=query_options)
            else:
                query = az_rg.models.QueryRequest(query=query_string, options=query_options)
            results = resource_graph_client.resources(query).as_dict()
            result.extend(results['data'])
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    return result


def tag_in_tags(tag_key, tag_value, tags):
    """ Check for tag key and value """
    if not tags:
        return False
    return tags.get(tag_key) == tag_value


# Arguments for libraries based on azure.core: retry_total: 10, retry_mode: 'exponential'.
# Note that individual libraries aren't obligated to support any of these arguments.

# We don't need to use nextLink with .list(), as the object returned by .list()
# is an ItemPaged iterator that will use nextLink if we loop though the returned object.
# If we want all resources in memory, we can use list(.list()).
# Note that doing so could result in out-of-memory errors at scale.


# Subscriptions (aka Azure Subscriptions)


def get_azure_subscriptions():
    """ Get Azure Subscriptions """
    subscriptions = []

    if args.graph_mode:
        query = """
        resourcecontainers
        | where type == "microsoft.resources/subscriptions"
        | order by name asc
        | project id = subscriptionId, subscription_id = subscriptionId, display_name = name
        """
        result = query_azure_resource_graph(None, query)
        for subscription in result:
            subscription_object = obj(subscription)
            subscriptions.append(subscription_object)
            verbose_print(f"subscription: {subscription}")
        return subscriptions

    try:
        subscription_client = SubscriptionClient(credential=azure_credential, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        # Wrapping .list() in list() to resolve the ItemPaged iterator and load all results.
        subscriptions = list(subscription_client.subscriptions.list())
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting Azure Subscriptions.")
    for subscription in subscriptions:
        verbose_print(f"subscription: {subscription}")
    return subscriptions


def get_azure_subscriptions_from_file():
    """ Get the list of Azure Subscriptions """
    subscriptions = []
    if os.path.isfile(subscriptions_file):
        try:
            with open(subscriptions_file, 'r', encoding='utf-8') as file:
                for line in file:
                    subscription_id = line.strip()
                    # Verify the Subscription ID.
                    if subscription_id and len(subscription_id) > 0:
                        try:
                            subscription = get_azure_subscription(subscription_id)[0]
                            subscriptions.append(subscription)
                        except Exception:  # pylint: disable=broad-exception-caught
                            print(f"Skipping invalid Azure Subscription ID from {subscriptions_file}: {subscription_id}")
                    else:
                        print(f"Skipping invalid Azure Subscription ID from {subscriptions_file}: {subscription_id}")
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
            print("Error getting Azure Subscriptions from file.")
            print("Exiting...")
            sys.exit(1)
    else:
        print("Input file does not exist.")
        print(f"Create a file named {subscriptions_file} and add each Azure Subscription ID to scan, one per line.")
        print("Exiting...")
        sys.exit(1)
    return subscriptions


def get_azure_subscription(subscription_id):
    """ Get Azure Subscription """
    subscriptions = []
    try:
        subscription_client = SubscriptionClient(credential=azure_credential, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        subscription = subscription_client.subscriptions.get(subscription_id)
        subscriptions.append(subscription)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription_id)
    for subscription in subscriptions:
        verbose_print(f"subscription: {subscription}")
    return subscriptions


# Virtual Machines: Compute VMs


def get_azure_vms(subscription):
    """ Get Azure VMs in the specified Azure Subscription """
    virtual_machines = []
    virtual_machines_count = 0
    non_os_disks_count = 0
    linux_instances_count = 0

    if args.graph_mode:
        query = """
        resources
        | where type == "microsoft.compute/virtualmachines"
        | where tags.Vendor != 'Databricks'
        | summarize count()
        """
        result = query_azure_resource_graph(subscription, query)
        virtual_machines_count = result[0]['count_']
        query = """
        resources
        | where type == "microsoft.compute/virtualmachines"
        | where tags.Vendor != 'Databricks'
        | project non_os_disks_count = iff(isnotempty(properties.storageProfile.dataDisks), array_length(properties.storageProfile.dataDisks), 0)
        | summarize sum(non_os_disks_count)
        """
        result = query_azure_resource_graph(subscription, query)
        non_os_disks_count = result[0]['sum_non_os_disks_count']
        if virtual_machines_count > 0 or args.verbose_mode:
            progress_print(resource_count=virtual_machines_count, resource_type='Virtual Machines [Compute]', subscription=subscription.display_name, details=f"with {non_os_disks_count} Non-OS Disks")
            add_total('Virtual Machines', virtual_machines_count)
            add_total('Non-OS Disks', non_os_disks_count)
        return

    try:
        compute_management_client = ComputeManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        virtual_machines = compute_management_client.virtual_machines.list_all()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for virtual_machine in virtual_machines:
        verbose_print(f"virtual_machine: {virtual_machine}")
        if virtual_machine.virtual_machine_scale_set:
            continue
        if tag_in_tags('Vendor', 'Databricks', virtual_machine.tags):
            verbose_print(f"Skipping Databricks virtual_machine: {virtual_machine.tags}")
            continue
        virtual_machines_count += 1
        if virtual_machine.os_profile and virtual_machine.os_profile.linux_configuration:
            linux_instances_count += 1
        if virtual_machine.storage_profile:
            if virtual_machine.storage_profile.data_disks:
                non_os_disks_count += len(virtual_machine.storage_profile.data_disks)

    if virtual_machines_count > 0 or args.verbose_mode:
        progress_print(resource_count=virtual_machines_count, resource_type='Virtual Machines [Compute]', subscription=subscription.display_name, details=f"with {non_os_disks_count} Non-OS Disks")
        add_total('Virtual Machines', virtual_machines_count)
        add_total('Non-OS Disks', non_os_disks_count)
        add_total('Virtual Machine Sensors', linux_instances_count)


# Virtual Machines: Scale Set VMs (Add Graph Mode Query)

# pylint: disable=too-many-locals
def get_azure_vms_scale_sets(subscription):
    """ Get Azure Scale Set VMs in the specified Azure Subscription """
    virtual_machines = []
    virtual_machines_count = 0
    non_os_disks_count = 0
    linux_instances_count = 0

    scale_sets = []
    try:
        compute_management_client = ComputeManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        scale_sets = compute_management_client.virtual_machine_scale_sets.list_all()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for scale_set in scale_sets:
        verbose_print(f"scale_set: {scale_set}")
        scale_set_resource_group_name = scale_set.id.split('/')[4]
        scale_set_non_os_disks_per_virtual_machine = 0
        scale_set_virtual_machines_count = 0
        scale_set_non_os_disks_count = 0
        if scale_set.virtual_machine_profile:
            if scale_set.virtual_machine_profile.storage_profile:
                if scale_set.virtual_machine_profile.storage_profile.data_disks:
                    scale_set_non_os_disks_per_virtual_machine = len(scale_set.virtual_machine_profile.storage_profile.data_disks)
        try:
            virtual_machines = compute_management_client.virtual_machine_scale_set_vms.list(resource_group_name=scale_set_resource_group_name, virtual_machine_scale_set_name=scale_set.name)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, subscription.display_name)
        # Loop through the ItemPaged response.
        for virtual_machine in virtual_machines:
            verbose_print(f"virtual_machine: {virtual_machine}")
            if tag_in_tags('Vendor', 'Databricks', virtual_machine.tags):
                verbose_print(f"Skipping Databricks virtual_machine: {virtual_machine.tags}")
                continue
            scale_set_virtual_machines_count += 1
            virtual_machines_count += 1
            if virtual_machine.os_profile and virtual_machine.os_profile.linux_configuration:
                linux_instances_count += 1
            if not scale_set.virtual_machine_profile:
                # No Virtual Machine / Scaling Profile: Virtual Machine manually attached after Scale Set deployment.
                virtual_machine_non_os_disks_count = 0
                try:
                    virtual_machine_detail = compute_management_client.virtual_machines.get(resource_group_name=scale_set_resource_group_name, vm_name=virtual_machine.name)
                    verbose_print(f"virtual_machine_detail: {virtual_machine_detail}")
                    if virtual_machine_detail.storage_profile:
                        if virtual_machine_detail.storage_profile.data_disks:
                            virtual_machine_non_os_disks_count = len(virtual_machine_detail.storage_profile.data_disks)
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    error_print(ex, subscription.display_name)
                scale_set_non_os_disks_count += virtual_machine_non_os_disks_count
        if scale_set.virtual_machine_profile:
            scale_set_non_os_disks_count = scale_set_non_os_disks_per_virtual_machine * scale_set_virtual_machines_count
        non_os_disks_count += scale_set_non_os_disks_count

    if virtual_machines_count > 0 or args.verbose_mode:
        progress_print(resource_count=virtual_machines_count, resource_type='Virtual Machines [Scale Sets]', subscription=subscription.display_name, details=f"with {non_os_disks_count} Non-OS Disks")
        add_total('Virtual Machines', virtual_machines_count)
        add_total('Non-OS Disks', non_os_disks_count)
        add_total('Virtual Machine Sensors', linux_instances_count)


# Container Hosts: AKS


def get_azure_aks_container_instances(subscription):
    """ Get Azure AKS Hosts in the specified Azure Subscription """
    managed_clusters = []
    aks_instances_count = 0

    if args.graph_mode:
        query = """
        resources
        | where type == "microsoft.containerservice/managedclusters"
        | mv-expand pool = properties.agentPoolProfiles
        | summarize aks_instances_count = sum(toint(pool["count"]))
        | project sum_aks_instances_count = aks_instances_count
        """
        result = query_azure_resource_graph(subscription, query)
        aks_instances_count = result[0]['sum_aks_instances_count']
        if aks_instances_count > 0 or args.verbose_mode:
            progress_print(resource_count=aks_instances_count, resource_type='Container Hosts [AKS]', subscription=subscription.display_name)
            add_total('Container Hosts', aks_instances_count)
            add_total('Kubernetes Sensors', aks_instances_count)
        return

    try:
        container_service_client = ContainerServiceClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        managed_clusters = container_service_client.managed_clusters.list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for managed_cluster in managed_clusters:
        verbose_print(f"managed_cluster: {managed_cluster}")
        for pool in (managed_cluster.agent_pool_profiles or []):
            aks_instances_count += pool.count or 0

    if aks_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=aks_instances_count, resource_type='Container Hosts', subscription=subscription.display_name)
        add_total('Container Hosts', aks_instances_count)
        add_total('Kubernetes Sensors', aks_instances_count)


# Serverless Containers: Azure Container Instances (ACI)


def get_azure_container_instances(subscription):
    """ Get Azure Container Instances in the specified Azure Subscription """
    container_instances = []
    container_instances_count = 0
    try:
        container_instances_client = ContainerInstanceManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        container_instances = container_instances_client.container_groups.list()

        for container_instance in container_instances:
            verbose_print(f"container_instance: {container_instance}")
            container_instances_count += 1

    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)

    if container_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=container_instances_count, resource_type='Serverless Containers [Azure Container Instances]', subscription=subscription.display_name)
        add_total('Serverless Containers', container_instances_count)


# Serverless Containers: Azure Container Apps


def get_azure_container_apps(subscription):
    """ Get Azure Container Apps in the specified Azure Subscription """
    container_apps = []
    container_apps_count = 0
    try:
        container_apps_client = ContainerAppsAPIClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        container_apps = container_apps_client.container_apps.list_by_subscription()

        for container_app in container_apps:
            verbose_print(f"container_app: {container_app}")
            container_apps_count += 1

    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)

    if container_apps_count > 0 or args.verbose_mode:
        progress_print(resource_count=container_apps_count, resource_type='Serverless Containers [Azure Container Apps]', subscription=subscription.display_name)
        add_total('Serverless Containers', container_apps_count)
        add_total('Serverless Container Sensors', container_apps_count)


# Serverless Functions: Web Apps


def get_azure_functions_web_apps(subscription):
    """ Get Azure Functions in the specified Azure Subscription """
    serverless_functions = []
    serverless_functions_count = 0

    if args.graph_mode:
        query = """
        resources
        | where type == "microsoft.web/sites"
        | summarize count()
        """
        result = query_azure_resource_graph(subscription, query)
        serverless_functions_count += result[0]['count_']
        query = """
        resources
        | where type == "microsoft.web/staticsites"
        | summarize count()
        """
        result = query_azure_resource_graph(subscription, query)
        serverless_functions_count += result[0]['count_']
        if serverless_functions_count > 0 or args.verbose_mode:
            progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [Web Apps]', subscription=subscription.display_name)
            add_total('Serverless Functions', serverless_functions_count)
        return

    try:
        web_site_management_client = WebSiteManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        web_apps = web_site_management_client.web_apps.list()
        for web_app in web_apps:
            serverless_functions_count += 1
            if 'functionapp' not in web_app.kind:
                continue
            child_functions = web_site_management_client.web_apps.list_functions(web_app.resource_group, web_app.name)
            # pylint: disable=unused-variable
            for function in child_functions:
                serverless_functions_count += 1
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for serverless_function in serverless_functions:
        verbose_print(f"serverless_function: {serverless_function}")
        serverless_functions_count += 1

    if serverless_functions_count > 0 or args.verbose_mode:
        progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [Web Apps]', subscription=subscription.display_name)
        add_total('Serverless Functions', serverless_functions_count)


# Serverless Functions: App Service Plans (Disabled: Double counts Web Apps)


def get_azure_functions_web_apps_app_service_plans(subscription):
    """ Get Azure App Services in the specified Azure Subscription """
    serverless_functions = []
    serverless_functions_count = 0

    if args.graph_mode:
        query = """
        resources
        | where type == "microsoft.web/serverfarms"
        | summarize count()
        """
        result = query_azure_resource_graph(subscription, query)
        serverless_functions_count = result[0]['count_']
        if serverless_functions_count > 0 or args.verbose_mode:
            progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [App Service Plans]', subscription=subscription.display_name)
            add_total('App Service Plan Serverless Functions', serverless_functions_count)
        return

    try:
        web_site_management_client = WebSiteManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        serverless_functions = web_site_management_client.app_service_plans.list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for serverless_function in serverless_functions:
        verbose_print(f"serverless_function: {serverless_function}")
        serverless_functions_count += 1

    if serverless_functions_count > 0 or args.verbose_mode:
        progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [App Service Plans]', subscription=subscription.display_name)
        add_total('Serverless Functions', serverless_functions_count)


# Registry Container Images: ACR
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 1000 Container Images per ACR Repository
# args.max_image_tags is already lower.


# pylint: disable=too-many-statements
def get_azure_acr_images(subscription):
    """ Get Azure Registry Container Images in the specified Azure Subscription """
    # https://github.com/Azure/azure-sdk-for-python/blob/azure-containerregistry_1.0.0b2/sdk/containerregistry/azure-containerregistry/azure/containerregistry/_container_registr>
    container_registry_images_count = 0

    # Avoid "Audience https://containerregistry.azure.net is not a supported MSI token audience" error, specific to Azure CloudShell.
    if os.environ.get('AZD_IN_CLOUDSHELL'):
        # Use an Azure Resource Graph query to minimize the use of subprocesses.
        # registries = subprocess.check_output('az acr list --query "[].{name:name}" --output tsv', shell=True, text=True, timeout=sub_process_timeout)
        query = """
        resources
        | where type == "microsoft.containerregistry/registries"
        | project name
        """
        registries = query_azure_resource_graph(subscription, query)
        for r, registry in enumerate(registries):
            registries[r] = registry['name']
        for registry in registries:
            verbose_print(f"registry: {registry}")
            try:
                repositories = subprocess.check_output(f'az acr repository list --name {registry} --output tsv', shell=True, text=True, timeout=sub_process_timeout)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex, subscription.display_name)
                continue
            for repository in repositories.splitlines():
                verbose_print(f"repository: {repository}")
                try:
                    tags = subprocess.check_output(f'az acr repository show-tags --name {registry} --repository {repository} --query "length(@)" --output tsv', shell=True, text=True, timeout=sub_process_timeout)
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    error_print(ex, subscription.display_name)
                    continue
                tags_count = max(1, int(tags))
                container_registry_images_count += min(args.max_image_tags, tags_count)

        if container_registry_images_count > 0 or args.verbose_mode:
            progress_print(resource_count=container_registry_images_count, resource_type='Registry Container Images [ACR]', subscription=subscription.display_name)
            add_total('Registry Container Images', container_registry_images_count)
        return

    registries = []
    try:
        registry_management_client = ContainerRegistryManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        registries = registry_management_client.registries.list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for registry in registries:
        verbose_print(f"registry: {registry}")
        try:
            endpoint = registry.login_server
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, subscription.display_name)
            continue # next registry loop
        try:
            registry_client = ContainerRegistryClient(endpoint, azure_credential)
            repositories = registry_client.list_repository_names()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, subscription.display_name)
            continue  # next registry loop
        # Loop through the ItemPaged response.
        for repository in repositories:
            verbose_print(f"repository: {repository}")
            try:
                repository_properties = registry_client.get_repository_properties(repository)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex, subscription.display_name)
                continue  # next repository in registry loop
            container_registry_images_count += min(args.max_image_tags, repository_properties.tag_count)

    if container_registry_images_count > 0 or args.verbose_mode:
        progress_print(resource_count=container_registry_images_count, resource_type='Registry Container Images [ACR]', subscription=subscription.display_name)
        add_total('Registry Container Images', container_registry_images_count)


# Data Buckets: Storage Containers
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 10000 Blob Storage Containers per Storage Account


def get_azure_storage_containers(subscription):
    """ Get Azure Storage Containers in the specified Azure Subscription """
    accounts = []
    bucket_count = 0

    try:
        storage_management_client = StorageManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        accounts = storage_management_client.storage_accounts.list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for account in accounts:
        verbose_print(f"account: {account}")
        if tag_in_tags('application', 'Databricks', account.tags):
            verbose_print(f"Skipping Databricks storage_account: {account.tags}")
            continue
        if tag_in_tags('databricks-environment', 'true', account.tags):
            verbose_print(f"Skipping Databricks storage_account: {account.tags}")
            continue
        # Extract resource group from account ID
        # Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{name}
        resource_group_name = account.id.split('/')[4]
        containers = []
        try:
            # Use Control Plane (ARM) to list containers - bypasses storage account firewall
            containers = storage_management_client.blob_containers.list(
                resource_group_name=resource_group_name,
                account_name=account.name
            )
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, subscription.display_name)
        container_count = 0
        # Loop through the ItemPaged response.
        for container in containers:
            verbose_print(f"container: {container}")
            if container_count > 10000:
                break
            container_count += 1
            bucket_count += 1

    if bucket_count > 0 or args.verbose_mode:
        progress_print(resource_count=bucket_count, resource_type='Data Buckets [Storage Containers]', subscription=subscription.display_name)
        add_total('Data Buckets', bucket_count)


# Data in PaaS Databases: Azure SQL


def get_azure_sql_servers(subscription):
    """ Get Azure SQL Servers in the specified Azure Subscription """
    sql_servers = []
    sql_databases = []
    sql_databases_count = 0

    if args.graph_mode:
        query = """
        resources
        | where type == "microsoft.sql/servers/databases"
        | summarize count()
        """
        result = query_azure_resource_graph(subscription, query)
        sql_databases_count = result[0]['count_']
        if sql_databases_count > 0 or args.verbose_mode:
            progress_print(resource_count=sql_databases_count, resource_type='PaaS Databases [SQL]', subscription=subscription.display_name)
            add_total('PaaS Databases', sql_databases_count)
        return

    try:
        sql_management_client = SqlManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        sql_servers = sql_management_client.servers.list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for sql_server in sql_servers:
        verbose_print(f"sql_server: {sql_server}")
        resource_group = sql_server.id.split('/')[4]
        try:
            sql_databases = sql_management_client.databases.list_by_server(resource_group, sql_server.name)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, subscription.display_name)
        # Loop through the ItemPaged response.
        for sql_database in sql_databases:
            verbose_print(f"sql_database: {sql_database}")
            if sql_database.name == 'master':
                continue
            sql_databases_count += 1

    if sql_databases_count > 0 or args.verbose_mode:
        progress_print(resource_count=sql_databases_count, resource_type='PaaS Databases [SQL]', subscription=subscription.display_name)
        add_total('PaaS Databases', sql_databases_count)


# Asset Metadata: Azure Arc Machines
# https://learn.microsoft.com/en-us/rest/api/hybridcompute/machines/list-by-subscription


def get_azure_arc_machines(subscription):
    """ Get Azure Arc Machines in the specified Azure Subscription """
    machines = []
    machines_count = 0

    try:
        hybrid_compute_managementClient = HybridComputeManagementClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        machines = hybrid_compute_managementClient.machines.list_by_subscription()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for machine in machines:
        verbose_print(f"machine: {machine}")
        machines_count += 1

    if machines_count > 0 or args.verbose_mode:
        progress_print(resource_count=machines_count, resource_type='Asset Metadata [Arc Machines]', subscription=subscription.display_name)
        add_total('Asset Metadata', machines_count)


# Asset Metadata: Azure Stack HCI Clusters
# https://learn.microsoft.com/en-us/rest/api/stackhci/clusters/list-by-subscription


def get_azure_stack_hci_clusters(subscription):
    """ Get Azure Stack HCI Clusters in the specified Azure Subscription """
    clusters = []
    clusters_count = 0

    try:
        stack_hci_client = AzureStackHCIClient(azure_credential, subscription.subscription_id, base_url=azure_base_url, credential_scopes=azure_credential_scopes)
        clusters = stack_hci_client.clusters.list_by_subscription()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, subscription.display_name)
    # Loop through the ItemPaged response.
    for cluster in clusters:
        verbose_print(f"cluster: {cluster}")
        clusters_count += 1

    if clusters_count > 0 or args.verbose_mode:
        progress_print(resource_count=clusters_count, resource_type='Asset Metadata [Stack HCI Clusters]', subscription=subscription.display_name)
        add_total('Asset Metadata', clusters_count)


####
# Main
####


def get_azure_resources(subscription):
    """ Get billable resources """
    exceptions = 0
    subscription_id = subscription.subscription_id
    status_print(f"[SCAN] Subscription start: {subscription_id} ({subscription.display_name})")
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        if enabled['Virtual Machines']:
            get_azure_vms(subscription)
            get_azure_vms_scale_sets(subscription)
        if enabled['Container Hosts']:
            get_azure_aks_container_instances(subscription)
        if enabled['Serverless Functions']:
            get_azure_functions_web_apps(subscription)
        if enabled['Serverless Containers']:
            get_azure_container_instances(subscription)
            get_azure_container_apps(subscription)
        if enabled['Asset Metadata']:
            get_azure_arc_machines(subscription)
            get_azure_stack_hci_clusters(subscription)
        if enabled['Data Buckets']:
            get_azure_storage_containers(subscription)
        if enabled['PaaS Databases']:
            get_azure_sql_servers(subscription)
        if enabled['Registry Container Images']:
            get_azure_acr_images(subscription)
    else:
        futures = {}
        failed_tasks = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            if enabled['Virtual Machines']:
                futures[executor.submit(get_azure_vms, subscription)] = 'Virtual Machines'
                futures[executor.submit(get_azure_vms_scale_sets, subscription)] = 'Virtual Machine Scale Sets'
            if enabled['Container Hosts']:
                futures[executor.submit(get_azure_aks_container_instances, subscription)] = 'AKS Container Instances'
            if enabled['Serverless Functions']:
                futures[executor.submit(get_azure_functions_web_apps, subscription)] = 'Functions/Web Apps'
            if enabled['Serverless Containers']:
                futures[executor.submit(get_azure_container_instances, subscription)] = 'Container Instances'
                futures[executor.submit(get_azure_container_apps, subscription)] = 'Container Apps'
            if enabled['Asset Metadata']:
                futures[executor.submit(get_azure_arc_machines, subscription)] = 'Arc Machines'
                futures[executor.submit(get_azure_stack_hci_clusters, subscription)] = 'Stack HCI Clusters'
            if enabled['Data Buckets']:
                futures[executor.submit(get_azure_storage_containers, subscription)] = 'Storage Containers'
            if enabled['PaaS Databases']:
                futures[executor.submit(get_azure_sql_servers, subscription)] = 'SQL Servers'
            if enabled['Registry Container Images']:
                futures[executor.submit(get_azure_acr_images, subscription)] = 'ACR Images'
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                failed_tasks += 1
                error_print(future.exception(), f"{subscription.display_name} task={futures[future]}")
        if failed_tasks:
            error_print(f"{failed_tasks} task(s) failed for subscription {subscription.display_name}")
    status_print(f"[DONE] Subscription complete: {subscription_id} ({len(futures) if not args.debug_mode else 'sequential'} task(s), {failed_tasks} exception(s))")


def output_results(subscriptions, partial=False):
    """ Output results """
    with totals_lock:
        totals_snapshot = dict(totals)
    with log_lock:
        totals_log_snapshot = list(totals_log)
        errors_log_snapshot = list(errors_log)
    os.makedirs(args.output_dir, exist_ok=True)
    # Summary File
    with open(output_path(output_file), 'w', encoding='utf-8', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals_snapshot.items():
            csv_writer.writerow([resource_type, resource_count])
    # Log File
    with open(output_path(output_file_log), 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Subscription'])
        for item in totals_log_snapshot:
            csv_writer.writerow(item)

    # Error File
    if errors_log_snapshot:
        with open(output_path(error_log_file), 'w', encoding='utf-8') as err_file:
            for error in errors_log_snapshot:
                err_file.write(error + "\n")

    # Summary
    label = "Partial results" if partial else "Results"
    print(f"\n{label} across {len(subscriptions)} Azure Subscriptions (script version: {version})\n")
    if partial:
        print("Scan interrupted; results above cover completed subscriptions only.\n")

    if enabled['Virtual Machines']:
        print(f"{str(totals_snapshot['Virtual Machines']).rjust(padding)} Virtual Machines [Compute, Scale Sets]")
    if enabled['Container Hosts']:
        print(f"{str(totals_snapshot['Container Hosts']).rjust(padding)} Container Hosts [AKS]")
    if enabled['Serverless Functions']:
        print(f"{str(totals_snapshot['Serverless Functions']).rjust(padding)} Serverless Functions [Web Apps]")
    if enabled['Serverless Containers']:
        print(f"{str(totals_snapshot['Serverless Containers']).rjust(padding)} Serverless Containers [Container Instances, Container Apps]")
    if enabled['Asset Metadata']:
        print(f"{str(totals_snapshot['Asset Metadata']).rjust(padding)} Asset Metadata [Arc Machines, Stack HCI Clusters]")

    if enabled['Data Buckets']:
        print()
        print(f"{str(totals_snapshot['Data Buckets']).rjust(padding)} Data Buckets (Public and Private) [Storage Containers]")
    if enabled['PaaS Databases']:
        print(f"{str(totals_snapshot['PaaS Databases']).rjust(padding)} PaaS Databases [SQL]")

    if enabled['Non-OS Disks']:
        print()
        print(f"{str(totals_snapshot['Non-OS Disks']).rjust(padding)} Non-OS Disks [Compute]")
    if enabled['Registry Container Images']:
        print()
        print(f"{str(totals_snapshot['Registry Container Images']).rjust(padding)} Registry Container Images [ACR]")

    if enabled['Kubernetes Sensors']:
        print()
        print(f"{str(totals_snapshot['Kubernetes Sensors']).rjust(padding)} (Potential) Kubernetes Sensors [Estimated from Platform]")
    if enabled['Virtual Machine Sensors']:
        print(f"{str(totals_snapshot['Virtual Machine Sensors']).rjust(padding)} (Potential) Virtual Machine Sensors [Estimated from VM Platform *]")
    if enabled['Serverless Container Sensors']:
        print(f"{str(totals_snapshot['Serverless Container Sensors']).rjust(padding)} (Potential) Serverless Container Sensors [Container Apps]")

    if enabled['Virtual Machine Sensors']:
        print()
        print("* Linux Sensor counts may be lower, depending upon kernel and operating system versions")

    if not args.data_mode:
        print()
        print("To count Data Security (Buckets, Databases, etc) resources, rerun with '--data'")
    if not args.images_mode:
        print()
        print("To count Registry Container Images, rerun with '--images'")

    print(f"\nDetails written to {output_file} and {output_file_log}")

    if errors_log_snapshot:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon error.")


def main():
    """ Calculon Compute! """
    global last_subscriptions  # pylint: disable=global-statement
    socket.setdefaulttimeout(args.request_timeout)
    subscriptions = []

    if args.all:
        print("Getting Azure Subscriptions")
        subscriptions = get_azure_subscriptions()
        print(f"\nFound {len(subscriptions)} Azure Subscription(s)")
        for subscription in subscriptions:
            subscription_id = subscription.id.rsplit('/', 1)[-1]
            print(f"-- {subscription_id} - {subscription.display_name}")
        print('')
    elif args.input_subscriptions:
        subscriptions = get_azure_subscriptions_from_file()
        print(f"\nFound {len(subscriptions)} Subscriptions:")
    else:
        if args.id:
            print(f"\nGetting Azure Subscription {args.id}")
            subscription_id = args.id
        else:
            subscription_id = input("Enter the Azure Subscription ID to scan: ")
            print('')
        subscriptions = get_azure_subscription(subscription_id)

    if not subscriptions:
        print("No Subscriptions found.")
        print("Exiting...")
        sys.exit(1)

    last_subscriptions = []
    filtered_subscriptions = [s for s in subscriptions if s.display_name != 'Access to Azure Active Directory']
    print("\nGetting Billable Resources for each Azure Subscription ...")
    past_start_after = not args.start_after_subscription
    scanned_count = 0
    try:
        for index, subscription in enumerate(filtered_subscriptions, start=1):
            subscription_id = subscription.id.rsplit('/', 1)[-1]
            if not past_start_after:
                if subscription_id == args.start_after_subscription:
                    past_start_after = True
                else:
                    status_print(f"[SKIP] Subscription {index}/{len(filtered_subscriptions)}: {subscription_id} (before --start-after-subscription)")
                continue
            if not subscription_matches_filters(subscription_id, subscription.display_name):
                status_print(f"[SKIP] Subscription {index}/{len(filtered_subscriptions)}: {subscription_id} - {subscription.display_name}")
                continue
            if max_runtime_reached():
                status_print(f"[STOP] Max runtime of {args.max_run_minutes}m reached after {scanned_count} subscription(s).")
                output_results(last_subscriptions, partial=True)
                return
            if args.max_subscriptions and scanned_count >= args.max_subscriptions:
                status_print(f"[STOP] Reached --max-subscriptions {args.max_subscriptions}.")
                output_results(last_subscriptions, partial=True)
                return
            status_print(f"[SCAN] Subscription {index}/{len(filtered_subscriptions)}: {subscription_id} - {subscription.display_name}")
            get_azure_resources(subscription)
            last_subscriptions.append(subscription)
            scanned_count += 1
            if args.checkpoint_interval and scanned_count % args.checkpoint_interval == 0:
                status_print(f"[CHECKPOINT] {scanned_count} subscription(s) complete.")
                output_results(last_subscriptions, partial=True)
    except Exception:
        output_results(last_subscriptions, partial=True)
        raise

    output_results(last_subscriptions)


if __name__ == "__main__":
    last_subscriptions = []
    signal.signal(signal.SIGINT,signal_handler)
    main()
