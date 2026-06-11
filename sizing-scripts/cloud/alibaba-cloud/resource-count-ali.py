#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Resource Count : Aliyun """

import argparse
import concurrent.futures
import csv
import inspect
import json
import math
import os
import signal
import sys
import warnings

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.auth.credentials import StsTokenCredential
    from aliyunsdkcore.endpoint.local_config_regional_endpoint_resolver import LocalConfigRegionalEndpointResolver
    from aliyunsdkecs.request.v20140526.DescribeDisksRequest import DescribeDisksRequest
    from aliyunsdkecs.request.v20140526.DescribeRegionsRequest import DescribeRegionsRequest
    from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest
    from aliyunsdkcs.request.v20151215.DescribeClustersRequest import DescribeClustersRequest
    from aliyunsdkresourcemanager.request.v20200331.ListAccountsRequest import ListAccountsRequest
    from aliyunsdksts.request.v20150401.AssumeRoleRequest import AssumeRoleRequest
    #
    # aliyunsdk (https://github.com/aliyun/aliyun-openapi-python-sdk)
    # alibabacloud https://api.aliyun.com/
    #
    from alibabacloud_cs20151215.client import Client as CS20151215Client
    from alibabacloud_cs20151215 import models as cs20151215_models
    from alibabacloud_sts20150401.client import Client as Sts20150401Client
    from alibabacloud_sts20150401 import models as sts_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models

except ImportError as exi:
    print(f"\nERROR: Missing required Alibaba SDK packages. {exi} Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade aliyun-python-sdk-core aliyun-python-sdk-resourcemanager aliyun-python-sdk-ecs aliyun-python-sdk-cs aliyun-python-sdk-sts alibabacloud_cs20151215 alibabacloud_sts20150401 alibabacloud_tea_openapi alibabacloud_tea_util")
    sys.exit(1)


version='2.8.0'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count Alibaba/Aliyun Resources')
parser.add_argument(
    '--access_key',
    help = 'Specify the Access Key to use to access Alibaba (required)',
    required = True
)
parser.add_argument(
    '--secret_key', '--access_key_secret',
    help = 'Specify the Access Key Secret to use to access Alibaba (required)',
    required = True
)
parser.add_argument(
    '--all',
    action = 'store_true',
    dest = 'all',
    help = 'Count resources in all Accounts in the current Alibaba Organization (default: disabled)',
    default = False
)
parser.add_argument(
    '--list-role-name',
    action = 'store',
    dest = 'list_role',
    help = 'Alibaba Role Name (not ARN) to assume when listing Organization Alibaba Accounts',
)
parser.add_argument(
    '--access-role-name',
    action = 'store',
    dest = 'access_role',
    help = 'Alibaba Role Name (not ARN) to assume when accessing Organization Alibaba Accounts',
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
args = parser.parse_args()

if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)


####
# Configuration and Globals
####


output_file           = 'ali-resources.csv'
output_file_log       = 'ali-resources-log.csv'
error_log_file        = 'ali-errors-log.txt'
padding = 6

# Map command-line arguments to counts to execute and display.
enabled = {
    'Virtual Machines':        True,
    'Container Hosts':         True,
    'Non-OS Disks':            True,
    'Kubernetes Sensors':      True,
    'Virtual Machine Sensors': True,
}

totals = {
    'Virtual Machines':        0,
    'Container Hosts':         0,
    'Non-OS Disks':            0,
    'Kubernetes Sensors':      0,
    'Virtual Machine Sensors': 0,
}

totals_log = []
errors_log = []

####
# Common Library Code
####


def signal_handler(_signal_received, _frame):
    """ Control-C """
    print("\nExiting")
    sys.exit(0)


def progress_print(resource_count, resource_type, account='', region='', details=''):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type} in {account} {region} {details}".split()))
    totals_log.append([resource_type, resource_count, account, region])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details, account=''):
    """ Error output """
    account = f"Account: {account} " if account else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {account}{function} {details}\n")
    errors_log.append(f"ERROR: {account}{function} {details}")


####
# Customized Library Code
####


# Pagination Method 1: NextToken
#
# Set MaxResults to specify the maximum number of entries to return in the call.
# The return value of NextToken is a pagination token, which can be used in the next request to retrieve a new page of results.
# When you call to retrieve a new page of results, set NextToken to the NextToken value returned in the previous call and set MaxResults to specify the maximum number of entries to return in this call.
#
# Pagination Method 2: PageNumber
#
# Use PageSize to specify the number of entries to return on each page and then use PageNumber to specify the number of the page to return.
# You can use only one of the preceding methods.
# If you specify MaxResults or NextToken, the PageSize and PageNumber request parameters do not take effect and the TotalCount response parameter is invalid.
#
# If a large number of entries are to be returned, we recommend that you use Method 1: NextToken.


def send_ali_request(client, request):
    """ Get Alibaba API Client Request """
    result = None
    try:
        request.set_accept_format('json')
        with warnings.catch_warnings():
            # For SNIMissingWarning in aliyunsdkcore/vendored/requests/packages/urllib3/util/ssl_.py
            warnings.simplefilter('ignore')
            response = client.do_action_with_exception(request)
        result = json.loads(response.decode('utf-8'))
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, request)
    return result


##


def get_ali_client(region_id='cn-shanghai'):
    """ Get Alibaba API Client """
    client = None
    try:
        client = AcsClient(args.access_key, args.secret_key, region_id)
        verbose_print(f"client: {client}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    return client


def get_ali_assume_role_client(assumed_role_client=None, role_arn=None, region_id='cn-hangzhou'):
    """ Assume Alibaba Role and return a Client """
    assume_role_client = None
    verbose_print(f"get_ali_assume_role_client: assumed_role_client: {assumed_role_client} role_arn: {role_arn}")
    try:
        request = AssumeRoleRequest()
        request.set_RoleArn(role_arn)
        request.set_RoleSessionName('Wiz-Resource-Discovery-Script')
        # Either Assume Role in the Organization, or if we have an assumed_role_client in the Organization then Assume Role in the Account.
        if not assumed_role_client:
            assumed_role_client = get_ali_client()
        response = send_ali_request(assumed_role_client, request)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    if response:
        access_key_id_assumed     = response['Credentials']['AccessKeyId']
        access_key_secret_assumed = response['Credentials']['AccessKeySecret']
        security_token            = response['Credentials']['SecurityToken']
        try:
            assume_role_client = AcsClient(region_id=region_id, credential=StsTokenCredential(access_key_id_assumed, access_key_secret_assumed, security_token))
            verbose_print(f"assume_role_client: {assume_role_client}")
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
    return assume_role_client

##

def get_ali_assume_role_client_sts(role_arn=None, region_id='cn-hangzhou'):
    """ Assume Alibaba CS Role and return a CS Client """
    assume_role_client_cs = None
    verbose_print(f"get_ali_assume_role_client_sts: role_arn: {role_arn}")
    try:
        # Assume Role in the Organization.
        # sts_client_config = open_api_models.Config(region_id=region_id, access_key_id=args.access_key, access_key_secret=args.secret_key)
        # sts_client = Sts20150401Client(sts_client_config)
        sts_client = get_ali_sts_client(region_id=region_id)
        assume_role_request = sts_models.AssumeRoleRequest(role_arn=role_arn, role_session_name="Wiz-Resource-Discovery-Script-CS")
        assume_role_response = sts_client.assume_role(assume_role_request)
        assume_role_credentials = assume_role_response.body.credentials
        access_key_id     = assume_role_credentials.access_key_id
        access_key_secret = assume_role_credentials.access_key_secret
        security_token    = assume_role_credentials.security_token
        # Assume Role in the Account.
        cs_client_config = open_api_models.Config(access_key_id=access_key_id, access_key_secret=access_key_secret, security_token=security_token)
        cs_client_config.endpoint = f"sts.{region_id}.aliyuncs.com"
        assume_role_client_cs = CS20151215Client(cs_client_config)
        verbose_print(f"assume_role_client_cs: {assume_role_client_cs}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    return assume_role_client_cs


def get_ali_sts_client(region_id='cn-shanghai'):
    """ Get Alibaba API Security Token Service Client """
    client = None
    try:
        config = open_api_models.Config(access_key_id=args.access_key, access_key_secret=args.secret_key)
        config.endpoint = f"sts.{region_id}.aliyuncs.com"
        client = Sts20150401Client(config)
        verbose_print(f"security_token_service_client: {client}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    return client


def get_ali_cs_client(region_id=None):
    """ Get Alibaba API Container Service Client """
    client = None
    try:
        config = open_api_models.Config(access_key_id=args.access_key, access_key_secret=args.secret_key)
        config.endpoint = f"cs.{region_id}.aliyuncs.com"
        client = CS20151215Client(config)
        verbose_print(f"container_service_client: {client}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    return client

# get_caller_identity_with_options: {'AccountId': '1234567890123456', 'Arn': 'acs:ram::1234567890123456:user/examplereaderuser', 'IdentityType': 'RAMUser', 'PrincipalId': '012345678901234567', 'RequestId': 'CD29225C-98A7-56E9-AAEC-962E71C0649C', 'UserId': '012345678901234567'}

def get_current_ali_account():
    """ Get Alibaba Account using GetCallerIdentity """
    account = {}
    try:
        client = get_ali_sts_client()
        runtime = util_models.RuntimeOptions()
        response = client.get_caller_identity_with_options(runtime)
        account['AccountId'] = response.body.account_id
        account['UserId']    = response.body.user_id
        verbose_print(f"current account: {account}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    return account

# AccountId; DisplayName; Type: CloudAccount, ResourceAccount
# PageNumber, PageSize

def get_ali_accounts(assumed_role_client=None):
    """ Get Alibaba Accounts """
    accounts = []
    try:
        request = ListAccountsRequest()
        # ListAccountsRequest() has no set_MaxResults()
        response = send_ali_request(assumed_role_client, request)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    while response:
        response_accounts = response.get('Accounts').get('Account', [])
        for account in response_accounts:
            verbose_print(f"account: {account}")
            accounts.append(account)
        page_number = response.get('PageNumber')
        total_count = response.get('TotalCount')
        if len(accounts) < total_count:
            try:
                request.set_PageNumber(page_number + 1)
                response = send_ali_request(assumed_role_client, request)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex)
                response = None
        else:
            response = None
    verbose_print(f"accounts: {accounts}")
    return accounts


def get_ali_regions_via_local(product_code='ecs'):
    """ Get Alibaba Regions using LocalConfigRegionalEndpointResolver (unused) """
    region_ids = []
    try:
        resolver = LocalConfigRegionalEndpointResolver()
        region_ids = resolver.get_valid_region_ids_by_product(product_code=product_code)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    verbose_print(f"regions via LocalConfigRegionalEndpointResolver: {region_ids}")
    return region_ids


def get_ali_regions_via_ecs(account=None, assumed_role_client=None):
    """ Get Alibaba Regions using DescribeRegionsRequest """
    region_ids = []
    try:
        if assumed_role_client:
            # Organization Account
            role_arn = f"acs:ram::{account['AccountId']}:role/{args.access_role}"
            client = get_ali_assume_role_client(assumed_role_client=assumed_role_client, role_arn=role_arn)
        else:
            # Individual Account
            client = get_ali_client()
        request = DescribeRegionsRequest()
        # DescribeRegionsRequest() has no set_MaxResults()
        response = send_ali_request(client, request)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    if response:
        response_regions = response.get('Regions').get('Region', [])
        for region in response_regions:
            region_ids.append(region['RegionId'])
    verbose_print(f"regions via DescribeRegionsRequest: {region_ids}")
    return region_ids


# Virtual Machines: ECS Instances


def get_ali_instances(account=None, region_id=None, assumed_role_client=None):
    """ Get Alibaba ECS Instances """
    instances_count = 0
    container_host_instances_count = 0
    linux_instances_count = 0
    non_os_disks_count = 0
    try:
        if assumed_role_client:
            # Organization Account
            role_arn = f"acs:ram::{account['AccountId']}:role/{args.access_role}"
            client = get_ali_assume_role_client(assumed_role_client=assumed_role_client, role_arn=role_arn, region_id=region_id)
        else:
            # Individual Account
            client = get_ali_client(region_id=region_id)
        request = DescribeInstancesRequest()
        request.set_MaxResults(100)
        response = send_ali_request(client, request)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    while response:
        response_instances = response.get('Instances').get('Instance', [])
        for instance in response_instances:
            verbose_print(f"instance: {instance}")
            # ACK instances are not returned by DescribeInstancesRequest().
            # The source of instances with a ClusterId is unknown. See also cluster_type in get_ali_cluster_instances().
            if instance.get('ClusterId'):
                container_host_instances_count += 1
            else:
                instances_count += 1
                if instance['OSType'].lower() == 'linux':
                    linux_instances_count += 1
            non_os_disks_count += get_ali_instance_disks(client=client, instance=instance)
        next_token = response.get('NextToken')
        if next_token:
            try:
                request.set_NextToken(next_token)
                response = send_ali_request(client, request)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex)
                response = None
        else:
            response = None

    if instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_count, resource_type='Virtual Machines [ECS]', account=account['AccountId'], details=f"with {non_os_disks_count} Non-OS Disks in Region {region_id}")
        totals['Virtual Machines'] += instances_count
        totals['Non-OS Disks'] += non_os_disks_count
        totals['Virtual Machine Sensors'] += linux_instances_count

    if container_host_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=container_host_instances_count, resource_type='Container Hosts [ACK]', account=account['AccountId'], details=f"in Region {region_id}")
        totals['Container Hosts'] += container_host_instances_count
        totals['Kubernetes Sensors'] += container_host_instances_count

# Virtual Machines: ECS Instances: Non-OS Disks

def get_ali_instance_disks(client=None, instance=None):
    """ Get Alibaba ECS Instance Disks via TotalCount """
    instance_non_os_disks_count = 0
    try:
        request = DescribeDisksRequest()
        # DescribeDisksRequest quietly ignores set_MaxResults()
        request.set_InstanceId(instance['InstanceId'])
        request.set_DiskType('data')
        response = send_ali_request(client, request)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    if response:
        verbose_print(f"disks: {response}")
        instance_non_os_disks_count = response.get('TotalCount', 0)
    return instance_non_os_disks_count


# Container Hosts: ACK Instances

# Unused: the response is an array without evidence of pagination.

def v0_get_ali_cluster_instances(account=None, region_id=None, assumed_role_client=None):
    """ Get Alibaba Container Hosts (ACK) in the specified Region """
    container_host_instances_count = 0
    try:
        if assumed_role_client:
            # Organization Account
            role_arn = f"acs:ram::{account['AccountId']}:role/{args.access_role}"
            client = get_ali_assume_role_client(assumed_role_client=assumed_role_client, role_arn=role_arn, region_id=region_id)
        else:
            # Individual Account
            client = get_ali_client(region_id=region_id)
        request = DescribeClustersRequest()
        # DescribeRegionsRequest() has no set_MaxResults()
        response = send_ali_request(client, request)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    if response:
        for cluster in response:
            verbose_print(f"cluster: {cluster}")
            container_host_instances_count += cluster.get('size', 0)

    if container_host_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=container_host_instances_count, resource_type='Container Hosts [ACK]', account=account['AccountId'], details=f"in Region {region_id}")
        totals['Container Hosts'] += container_host_instances_count
        totals['Kubernetes Sensors'] += container_host_instances_count


# pylint: disable=too-many-locals, too-many-statements
def get_ali_cluster_instances(account=None, region_id=None, role_arn=None):
    """ Get Alibaba Container Hosts (ACK) in the specified Region """
    container_host_instances_count = 0
    try:
        if role_arn:
            # Organization Account
            client = get_ali_assume_role_client_sts(region_id=region_id, role_arn=role_arn)
        else:
            # Individual Account
            client = get_ali_cs_client(region_id=region_id)
        request = cs20151215_models.DescribeClustersV1Request()
        runtime = util_models.RuntimeOptions()
        headers = {}
        response = client.describe_clusters_v1with_options(request, headers, runtime)
        page_size = response.body.page_info.page_size
        total_count = response.body.page_info.total_count
        if page_size >= total_count:
            last_page_number = 1
        else:
            last_page_number = math.floor(total_count / page_size)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        response = None
    while response:
        try:
            clusters = response.body.clusters
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
            clusters = []
        for cluster in clusters:
            verbose_print(f"cluster: {cluster}")
            # cluster.get('cluster_type') can be one of:
            # Kubernetes (Dedicated), ManagedKubernetes (Managed), ExternalKubernetes (Registered)
            container_host_instances_count += cluster.size
        page_number = response.body.page_info.page_number
        next_page_number = page_number + 1
        if next_page_number < last_page_number:
            try:
                request.page_number(next_page_number)
                response = client.describe_clusters_v1with_options(request, headers, runtime)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex)
                response = None
        else:
            response = None

    if container_host_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=container_host_instances_count, resource_type='Container Hosts [ACK]', account=account['AccountId'], details=f"in Region {region_id}")
        totals['Container Hosts'] += container_host_instances_count
        totals['Kubernetes Sensors'] += container_host_instances_count


####
# Main
####

def get_ali_resources(account=None, assumed_role_client=None, role_arn=None):
    """ Get billable resources for the specified Account """
    exceptions = 0
    ecs_regions_list = get_ali_regions_via_ecs(account=account, assumed_role_client=assumed_role_client)

    if os.environ.get('ALI_DEV'):
        ecs_regions_list = ['ap-southeast-1', 'us-west-1']
        print(f"\nLimiting Regions to {ecs_regions_list} while in ALI_DEV mode\n")

    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        for region_id in ecs_regions_list:
            if enabled['Virtual Machines'] or enabled['Container Hosts']:
                get_ali_instances(account=account, region_id=region_id, assumed_role_client=assumed_role_client)
                if args.all and not os.environ.get('ALI_DEV'):
                    pass
                else:
                    if enabled['Container Hosts']:
                        get_ali_cluster_instances(account=account, region_id=region_id, role_arn=role_arn)
    else:
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            for region_id in ecs_regions_list:
                if enabled['Virtual Machines'] or enabled['Container Hosts']:
                    futures.append(executor.submit(get_ali_instances, account=account, region_id=region_id, assumed_role_client=assumed_role_client))
                    if args.all and not os.environ.get('ALI_DEV'):
                        pass
                    else:
                        if enabled['Container Hosts']:
                            futures.append(executor.submit(get_ali_cluster_instances, account=account, region_id=region_id, role_arn=role_arn))
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                exceptions += 1


def output_results(accounts):
    """ Output results """
    # Summary File
    with open(output_file, 'w', encoding='utf-8', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals.items():
            csv_writer.writerow([resource_type, resource_count])
    # Log File
    with open(output_file_log, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Project', 'Region'])
        for item in totals_log:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(error_log_file, 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    print(f"\nResults across {len(accounts)} Alibaba Accounts (script version: {version})\n")

    if enabled['Virtual Machines']:
        print(f"{str(totals['Virtual Machines']).rjust(padding)} Virtual Machines [ECS]")
    if enabled['Container Hosts']:
        print(f"{str(totals['Container Hosts']).rjust(padding)} Container Hosts [ACK]")

    if enabled['Non-OS Disks']:
        print()
        print(f"{str(totals['Non-OS Disks']).rjust(padding)} Non-OS Disks [ECS]")

    if enabled['Kubernetes Sensors']:
        print()
        print(f"{str(totals['Kubernetes Sensors']).rjust(padding)} Kubernetes Sensors")
    if enabled['Virtual Machine Sensors']:
        print(f"{str(totals['Virtual Machine Sensors']).rjust(padding)} Virtual Machine Sensors *")

    if enabled['Virtual Machine Sensors']:
        print()
        print("* Linux Sensor counts may be lower, depending upon kernel and operating system versions")

    print(f"\nDetails written to {output_file} and {output_file_log}")

    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


def main():
    """ Calculon Compute! """
    accounts = []
    org_assumed_role_client = None
    org_role_arn            = None

    print("Getting the current Alibaba Account:\n")
    current_account = get_current_ali_account()
    print(f"-- {current_account['AccountId']}")

    if args.all:
        if not args.list_role:
            print("ERROR: You must specify a Role name (via '--list-role-name') to list Organization Accounts when specifying '--all'")
            print("Exiting...")
        if not args.access_role:
            print("ERROR: You must specify a Role name (via '--access-role-name') to access Organization Accounts when specifying '--all'")
            print("Exiting...")

        # Assume Role in the Organization to List Accounts, and use that Role to Assume Role in each Account.
        org_role_arn = f"acs:ram::{current_account['AccountId']}:role/{args.list_role}"
        org_assumed_role_client = get_ali_assume_role_client(role_arn=org_role_arn)

        print("\nGetting Alibaba Accounts in the current Organization")
        accounts = get_ali_accounts(assumed_role_client=org_assumed_role_client)
        print(f"\nFound {len(accounts)} Accounts:\n")
        for account in accounts:
            print(f"-- {account['AccountId']}")

        if not os.environ.get('ALI_DEV'):
            print("\nNOTE: Currently unable to scan Alibaba Container Hosts (ACK) in Organization mode")
    else:
        print(f"\nFound Account:\n\n-- {current_account['AccountId']}")
        accounts = [current_account]

    print("\nGetting Billable Resources for the each Alibaba Account ...")
    for account in accounts:
        if os.environ.get('ALI_DEV'):
            if account['AccountId'] != os.environ.get('ALI_DEV'):
                print(f"\nSkipping {account['AccountId']} while in ALI_DEV mode\n")
                continue
        print(f"\nScanning {account['AccountId']}\n")
        get_ali_resources(account=account, assumed_role_client=org_assumed_role_client, role_arn=org_role_arn)
    output_results(accounts)


if __name__ == '__main__':
    signal.signal(signal.SIGINT,signal_handler)
    main()
