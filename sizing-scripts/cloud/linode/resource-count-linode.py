#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Resource Count : Linode """

import argparse
import concurrent.futures
import csv
import inspect
import os
import re
import signal
import sys

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    from linode_api4 import LinodeClient, ApiError
except ImportError:
    print("\nERROR: Missing required Linode SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade linode_api4")
    sys.exit(1)


version='2.8.0'


####
# Command Line Arguments
####


parser = argparse.ArgumentParser(description = 'Count Linode Resources')
parser.add_argument(
    '--token',
    dest = 'token',
    help = 'Use this Linode Personal Access Token (default: env LINODE_TOKEN)',
    default = os.environ.get('LINODE_TOKEN', ''),
)
parser.add_argument(
    '--max-workers',
    dest = 'max_workers',
    help = 'Maximum parallel processing requests (default: 100)',
    type = int,
    default = 100
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


####
# Configuration and Globals
####


output_file     = 'linode-resources.csv'
output_file_log = 'linode-resources-log.csv'
padding = 6
totals = {
    'Asset Metadata': 0
}
totals_log = []


####
# Common Library Code
####


def signal_handler(_signal_received, _frame):
    """ Control-C """
    print("\nExiting")
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

# API Token Scopes:
#
# Account
# Databases
# Linodes
# Kubernetes
# Object Storage
# Volumes

# Pagination:
#
# The Linode API returns collections of resources one page at a time.
# The first page of a collection is always loaded when the collection is returned,
# and subsequent pages are loaded as they are required.
# Pagination is handled transparently, and as requested.

# Models:
#
# Many models are related to other models.
# For example a Linode Instance has disks, configs, volumes, backups, a region, etc).
# Related attributes are accessed like any other attribute on the model,
# and will emit an API call to retrieve the related models if necessary.


def get_linode_client(token):
    """ Return a Linode Client """
    try:
        client = LinodeClient(token)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting Linode Client.")
        error_print("Exiting...")
        sys.exit(0)
    return client


def api_error_print(ex, scope):
    """ Error output """
    if ex.status == 403:
        print(f"- WARNING: Account or API Token Scope excludes {scope}")


# Subscriptions (aka Linode Accounts)


def get_linode_account(client):
    """ Get Linode Account """
    account = account = {'ID': 'UNKNOWN', 'Object': None}
    try:
        account = client.account()
        account = {'ID': account.euuid, 'Object': account}
    except ApiError as ex:
        if ex.status == 403:
            error_print('Error getting Linode Account. Account or API Token scope excludes Account.')
        else:
            error_print(ex)
            error_print('Error getting Linode Account.')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode Account.')
    verbose_print(f"account: {account}")
    return account


# Virtual Machines: Linodes and LKE Linodes (Asset Metadata)

# This counts Linodes, and can count Non-OS Disks (aka Volumes).
# non_os_disks_count += len(instance.volumes())

def get_linode_instances(client):
    """ Get Linode Instances """
    instances_count = 0
    container_instances_count = 0
    try:
        instances = client.linode.instances()
    except ApiError as ex:
        api_error_print(ex, 'Linodes')
        return
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode Instances.')
        return
    for instance in instances:
        verbose_print(f"instance: {instance.id} ({instance.label})")
        if instance.status == 'deleting':
            continue
        instance_label = instance.label
        # Instances and LKE Instances are returned by client.linode.instances().
        # Unable to identify more specific methods to use.
        regex = r"lke\d{6}\-\d{6}\-\w{12}"
        if re.match(regex, instance_label):
            container_instances_count += 1
        else:
            instances_count += 1

    if instances_count > 0 or args.debug_mode:
        progress_print(resource_count=instances_count, resource_type='Asset Metadata [Linodes]')
        totals['Asset Metadata'] += instances_count

    if container_instances_count > 0 or args.debug_mode:
        progress_print(resource_count=container_instances_count, resource_type='Asset Metadata [Linodes LKE]')
        totals['Asset Metadata'] += container_instances_count


# Virtual Machines: LKE Linodes (Asset Metadata)

# ALT:
# This also counts LKE Linodes, but cannot count Non-OS Disks (aka Volumes).
# Volumes cannot be created by default when creating LKE Clusters, but can be added to existing LKE Cluster Nodes.

def get_linode_lke_instances(client):
    """ Get Linode LKE Instances """
    container_instances_count = 0
    try:
        clusters = client.lke.clusters()
    except ApiError as ex:
        api_error_print(ex, 'Kubernetes')
        return
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode LKE Instances.')
        return
    for cluster in clusters:
        verbose_print(f"cluster: {cluster.id} ({cluster.label})")
        pools = cluster.pools
        for pool in pools:
            verbose_print(f"pool: {pool.id}")
            nodes = pool.nodes
            for node in nodes:
                verbose_print(f"node: {node.id}")
                container_instances_count += 1

    if container_instances_count > 0 or args.debug_mode:
        progress_print(resource_count=container_instances_count, resource_type='ALT Asset Metadata [Linodes LKE]')
        totals['ALT Asset Metadata'] +=  container_instances_count


# Data Buckets: Object Storage Buckets (Asset Metadata)


def get_linode_buckets(client):
    """ Get Linode Object Storage Bucket """
    buckets_count = 0
    try:
        buckets = client.object_storage.buckets()
    except ApiError as ex:
        api_error_print(ex, 'Object Storage')
        return
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode Object Storage Buckets.')
        return
    for bucket in buckets:
        verbose_print(f"bucket: {bucket}")
        buckets_count += 1

    if buckets_count > 0 or args.debug_mode:
        progress_print(resource_count=buckets_count, resource_type='Asset Metadata [Object Storage Buckets]')
        totals['Asset Metadata'] += buckets_count


# Databases (PaaS): MongoDB, MySQL, PostgreSQL (Asset Metadata)

# As per: https://www.linode.com/docs/products/databases/managed-databases/guides/create-database/
# Databases are currently disabled, resulting in the following errors.
# AttributeError: 'DatabaseGroup' object has no attribute 'mongodb_instances'
# linode_api4.errors.ApiError: 404: Not found (mysql_instances, postgresql_instances)


def get_linode_databases(client):
    """ Get Linode Databases """
    database_instances_count = 0
    try:
        mongodb_instances = client.database.mongodb_instances()
        for db in mongodb_instances:
            verbose_print(f"mongodb: {db}")
            database_instances_count += 1
    except AttributeError:
        pass
    except ApiError as ex:
        api_error_print(ex, 'Databases')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode Databases.')
    try:
        mysql_instances = client.database.mysql_instances()
        for db in mysql_instances:
            verbose_print(f"mysql: {db}")
            database_instances_count += 1
    except ApiError as ex:
        api_error_print(ex, 'Databases')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode Databases.')
    try:
        postgresql_instances = client.database.postgresql_instances()
        for db in postgresql_instances:
            verbose_print(f"postgresql: {db}")
            database_instances_count += 1
    except ApiError as ex:
        api_error_print(ex, 'Databases')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print('Error getting Linode Databases.')

    if database_instances_count > 0 or args.debug_mode:
        progress_print(resource_count=database_instances_count, resource_type='Asset Metadata [Databases: MongoDB, MySQL, PostgreSQL]')
        totals['Asset Metadata'] += database_instances_count


####
# Main
####


def get_linode_resources(client):
    """ Get billable resources """
    exceptions = 0
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        get_linode_instances(client=client)
        get_linode_buckets(client=client)
        get_linode_databases(client=client)
    else:
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures.append(executor.submit(get_linode_instances, client=client))
            futures.append(executor.submit(get_linode_buckets, client=client))
            futures.append(executor.submit(get_linode_databases, client=client))
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                exceptions += 1
        if exceptions:
            print("\nExceptions occurred.")
            print("Rerun with '--debug' to disable parallel processing and exit upon first error.")

def output_results():
    """ Output results """
    # Summary File
    with open(output_file, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals.items():
            csv_writer.writerow([resource_type, resource_count])

    # Log File
    #with open(output_file_log, 'w', encoding='utf-8') as csv_file:
    #    csv_writer = csv.writer(csv_file)
    #    csv_writer.writerow(['Resource Type', 'Resource Count'])
    #    for item in totals_log:
    #        csv_writer.writerow(item)

    # Summary
    print("\nResults (script version: {version})\n")

    print(f"{str(totals['Asset Metadata']).rjust(padding)} Asset Metadata [Linodes, LKE Linodes, Object Storage Buckets, MongoDB, MySQL, PostgreSQL Databases]")

    print(f"\nDetails written to {output_file}")


def main():
    """ Calculon Compute! """

    client = get_linode_client(args.token)

    print("Getting the current Linode Account")
    account = get_linode_account(client)

    print(f"\nFound Account:\n- {account['ID']}")

    print("\nGetting Billable Resources for the current Linode Account ...")
    get_linode_resources(client)

    output_results()


####

if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
