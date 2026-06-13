#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Resource Count : vSphere """

import argparse
import concurrent.futures
import csv
import getpass
import inspect
import os
import signal
import ssl
import sys
import time

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    from pyVim import connect
    from pyVmomi import vim
except ImportError:
    print("\nERROR: Missing required vSphere SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade pyvim pyvmomi")
    sys.exit(1)


version='2.8.0'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count vSphere Resources')
parser.add_argument(
    '--server',
    dest = 'server',
    help = 'vSphere Server Host Name',
    default = os.environ.get('VSPHERE_SERVER'),
)
parser.add_argument(
    '--port',
    dest = 'port',
    help = 'vSphere Port',
    default = 443,
)
parser.add_argument(
    '--username',
    dest = 'username',
    help = 'vSphere User',
    default = os.environ.get('VSPHERE_USERNAME'),
)
parser.add_argument(
    '--password',
    dest = 'password',
    help = 'vSphere Password',
    default = os.environ.get('VSPHERE_PASSWORD'),
)
parser.add_argument(
    '--cluster',
    dest = 'cluster',
    help = 'vSphere Cluster Name',
    default = os.environ.get('VSPHERE_CLUSTER'),
)
parser.add_argument(
    '--ssl_verify',
    action = 'store_true',
    dest = 'ssl_verify',
    help = 'Verify SSL connection (default: disabled)',
    default = False,
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
    help = 'Directory for output CSV files (default: current directory)',
    default = '.'
)
args = parser.parse_args()

if not args.server:
    print("\nerror: the following arguments are required: --server")
    sys.exit(0)
if not args.username:
    print("\nerror: the following arguments are required: --username")
    sys.exit(0)
# --cluster is optional; omitting it scans the entire vCenter.
if not args.password:
    args.password = getpass.getpass(prompt='Enter vSphere Password')
if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)


####
# Configuration and Globals
####


output_file     = 'vsphere-resources.csv'
output_file_log = 'vsphere-resources-log.csv'
padding = 6
totals = {
    'Asset Metadata': 0
}
totals_log = []
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


def signal_handler(_signal_received, _frame):
    """ Control-C """
    status_print("[INTERRUPTED] Writing partial results before exiting.")
    output_results(partial=True)
    sys.exit(0)


def progress_print(resource_count, resource_type):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type}".split()))
    totals_log.append([resource_type, resource_count])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details):
    """ Error output """
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    print(f"\nERROR: {function} {details}\n")



####
# Customized Library Code
####


def get_vsphere_client():
    """ Return a vSphere Client """
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not args.ssl_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        client = connect.SmartConnect(host=args.server, port=args.port, user=args.username, pwd=args.password, sslContext=context)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        sys.exit(0)
    return client


def get_objects(content, vim_types):
    """ Populates objects of managed entity types """
    objects = {}
    container = content.viewManager.CreateContainerView(content.rootFolder, vim_types, recursive=True)
    # return list(item for item in container.view)
    for managed_object_reference in container.view:
        objects.update({managed_object_reference: managed_object_reference.name})
    return objects


# Using the VMware vSphere Automation SDK:
#
# from vmware.vapi.vsphere.client import create_vsphere_client
# client = create_vsphere_client(server=args.server, username=args.username, password=args.password)
# hosts  = client.vcenter.Host.list(Host.FilterSpec(clusters=set([args.cluster])))
# vms    = client.vcenter.VM.list(VM.FilterSpec(clusters=set([args.cluster])))


# Hosts


def get_vsphere_hosts(content):
    """ vsphere#hostSystem """
    hosts_count = 0
    # pylint: disable=c-extension-no-member
    for cluster in get_objects(content, [vim.ComputeResource]):
        if args.cluster:
            if cluster.name == args.cluster:
                for host in cluster.host:
                    verbose_print(f"cluster: {args.cluster} host: {host.name}")
                    hosts_count += 1
        else:
            for host in cluster.host:
                verbose_print(f"cluster: {args.cluster} host: {host.name}")
                hosts_count += 1

    if hosts_count > 0 or args.verbose_mode:
        progress_print(resource_count=hosts_count, resource_type='Asset Metadata [Hosts]')
        totals['Asset Metadata'] += hosts_count


# Virtual Machines


def get_vsphere_instances(content):
    """ vsphere#virtualMachine """
    instances_count = 0
    # pylint: disable=c-extension-no-member
    for cluster in get_objects(content, [vim.ComputeResource]):
        if args.cluster:
            if cluster.name == args.cluster:
                for host in cluster.host:
                    for vm in host.vm:
                        verbose_print(f"cluster: {args.cluster} host: {host.name} vm: {vm.name} ")
                        instances_count += 1
        else:
            for host in cluster.host:
                for vm in host.vm:
                    verbose_print(f"cluster: {args.cluster} host: {host.name} vm: {vm.name} ")
                    instances_count += 1

    if instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_count, resource_type='Asset Metadata [Virtual Machines]')
        totals['Asset Metadata'] += instances_count


####
# Main
####


def get_vsphere_resources(client):
    """ Get billable resources """
    exceptions = 0
    content = client.RetrieveContent()
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        get_vsphere_hosts(content=content)
        get_vsphere_instances(content=content)
    else:
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures.append(executor.submit(get_vsphere_hosts, content=content))
            futures.append(executor.submit(get_vsphere_instances, content=content))
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                exceptions += 1
        if exceptions:
            print("\nExceptions occurred.")
            print("Rerun with '--debug' to disable parallel processing and exit upon first error.")


def output_results(partial=False):
    """ Output results """
    os.makedirs(args.output_dir, exist_ok=True)
    # Summary File
    with open(output_path(output_file), 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals.items():
            csv_writer.writerow([resource_type, resource_count])

    # Log File
    with open(output_path(output_file_log), 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for item in totals_log:
            csv_writer.writerow(item)

    # Summary
    label = "Partial results" if partial else "Results"
    print(f"\n{label} (script version: {version})\n")

    print(f"{str(totals['Asset Metadata']).rjust(padding)} Asset Metadata [Hosts, Virtual Machines]")

    print(f"\nDetails written to {output_file}")


def main():
    """ Calculon Compute! """

    client = get_vsphere_client()

    status_print(f"[SCAN] Starting vSphere scan of {args.server} ...")
    if args.cluster:
        print(f"Filtering on Cluster: {args.cluster} ...")

    get_vsphere_resources(client)
    status_print("[DONE] vSphere scan complete.")
    output_results()

####

if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
