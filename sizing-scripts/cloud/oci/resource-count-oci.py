#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Resource Count : OCI """

# pip3 install oci

import argparse
import concurrent.futures
import csv
import inspect
import json
import os
import re
import signal
import sys
import threading
import time

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import oci
except ImportError:
    print("\nERROR: Missing required OCI SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade oci")
    sys.exit(1)


version='2.8.0'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count OCI Resources')
parser.add_argument(
    '--data',
    action = 'store_true',
    dest = 'data_mode',
    help = 'Count Data Security (Buckets, etc) resources (default: disabled)',
    default = False
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
    '--max-run-minutes',
    dest = 'max_run_minutes',
    help = 'Stop scanning after N minutes and write partial results (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--max-compartments',
    dest = 'max_compartments',
    help = 'Stop after scanning N compartments (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--checkpoint-interval',
    dest = 'checkpoint_interval',
    help = 'Write partial output every N completed compartments (default: 0, disabled)',
    type = int,
    default = 0
)
parser.add_argument(
    '--start-after-compartment',
    dest = 'start_after_compartment',
    help = 'Skip compartments until after this compartment ID, useful for resuming sorted scans',
    default = None
)
parser.add_argument(
    '--include-compartment-regex',
    dest = 'include_compartment_regex',
    help = 'Only scan compartments whose ID or name matches this regular expression',
    default = None
)
parser.add_argument(
    '--exclude-compartment-regex',
    dest = 'exclude_compartment_regex',
    help = 'Skip compartments whose ID or name matches this regular expression',
    default = None
)
args = parser.parse_args()

if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)

include_compartment_pattern = re.compile(args.include_compartment_regex) if args.include_compartment_regex else None
exclude_compartment_pattern = re.compile(args.exclude_compartment_regex) if args.exclude_compartment_regex else None


####
# Configuration and Globals
####


delegation_token_file = '/etc/oci/delegation_token'
output_file           = 'oci-resources.csv'
output_file_log       = 'oci-resources-log.csv'
error_log_file        = 'oci-errors-log.txt'
padding = 6

# Map command-line arguments to counts to execute and display.
enabled = {
    'Virtual Machines':        True,
    'Container Hosts':         True,
    'Serverless Functions':    True,

    'Data Buckets':            args.data_mode,

    'Kubernetes Sensors':      True,
    'Virtual Machine Sensors': True,
}

totals = {
    'Virtual Machines':         0,
    'Container Hosts':          0,
    'Serverless Functions':     0,
    'Serverless Containers':    0,

    'Data Buckets':             0,

    'Kubernetes Sensors':       0,
    'Virtual Machine Sensors':  0,
}

totals_log = []
errors_log = []

_image_os_cache = {}
_image_os_cache_lock = threading.Lock()
run_started_at = time.monotonic()


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


def compartment_matches_filters(compartment_id, compartment_name):
    haystack = f"{compartment_id} {compartment_name}"
    if include_compartment_pattern and not include_compartment_pattern.search(haystack):
        return False
    if exclude_compartment_pattern and exclude_compartment_pattern.search(haystack):
        return False
    return True


def signal_handler(_signal_received, _frame):
    """ Control-C """
    status_print("[INTERRUPTED] Writing partial results before exiting.")
    output_results(last_compartments, partial=True)
    sys.exit(0)


def progress_print(resource_count, resource_type, region=''):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type} in {region}".split()))
    totals_log.append([resource_type, resource_count, region])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details, compartment = ''):
    """ Error output """
    compartment  = f"Compartment: {compartment} " if compartment else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {compartment}{function} {details}\n")
    errors_log.append(f"ERROR: {compartment}{function} {details}")

####
# Customized Library Code
####


# Subscriptions (aka OCI Compartments)


def get_oci_compartments(config, signer):
    """ Get a list of OCI Compartments """
    try:
        compartments = []
        identity_client = oci.identity.IdentityClient(config=config, signer=signer, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
        get_compartment_response = identity_client.get_compartment(compartment_id=config['tenancy'])
        root_compartment = json.loads(str(get_compartment_response.data))
        root_compartment['compartment_id'] = root_compartment['id']
        compartments.append(root_compartment)
        verbose_print(f"root_compartment: {root_compartment}")
        response = identity_client.list_compartments(compartment_id=config['tenancy'], compartment_id_in_subtree=True, limit=1000)
        verbose_print(f"compartments: {response.data}")
        compartments.extend(json.loads(str(response.data)))
        while response.has_next_page:
            response = identity_client.list_compartments(compartment_id=config['tenancy'], compartment_id_in_subtree=True, limit=1000, page=response.next_page)
            verbose_print(f"compartments: {response.data}")
            compartments.extend(json.loads(str(response.data)))
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting OCI Compartments.")
        error_print("Exiting...")
        sys.exit()
    verbose_print(f"compartments: {compartments}")
    return compartments


def get_oci_regions(config, signer):
    """ Get a list of OCI Regions """
    try:
        identity_client = oci.identity.IdentityClient(config=config, signer=signer, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
        list_regions_response = identity_client.list_region_subscriptions(tenancy_id=config['tenancy'])
        regions = json.loads(str(list_regions_response.data))
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting OCI Regions.")
        error_print("Exiting...")
        sys.exit(1)
    verbose_print(f"regions: {regions}")
    return regions


def config_for_region(config, region):
    """ Create a copy of the OCI config with the specified region (never mutate the shared config). """
    region_config = dict(config) if config else {}
    region_config['region'] = region['region_key']
    return region_config


# Virtual Machines: Compute Instances and Container Hosts: OKE


def get_oci_instances_and_oke_instances(config, signer, compartment, regions):
    """ Get OCI Compute Instances and OKE Instances """
    for region in regions:
        instances = []
        instances_count = 0
        container_instances_count = 0
        linux_instances_count = 0
        try:
            search_client = oci.resource_search.ResourceSearchClient(config=config_for_region(config, region), signer=signer, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            response = search_client.search_resources(
                oci.resource_search.models.StructuredSearchDetails(
                    type="Structured",
                   query=f"query instance resources return allAdditionalFields where compartmentId = '{compartment['id']}' && lifeCycleState != 'TERMINATED' && lifeCycleState != 'TERMINATING'")
            )
            verbose_print(f"instances: {response.data}")
            instances = json.loads(str(response.data))['items']
            while response.has_next_page:
                response = search_client.search_resources(
                    oci.resource_search.models.StructuredSearchDetails(
                        type="Structured",
                        query=f"query instance resources return allAdditionalFields where compartmentId = '{compartment['id']}' && lifeCycleState != 'TERMINATED' && lifeCycleState != 'TERMINATING'"),
                    page=response.next_page
                )
                verbose_print(f"instances: {response.data}")
                instances.extend(json.loads(str(response.data))['items'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(f"Exception getting Instances in Region: {region}: {ex}", compartment['id'])
        core_client = oci.core.ComputeClient(config=config_for_region(config, region), signer=signer, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
        for instance in instances:
            verbose_print(f"virtual_machine: {instance}")
            instances_count += 1
            if instance.get('defined_tags', {}).get('Oracle-Tags', {}).get('CreatedBy') == 'oke':
                container_instances_count += 1
            operating_system = get_oci_image_operating_system(core_client, instance['additional_details']['imageId'])
            if operating_system and 'win' not in operating_system.lower():
                linux_instances_count += 1

        if instances_count > 0 or args.verbose_mode:
            progress_print(resource_count=instances_count, resource_type='Virtual Machines [Compute]', region=region['region_name'])
            totals['Virtual Machines'] += instances_count
            totals['Virtual Machine Sensors'] += linux_instances_count

        if container_instances_count > 0 or args.verbose_mode:
            progress_print(resource_count=container_instances_count, resource_type='Container Hosts [OKE]', region=region['region_name'])
            totals['Container Hosts'] += container_instances_count
            totals['Kubernetes Sensors'] += container_instances_count


def get_oci_image_operating_system(core_client, image_id):
    """ Get OCI Compute Image operating system, cached by image_id to avoid N+1 API calls. """
    with _image_os_cache_lock:
        if image_id in _image_os_cache:
            return _image_os_cache[image_id]
    try:
        image = core_client.get_image(image_id=image_id)
        verbose_print(f"image: {image.data}")
        os_name = image.data.operating_system
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(f"Exception getting Operating System for Image: {image_id}: {ex}")
        os_name = ''
    with _image_os_cache_lock:
        _image_os_cache[image_id] = os_name
    return os_name


# Serverless Functions: FunctionsFunction (FunctionsApplication ?)


def get_oci_cloud_functions_function(config, signer, compartment, regions):
    """ Get OCI Cloud Functions """
    for region in regions:
        serverless_functions_count = 0
        try:
            search_client = oci.resource_search.ResourceSearchClient(config=config_for_region(config, region), signer=signer, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            query_text = f"query functionsfunction resources where compartmentId = '{compartment['id']}' && lifeCycleState != 'DELETED' && lifeCycleState != 'DELETING'"
            response = search_client.search_resources(
                oci.resource_search.models.StructuredSearchDetails(
                    type="Structured",
                    query=query_text)
            )
            verbose_print(f"functions: {response.data}")
            items = json.loads(str(response.data))['items']
            serverless_functions_count = len(items)
            while response.has_next_page:
                response = search_client.search_resources(
                    oci.resource_search.models.StructuredSearchDetails(
                        type="Structured",
                        query=query_text),
                    page=response.next_page
                )
                verbose_print(f"functions: {response.data}")
                serverless_functions_count += len(json.loads(str(response.data))['items'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(f"Exception getting Serverless Functions in Region: {region}: {ex}", compartment['id'])

        if serverless_functions_count > 0 or args.verbose_mode:
            progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [Functions]', region=region['region_name'])
            totals['Serverless Functions'] += serverless_functions_count


def get_oci_buckets(config, signer, compartment, regions):
    """ Get OCI Buckets """
    for region in regions:
        buckets_count = 0
        try:
            search_client = oci.resource_search.ResourceSearchClient(config=config_for_region(config, region), signer=signer, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            response = search_client.search_resources(
                oci.resource_search.models.StructuredSearchDetails(
                    type="Structured",
                    query=f"query bucket resources where compartmentId = '{compartment['id']}' && lifeCycleState != 'TERMINATED' && lifeCycleState != 'TERMINATING'")
            )
            verbose_print(f"buckets: {response.data}")
            buckets_count = len(json.loads(str(response.data))['items'])
            while response.has_next_page:
                response = search_client.search_resources(
                    oci.resource_search.models.StructuredSearchDetails(
                        type="Structured",
                        query=f"query bucket resources where compartmentId = '{compartment['id']}' && lifeCycleState != 'TERMINATED' && lifeCycleState != 'TERMINATING'"),
                    page=response.next_page
                )
                verbose_print(f"buckets: {response.data}")
                buckets_count += len(json.loads(str(response.data))['items'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(f"Exception getting Buckets in Region: {region}: {ex}", compartment['id'])

        if buckets_count > 0 or args.verbose_mode:
            progress_print(resource_count=buckets_count, resource_type='Buckets', region=region['region_name'])
            totals['Data Buckets'] += buckets_count


####
# Main
####


def get_oci_resources(config, signer, compartment, regions):
    """ Get billable resources """
    exceptions = 0
    status_print(f"[SCAN] Compartment start: {compartment['id']} ({compartment['name']})")
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        if enabled['Virtual Machines'] or enabled['Container Hosts']:
            get_oci_instances_and_oke_instances(config, signer=signer, compartment=compartment, regions=regions)
            if enabled['Serverless Functions']:
                get_oci_cloud_functions_function(config, signer=signer, compartment=compartment, regions=regions)
        if enabled['Data Buckets']:
            get_oci_buckets(config, signer=signer, compartment=compartment, regions=regions)
    else:
        futures = {}
        failed_tasks = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            if enabled['Virtual Machines'] or enabled['Container Hosts']:
                futures[executor.submit(get_oci_instances_and_oke_instances, config, compartment=compartment, signer=signer, regions=regions)] = 'Compute/OKE instances'
                if enabled['Serverless Functions']:
                    futures[executor.submit(get_oci_cloud_functions_function, config, signer=signer, compartment=compartment, regions=regions)] = 'Cloud Functions'
            if enabled['Data Buckets']:
                futures[executor.submit(get_oci_buckets, config, signer=signer, compartment=compartment, regions=regions)] = 'Object Storage buckets'
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                failed_tasks += 1
                error_print(future.exception(), f"{compartment['id']} task={futures[future]}")
        if failed_tasks:
            error_print(f"{failed_tasks} task(s) failed for compartment {compartment['id']}")
    status_print(f"[DONE] Compartment complete: {compartment['id']} ({len(futures) if not args.debug_mode else 'sequential'} task(s), {failed_tasks} exception(s))")


def output_results(compartments, partial=False):
    """ Output results """
    os.makedirs(args.output_dir, exist_ok=True)
    # Summary File
    with open(output_path(output_file), 'w', encoding='utf-8', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals.items():
            csv_writer.writerow([resource_type, resource_count])
    # Log File
    with open(output_path(output_file_log), 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Region'])
        for item in totals_log:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(output_path(error_log_file), 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    label = "Partial results" if partial else "Results"
    print(f"\n{label} across {len(compartments)} OCI Compartments (script version: {version})\n")
    if partial:
        print("Scan interrupted; results above cover completed compartments only.\n")

    if enabled['Virtual Machines']:
        print(f"{str(totals['Virtual Machines']).rjust(padding)} Virtual Machines [Compute Instances]")
    if enabled['Container Hosts']:
        print(f"{str(totals['Container Hosts']).rjust(padding)} Container Hosts [OKE]")
    if enabled['Serverless Functions']:
        print(f"{str(totals['Serverless Functions']).rjust(padding)} Serverless Functions [Cloud Functions]")

    if enabled['Data Buckets']:
        print()
        print(f"{str(totals['Data Buckets']).rjust(padding)} Data Buckets (Public and Private) [Buckets]")

    if enabled['Kubernetes Sensors']:
        print()
        print(f"{str(totals['Kubernetes Sensors']).rjust(padding)} Kubernetes Sensors")
    if enabled['Virtual Machine Sensors']:
        print(f"{str(totals['Virtual Machine Sensors']).rjust(padding)} Virtual Machine Sensors [Estimated from Virtual Machine Image Operative System *]")

    if enabled['Virtual Machine Sensors']:
        print()
        print("* Linux Sensor counts may be lower, depending upon kernel and operating system versions")

    if not args.data_mode:
        print("\nTo count Data Security (Buckets, Databases, etc) resources, rerun with '--data'")

    print(f"\nDetails written to {output_file} and {output_file_log}")

    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


def main():
    """ Calculon Compute! """
    global last_compartments  # pylint: disable=global-statement
    try:
        config = oci.config.from_file(oci.config.DEFAULT_LOCATION, oci.config.DEFAULT_PROFILE)
        verbose_print(f"configuration: {config} from {oci.config.DEFAULT_LOCATION} using {oci.config.DEFAULT_PROFILE} profile")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error reading OCI configuration from default Location and Profile.")
        error_print("Exiting...")
        sys.exit(0)

    if os.path.isfile(delegation_token_file):
        with open(delegation_token_file, 'r', encoding='utf-8') as f:
            delegation_token = f.read().strip()
        try:
            signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(
                delegation_token = delegation_token
            )
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
            error_print("Error authenticating via Delegation Token File.")
            error_print("Exiting...")
            sys.exit(0)
    else:
        try:
            signer = oci.signer.Signer(
                tenancy                   = config['tenancy'],
                user                      = config['user'],
                fingerprint               = config['fingerprint'],
                private_key_file_location = config.get('key_file'),
            )
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
            error_print("Error authenticating via Profile.")
            error_print("Exiting...")
            sys.exit(0)

    regions = get_oci_regions(config, signer)

    print("Getting OCI Compartments")
    compartments = get_oci_compartments(config, signer)
    print(f"\nFound {len(compartments)} Compartments:")
    for compartment in compartments:
        print(f"- {compartment['id']} - {compartment['name']}")

    last_compartments = []
    past_start_after = not args.start_after_compartment
    scanned_count = 0
    print("\nGetting Billable Resources for each OCI Compartment ...")
    try:
        for index, compartment in enumerate(compartments, start=1):
            compartment_id = compartment['id']
            compartment_name = compartment['name']
            if not past_start_after:
                if compartment_id == args.start_after_compartment:
                    past_start_after = True
                else:
                    status_print(f"[SKIP] Compartment {index}/{len(compartments)}: {compartment_id} (before --start-after-compartment)")
                continue
            if not compartment_matches_filters(compartment_id, compartment_name):
                status_print(f"[SKIP] Compartment {index}/{len(compartments)}: {compartment_id} - {compartment_name}")
                continue
            if max_runtime_reached():
                status_print(f"[STOP] Max runtime of {args.max_run_minutes}m reached after {scanned_count} compartment(s).")
                output_results(last_compartments, partial=True)
                return
            if args.max_compartments and scanned_count >= args.max_compartments:
                status_print(f"[STOP] Reached --max-compartments {args.max_compartments}.")
                output_results(last_compartments, partial=True)
                return
            status_print(f"[SCAN] Compartment {index}/{len(compartments)}: {compartment_id} - {compartment_name}")
            get_oci_resources(config, signer, compartment, regions)
            last_compartments.append(compartment)
            scanned_count += 1
            if args.checkpoint_interval and scanned_count % args.checkpoint_interval == 0:
                status_print(f"[CHECKPOINT] {scanned_count} compartment(s) complete.")
                output_results(last_compartments, partial=True)
    except Exception:
        output_results(last_compartments, partial=True)
        raise

    output_results(last_compartments)


if __name__ == "__main__":
    last_compartments = []
    signal.signal(signal.SIGINT,signal_handler)
    main()
