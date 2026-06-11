#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines

""" Wiz : Resource Count : AWS """

import argparse
import concurrent.futures
import csv
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import boto3
    from botocore.config import Config
except ImportError:
    print("\nERROR: Missing required AWS SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade boto3 botocore")
    sys.exit(1)


version='2.12.0'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count AWS Resources')
parser.add_argument(
    '--all',
    action = 'store_true',
    dest = 'all',
    help = 'Count resources in all Accounts in the current AWS Organization (default: disabled)',
    default = False
)
parser.add_argument(
    '--id',
    dest = 'id',
    help = 'Count resources in the specified AWS Account (default: the ID of the current account)',
    default = None
)
parser.add_argument(
    '--accounts',
    action = 'store_true',
    dest = 'input_accounts',
    help = 'Count resources in the list of AWS Accounts (one ID per line) in a file named accounts.txt (default: disabled)',
    default = False
)
parser.add_argument(
    '--regions',
    action = 'store_true',
    dest = 'input_regions',
    help = 'Count resources in the list of AWS Regions (one per line) in a file named regions.txt (default: disabled)',
    default = False
)
parser.add_argument(
    '--role-name',
    action = 'store',
    dest = 'role_name',
    help = 'Specify the AWS IAM role name to use when assuming access to other AWS Accounts (default: OrganizationAccountAccessRole)',
    default = 'OrganizationAccountAccessRole'
)
parser.add_argument(
    '--no-data',
    action = 'store_false',
    dest = 'data_mode',
    help = 'Disable counting Wiz Cloud Data Security (Buckets) resources (default: enabled)',
    default = True
)
pgroup = parser.add_mutually_exclusive_group()
pgroup.add_argument(
    '--gov',
    action = 'store_true',
    dest = 'use_gov',
    help = 'Use GovCloud regions (default: disabled)',
    default = False
)
pgroup.add_argument(
    '--china',
    action = 'store_true',
    dest = 'use_china',
    help = 'Use China regions (default: disabled)',
    default = False
)
parser.add_argument(
    '--max-lambda-versions',
    action = 'store',
    dest = 'max_lambda_versions',
    help = 'Number of versions to count per Lambda Function (default: 5, range 0 to 10)',
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
    '--full-bucket-scan',
    action = 'store_true',
    dest = 'full_bucket_scan',
    help = 'Check each S3 bucket for public access block settings and only count publicly accessible buckets as Application Endpoints (default: disabled, counts all buckets)',
    default = False
)
parser.add_argument(
    '--port-scan',
    action='store_true',
    dest='port_scan',
    help='Scan discovered public IPs with masscan for open ports to improve AE accuracy (requires masscan + sudo)',
    default=False
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
    '--profile',
    dest = 'profile',
    help = 'AWS CLI profile name to use for authentication (default: the default profile)',
    default = None
)
args = parser.parse_args()

if args.max_lambda_versions < 0 or args.max_lambda_versions > 10:
    print(f"ERROR: --max-lambda-versions {args.max_lambda_versions} out of range: [0 .. 10]")
    sys.exit(1)
if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)

if args.profile:
    boto3.setup_default_session(profile_name=args.profile)


####
# Configuration and Globals
####

accounts_file   = 'accounts.txt'
regions_file    = 'regions.txt'
output_file     = 'aws-resources.csv'
output_file_log = 'aws-resources-log.csv'
output_file_ips = 'aws-vm-public-ips.csv'
output_file_domains = 'aws-public-domains.csv'
error_log_file  = 'aws-errors-log.txt'
padding = 6

# Map command-line arguments to counts to execute and display.
enabled = {
    'Virtual Machines':             True,
    'Serverless Functions':         True,
    'Elastic IP Addresses':         True,
    'Load Balancers':               True,
    'CloudFront Distributions':     True,
    'EKS Clusters':                 True,
    'Global Accelerators':          not (args.use_gov or args.use_china),
    'Route53 Hosted Zones':         True,
    'Route53 DNS Records':          True,
    'API Gateways':                 True,
    'Data Buckets':                 args.data_mode,
    'SageMaker Endpoints':          True,
    'Virtual Machine Sensors':      True,
}

totals = {
    'Virtual Machines':              0,
    'Serverless Functions':          0,
    'Elastic IP Addresses':          0,
    'Load Balancers':                0,
    'CloudFront Distributions':      0,
    'EKS Clusters':                  0,
    'Global Accelerators':           0,
    'Route53 Hosted Zones':          0,
    'Route53 DNS Records':           0,
    'API Gateways':                  0,
    'Data Buckets':                  0,
    'SageMaker Endpoints':           0,
    'Virtual Machine Sensors':       0,
}

totals_log = []
errors_log = []

vm_public_ips = []
vm_public_ips_lock = threading.Lock()

public_domains = []
public_domains_lock = threading.Lock()
totals_lock = threading.Lock()
totals_log_lock = threading.Lock()
errors_log_lock = threading.Lock()


try:
    aws_api_config = Config(
        retries = {
            'max_attempts' : 10,
            'mode'         : 'adaptive'
        }
    )
except Exception as ex0:  # pylint: disable=broad-exception-caught
    print("\nERROR: ")
    print(ex0)
    print("Unable to authenticate. Please verify your configuration")
    sys.exit(1)


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
    print(' '.join(f"- {rc} {resource_type} in {region} {details}".split()))
    with totals_log_lock:
        totals_log.append([resource_type, resource_count, account, region])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


# Common service + web + app ports for Wiz ASM endpoint estimation.
# Derived from actual Wiz AE port distribution analysis.
SCAN_PORTS = "21-22,25,53,80-81,110,143,389,443,445,465,587,993,995,1433,1521,3000,3306,3389,4443-4444,5000,5432,5900,6443,8000,8008-8009,8080-8094,8443,8888-8889,9000,9090,9200,9443,9997"


def check_masscan():
    """ Check if masscan is installed and accessible via PATH """
    if not shutil.which("masscan"):
        print("\nERROR: masscan is not installed or not in PATH.")
        print("Install it using your package manager:")
        if sys.platform == 'darwin':
            print("  brew install masscan")
        else:
            # Suggest based on available package managers
            if shutil.which("apt-get"):
                print("  sudo apt-get install masscan")
            elif shutil.which("dnf"):
                print("  sudo dnf install masscan")
            elif shutil.which("yum"):
                print("  sudo yum install masscan")
            elif shutil.which("pacman"):
                print("  sudo pacman -S masscan")
            else:
                print("  See https://github.com/robertdavidgraham/masscan#building")
        sys.exit(1)


def parse_masscan_json(filepath):
    """ Parse masscan's JSON output (which has broken trailing commas) """
    results = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
        if not raw:
            return results
        # Remove the {finished: 1} line masscan appends
        lines = [line for line in raw.splitlines()
                 if not line.strip().startswith('{') or '"ip"' in line or line.strip() == '[' or line.strip() == ']']
        # Rejoin and fix trailing commas before ]
        text = '\n'.join(lines)
        # Remove trailing comma before closing bracket
        text = text.rstrip().rstrip(',')
        if not text.endswith(']'):
            text += '\n]'
        data = json.loads(text)
        for entry in data:
            ip = entry.get('ip', '')
            for port_info in entry.get('ports', []):
                port = port_info.get('port')
                if ip and port:
                    results.setdefault(ip, set()).add(port)
    except (json.JSONDecodeError, KeyError, TypeError) as ex:
        verbose_print(f"Warning: Could not parse masscan output: {ex}")
    return results


def run_port_scan(ips):
    """ Scan a set of IPs with masscan for open ports. Returns dict[str, set[int]]. """
    if not ips:
        return {}
    ip_file = None
    out_file = None
    try:
        ip_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        for ip in sorted(ips):
            ip_file.write(ip + '\n')
        ip_file.close()

        out_file = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        out_file.close()

        cmd = [
            'sudo', 'masscan',
            '-iL', ip_file.name,
            '-p', SCAN_PORTS,
            '--rate', '100',
            '--wait', '3',
            '-oJ', out_file.name
        ]
        verbose_print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode != 0 and result.stderr:
            stderr_lower = result.stderr.lower()
            if 'password' in stderr_lower or 'terminal' in stderr_lower or 'askpass' in stderr_lower:
                print(f"\nERROR: sudo requires a password. Run with passwordless sudo or use: sudo -v")
                print(f"  {result.stderr.strip()}")
                return {}
            if 'permission' in stderr_lower or 'denied' in stderr_lower or 'error' in stderr_lower:
                print(f"\nWARNING: masscan stderr: {result.stderr.strip()}")

        return parse_masscan_json(out_file.name)
    except subprocess.TimeoutExpired:
        print("\nWARNING: masscan timed out after 300 seconds. Partial results may be used.")
        if out_file:
            return parse_masscan_json(out_file.name)
        return {}
    except FileNotFoundError:
        print("\nERROR: sudo or masscan not found.")
        return {}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        print(f"\nERROR: masscan failed: {ex}")
        return {}
    finally:
        if ip_file and os.path.exists(ip_file.name):
            os.unlink(ip_file.name)
        if out_file and os.path.exists(out_file.name):
            os.unlink(out_file.name)


def error_print(details, account=''):
    """ Error output """
    account  = f"Account: {account} " if account else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {account} {function} {details}\n")
    with errors_log_lock:
        errors_log.append(f"ERROR: {account} {function} {details}")


def increment_total(resource_type, count):
    """ Thread-safe totals increment """
    with totals_lock:
        totals[resource_type] += count


####
# Customized Library Code
####


# Pagination:
# Some AWS services use NextToken, nextToken, Marker, or Marker/NextMarker:
# https://github.com/iann0036/aws-pagination-rules/blob/master/README.md
# See also: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/paginators.html
def select_default_region():
    """ Select the default region based upon environment (aws, aws-cn, aws-us-gov) """
    if args.use_gov:
        return 'us-gov-east-1'
    if args.use_china:
        return 'cn-north-1'
    return 'us-east-1'


def tag_in_tags(tag_key, tag_value, tags):
    """ Check for tag key and value """
    if not tags:
        return False
    for tag in tags:
        if tag['Key'] == tag_key and tag['Value'] == tag_value:
            return True
    return False


# Subscriptions (aka AWS Accounts)
def get_aws_organization():
    """ Get Active Accounts in an AWS Organization """
    root_account_id = None
    accounts = []
    RESTORE_AWS_STS_REGIONAL_ENDPOINTS = os.environ.pop('AWS_STS_REGIONAL_ENDPOINTS', None)
    try:
        os.environ['AWS_STS_REGIONAL_ENDPOINTS'] = 'regional'
        client = boto3.client('organizations', region_name=select_default_region(), config=aws_api_config)
        root_account_id = client.describe_organization()['Organization']['MasterAccountId']
        response = client.list_accounts()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting AWS Organization.")
        if RESTORE_AWS_STS_REGIONAL_ENDPOINTS is None:
            del os.environ['AWS_STS_REGIONAL_ENDPOINTS']
        else:
            os.environ['AWS_STS_REGIONAL_ENDPOINTS'] = RESTORE_AWS_STS_REGIONAL_ENDPOINTS
        return root_account_id, accounts
    for account in response['Accounts']:
        verbose_print(f"account: {account}")
        if account['Status'] != 'ACTIVE':
            continue
        accounts.append(account)
    while 'NextToken' in response:
        response = client.list_accounts(NextToken=response['NextToken'])
        for account in response['Accounts']:
            verbose_print(f"account: {account}")
            if account['Status'] != 'ACTIVE':
                continue
            accounts.append(account)
    if RESTORE_AWS_STS_REGIONAL_ENDPOINTS is None:
        del os.environ['AWS_STS_REGIONAL_ENDPOINTS']
    else:
        os.environ['AWS_STS_REGIONAL_ENDPOINTS'] = RESTORE_AWS_STS_REGIONAL_ENDPOINTS
    return root_account_id, accounts


def is_sso_token_error(ex):
    """ Check if an exception is an SSO token error """
    ex_type = type(ex).__name__
    ex_str = str(ex)
    sso_error_types = {'SSOTokenLoadError', 'UnauthorizedSSOTokenError', 'SSOError', 'TokenRetrievalError'}
    if ex_type in sso_error_types:
        return True
    if 'Error loading SSO Token' in ex_str or 'expired' in ex_str.lower() and 'sso' in ex_str.lower():
        return True
    return False


def run_sso_login(profile):
    """ Run aws sso login for the given profile """
    print(f"\nSSO token expired or not found for profile '{profile}'.")
    print(f"Running: aws sso login --profile {profile}\n")
    try:
        result = subprocess.run(['aws', 'sso', 'login', '--profile', profile], check=False)
        if result.returncode == 0:
            boto3.setup_default_session(profile_name=profile)
            return True
        print("\nERROR: SSO login failed.")
        return False
    except FileNotFoundError:
        print("\nERROR: AWS CLI not found. Install the AWS CLI and run 'aws sso login' manually.")
        return False


def get_aws_account():
    """ Get AWS Account (UserId, Account, Arn) """
    try:
        client = boto3.client('sts')
        account = client.get_caller_identity()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        if args.profile and is_sso_token_error(ex):
            if run_sso_login(args.profile):
                try:
                    client = boto3.client('sts')
                    account = client.get_caller_identity()
                    verbose_print(f"account: {account}")
                    return account
                except Exception as ex2:  # pylint: disable=broad-exception-caught
                    error_print(ex2)
                    error_print("Error getting current AWS Account after SSO login.")
                    return None
            return None
        error_print(ex)
        error_print("Error getting current AWS Account.")
        return None
    verbose_print(f"account: {account}")
    return account


def get_aws_accounts_from_file():
    """Get the list of AWS Accounts """
    accounts = []
    if os.path.isfile(accounts_file):
        try:
            with open(accounts_file, 'r', encoding='utf-8') as file:
                for line in file:
                    account_id = line.strip()
                    # Verify the AWS Account ID is 12 digits.
                    if account_id and account_id.isdigit() and len(account_id) == 12:
                        accounts.append({'Id': account_id, 'Name': account_id})
                    else:
                        print(f"Skipping invalid Account ID from {accounts_file}: {account_id}")
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
            print("Error getting AWS Accounts from file.")
            print("Exiting...")
            sys.exit(1)
    else:
        print("Input file does not exist.")
        print(f"Create a file named {accounts_file} and add each AWS Account ID to scan, one per line.")
        print("Exiting...")
        sys.exit(1)
    return accounts


def aws_get_credentials(target_account_id, current_account_id, root_account_id):
    """ Get AWS Credentials to access the target account """
    # pylint: disable=consider-using-in
    if target_account_id == current_account_id or target_account_id == root_account_id:
        try:
            session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
            credentials = session.get_credentials()
            credentials = credentials.get_frozen_credentials()
            return {
                'AccessKeyId':     credentials.access_key,
                'SecretAccessKey': credentials.secret_key,
                'SessionToken':    credentials.token
            }
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, target_account_id)
            return None
    try:
        client = boto3.client('sts', config=aws_api_config)
        aws_partition = "arn:aws:iam::"
        if args.use_gov:
            aws_partition = "arn:aws-us-gov:iam::"
        elif args.use_china:
            aws_partition = "arn:aws-cn:iam::"
        assumed_role_object = client.assume_role(
            # Example: arn:aws:iam::123456789012:role/MyRoleName
            RoleArn=aws_partition + str(target_account_id) + ':role/' + args.role_name,
            RoleSessionName='Session1'
        )
        credentials = assumed_role_object['Credentials']
        return {
            'AccessKeyId':     credentials['AccessKeyId'],
            'SecretAccessKey': credentials['SecretAccessKey'],
            'SessionToken':    credentials['SessionToken']
        }
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, target_account_id)
        return None


def get_aws_regions(credentials):
    """ Get AWS Regions, using the "default" AWS region for the partition (aws, aws-cn, aws-us-gov) """
    client = get_aws_client('ec2', select_default_region(), credentials)
    try: # pylint: disable=broad-exception-caught
        response = client.describe_regions(AllRegions=False)
        regions = response['Regions']
        regions = sorted(regions, key=lambda d: d['RegionName'])
        verbose_print(f"regions: {regions}")
        return regions
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting AWS Regions.")
        return []


def get_aws_regions_from_file():
    """Get the list of AWS Regions """
    regions = []
    if os.path.isfile(regions_file):
        try:
            with open(regions_file, 'r', encoding='utf-8') as file:
                for line in file:
                    region = line.strip()
                    regions.append({'RegionName': region})
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex)
            print("Error getting AWS Regions from file.")
            print("Exiting...")
            sys.exit(1)
    else:
        print("Regions file does not exist.")
        print(f"Create a file named {regions_file} and add each AWS Region to scan, one per line.")
        print("Exiting...")
        sys.exit(1)
    return regions


def get_aws_client(service, region, credentials):
    """ Return an AWS Client """
    try:
        client = boto3.client(
            service,
            region_name           = region,
            config                = aws_api_config,
            aws_access_key_id     = credentials['AccessKeyId'],
            aws_secret_access_key = credentials['SecretAccessKey'],
            aws_session_token     = credentials['SessionToken']
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        print("Error getting AWS Client.")
        print("Exiting...")
        sys.exit(1)
    return client


def is_access_denied(ex):
    """ Check if an exception is an access denied / permissions error """
    error_code = getattr(ex, 'response', {}).get('Error', {}).get('Code', '')
    access_denied_codes = {
        'AccessDenied', 'AccessDeniedException', 'UnauthorizedAccess',
        'UnauthorizedOperation', 'AuthorizationError', 'ForbiddenException',
        'InsufficientPrivilegesException',
    }
    return error_code in access_denied_codes


def handle_api_error(ex, account=''):
    """ Handle API errors — suppress access denied console output unless in debug/verbose mode """
    if is_access_denied(ex) and not (args.debug_mode or args.verbose_mode):
        # Still record in error log file for diagnostics
        account_str = f"Account: {account} " if account else ""
        try:
            function = f"{inspect.stack()[1].function}()"
        except Exception:  # pylint: disable=broad-exception-caught
            function = ''
        try:
            details = str(ex).replace("\n", " ").replace("\r", " ")
        except Exception:  # pylint: disable=broad-exception-caught
            details = str(ex)
        with errors_log_lock:
            errors_log.append(f"ERROR: {account_str} {function} {details}")
        return
    error_print(ex, account)


def validate_permissions(credentials):
    """ Validate AWS permissions and disable resource types that cannot be accessed.
        Returns a list of (permission, [affected_resource_types]) for missing permissions. """
    region = select_default_region()
    missing = []

    # ec2:DescribeRegions — required to discover available regions
    if not args.input_regions:
        try:
            client = get_aws_client('ec2', region, credentials)
            client.describe_regions(AllRegions=False)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                print("\nERROR: Missing permission ec2:DescribeRegions.")
                print("Cannot discover AWS regions. Use --regions with a regions.txt file, or grant ec2:DescribeRegions.")
                print("Exiting...")
                sys.exit(1)

    # ec2:DescribeInstances -> Virtual Machines
    if enabled['Virtual Machines']:
        try:
            client = get_aws_client('ec2', region, credentials)
            client.describe_instances(MaxResults=5)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('ec2:DescribeInstances', ['Virtual Machines', 'Virtual Machine Sensors']))
                enabled['Virtual Machines'] = False

    # ec2:DescribeAddresses -> Elastic IP Addresses
    if enabled['Elastic IP Addresses']:
        try:
            client = get_aws_client('ec2', region, credentials)
            client.describe_addresses()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('ec2:DescribeAddresses', ['Elastic IP Addresses']))
                enabled['Elastic IP Addresses'] = False

    # lambda:ListFunctions -> Serverless Functions
    if enabled['Serverless Functions']:
        try:
            client = get_aws_client('lambda', region, credentials)
            client.list_functions(MaxItems=1)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('lambda:ListFunctions', ['Serverless Functions']))
                enabled['Serverless Functions'] = False

    # route53:ListHostedZones -> Route53 Hosted Zones and Route53 DNS Records
    if enabled['Route53 Hosted Zones'] or enabled['Route53 DNS Records']:
        try:
            client = get_aws_client('route53', region, credentials)
            client.list_hosted_zones(MaxItems='1')
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                affected = []
                if enabled['Route53 Hosted Zones']:
                    affected.append('Route53 Hosted Zones')
                    enabled['Route53 Hosted Zones'] = False
                if enabled['Route53 DNS Records']:
                    affected.append('Route53 DNS Records')
                    enabled['Route53 DNS Records'] = False
                missing.append(('route53:ListHostedZones', affected))

    # apigateway:GET -> API Gateways (REST APIs)
    if enabled['API Gateways']:
        try:
            client = get_aws_client('apigateway', region, credentials)
            client.get_rest_apis(limit=1)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('apigateway:GET', ['API Gateways']))
                enabled['API Gateways'] = False

    # apigatewayv2:GetApis -> API Gateways (HTTP APIs)
    if enabled['API Gateways']:
        try:
            client = get_aws_client('apigatewayv2', region, credentials)
            client.get_apis(MaxResults='1')
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('apigatewayv2:GetApis', ['API Gateways']))
                enabled['API Gateways'] = False

    # elasticloadbalancing:DescribeLoadBalancers -> Load Balancers
    if enabled['Load Balancers']:
        try:
            client = get_aws_client('elbv2', region, credentials)
            client.describe_load_balancers(PageSize=1)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('elasticloadbalancing:DescribeLoadBalancers', ['Load Balancers']))
                enabled['Load Balancers'] = False

    # cloudfront:ListDistributions -> CloudFront Distributions
    if enabled['CloudFront Distributions']:
        try:
            client = get_aws_client('cloudfront', region, credentials)
            client.list_distributions(MaxItems='1')
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('cloudfront:ListDistributions', ['CloudFront Distributions']))
                enabled['CloudFront Distributions'] = False

    # eks:ListClusters -> EKS Clusters
    if enabled['EKS Clusters']:
        try:
            client = get_aws_client('eks', region, credentials)
            client.list_clusters(maxResults=1)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('eks:ListClusters', ['EKS Clusters']))
                enabled['EKS Clusters'] = False

    # globalaccelerator:ListAccelerators -> Global Accelerators
    if enabled['Global Accelerators']:
        try:
            client = get_aws_client('globalaccelerator', 'us-west-2', credentials)
            client.list_accelerators(MaxResults=1)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('globalaccelerator:ListAccelerators', ['Global Accelerators']))
                enabled['Global Accelerators'] = False

    # sagemaker:ListEndpoints -> SageMaker Endpoints
    if enabled['SageMaker Endpoints']:
        try:
            client = get_aws_client('sagemaker', region, credentials)
            client.list_endpoints(MaxResults=1)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('sagemaker:ListEndpoints', ['SageMaker Endpoints']))
                enabled['SageMaker Endpoints'] = False

    # s3:ListAllMyBuckets -> Data Buckets
    if enabled['Data Buckets']:
        try:
            client = get_aws_client('s3', region, credentials)
            client.list_buckets()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if is_access_denied(ex):
                missing.append(('s3:ListAllMyBuckets', ['Data Buckets']))
                enabled['Data Buckets'] = False

    return missing


# Virtual Machines: EC2 Instances
def get_aws_ec2_instances(region, credentials, account):
    """ Get AWS EC2 Instances in the specified Account and Region """
    instances_count = 0
    linux_instances_count = 0
    public_ip_entries = []  # Local list: collect before SG analysis
    client = get_aws_client('ec2', region, credentials)
    try:
        response = client.describe_instances(MaxResults=1000)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return

    while True:
        for reservation in response.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                verbose_print(f"instance: {instance}")
                if instance['State']['Name'] == 'terminated':
                    continue
                if tag_in_tags('Vendor', 'Databricks', instance.get('Tags', {})):
                    verbose_print(f"Skipping Databricks instance: {instance['Tags']}")
                    continue
                instances_count += 1
                if 'PublicIpAddress' in instance:
                    sg_ids = [sg['GroupId'] for sg in instance.get('SecurityGroups', [])]
                    public_ip_entries.append({
                        'instance_id': instance['InstanceId'],
                        'public_ip': instance['PublicIpAddress'],
                        'sg_ids': sg_ids,
                    })
                if 'PlatformDetails' in instance and 'win' not in instance['PlatformDetails'].lower():
                    linux_instances_count += 1

        if 'NextToken' not in response:
            break
        try:
            response = client.describe_instances(NextToken=response['NextToken'], MaxResults=1000)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break

    # Analyze security groups: check which SGs allow inbound from the internet
    # (0.0.0.0/0 or ::/0). Instances without internet-accessible SGs will not
    # create Application Endpoints even if they have a public IP.
    all_sg_ids = set()
    for entry in public_ip_entries:
        all_sg_ids.update(entry['sg_ids'])

    internet_accessible_sgs = set()
    if all_sg_ids:
        try:
            sg_id_list = list(all_sg_ids)
            for i in range(0, len(sg_id_list), 200):
                batch = sg_id_list[i:i + 200]
                sg_response = client.describe_security_groups(GroupIds=batch)
                for sg in sg_response['SecurityGroups']:
                    for rule in sg.get('IpPermissions', []):
                        # Only count TCP, UDP, or all-protocols rules (-1).
                        # ICMP-only rules don't create Application Endpoints.
                        proto = str(rule.get('IpProtocol', ''))
                        if proto not in ('-1', 'tcp', 'udp', '6', '17'):
                            continue
                        if any(r.get('CidrIp') == '0.0.0.0/0' for r in rule.get('IpRanges', [])):
                            internet_accessible_sgs.add(sg['GroupId'])
                        if any(r.get('CidrIpv6') == '::/0' for r in rule.get('Ipv6Ranges', [])):
                            internet_accessible_sgs.add(sg['GroupId'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            # If SG check fails, conservatively assume all are accessible
            verbose_print(f"Warning: Could not check security groups in {region}: {ex}")
            internet_accessible_sgs = all_sg_ids

    # Add instances to shared vm_public_ips with internet_accessible flag
    for entry in public_ip_entries:
        is_accessible = bool(internet_accessible_sgs & set(entry['sg_ids']))
        with vm_public_ips_lock:
            vm_public_ips.append({
                'instance_id': entry['instance_id'],
                'public_ip': entry['public_ip'],
                'type': 'EC2',
                'region': region,
                'account': account['Name'],
                'internet_accessible': is_accessible,
            })
        # Wiz creates a separate Application Endpoint for the EC2 reverse DNS name.
        # DNS resolution works independently of security groups, so Wiz discovers
        # these for ALL EC2 instances with public IPs.
        # Format: ec2-{ip}.compute-1.amazonaws.com (us-east-1) or
        #         ec2-{ip}.{region}.compute.amazonaws.com (other regions)
        ip_dashed = entry['public_ip'].replace('.', '-')
        if region == 'us-east-1':
            ec2_dns = f"ec2-{ip_dashed}.compute-1.amazonaws.com"
        else:
            ec2_dns = f"ec2-{ip_dashed}.{region}.compute.amazonaws.com"
        with public_domains_lock:
            public_domains.append({
                'resource_id': entry['instance_id'],
                'domain': ec2_dns,
                'type': 'EC2-DNS',
                'region': region,
                'account': account['Name']
            })

    if instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_count, resource_type='Virtual Machines [EC2]', region=region, account=account['Name'])
        increment_total('Virtual Machines', instances_count)
        increment_total('Virtual Machine Sensors', linux_instances_count)


# Serverless Functions: Lambda Functions
def get_aws_lambda_functions(region, credentials, account):
    """ Get AWS Lambda Functions in the specified Account """
    serverless_functions_count = 0
    client = get_aws_client('lambda', region, credentials)
    try:
        response = client.list_functions(MaxItems=1000)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        response = {}
        handle_api_error(ex, account['Id'])
        return
    functions = response['Functions']
    serverless_functions_count += len(functions)
    while 'NextMarker' in response:
        try:
            response = client.list_functions(Marker=response['NextMarker'], MaxItems=1000)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            response = {}
            handle_api_error(ex, account['Id'])
            continue
        functions.extend(response['Functions'])
        serverless_functions_count += len(response['Functions'])

    # Collect Lambda function URLs
    for function in functions:
        function_name = function['FunctionName']
        try:
            url_config = client.get_function_url_config(FunctionName=function_name)
            function_url = url_config.get('FunctionUrl', '')
            if function_url:
                # Extract domain from URL (remove https:// and trailing /)
                domain = function_url.replace('https://', '').rstrip('/')
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': function_name,
                        'domain': domain,
                        'type': 'Lambda',
                        'region': region,
                        'account': account['Name']
                    })
        except client.exceptions.ResourceNotFoundException:
            # Function URL not configured for this function
            pass
        except Exception as ex:  # pylint: disable=broad-exception-caught
            verbose_print(f"Error getting function URL for {function_name}: {ex}")

    serverless_functions_versions_count = 0
    # Wiz inspects a default of 5 (new Tenants) up to 10 (via Settings) versions.
    if args.max_lambda_versions > 0:
        for function in functions:
            versions = get_aws_lambda_function_versions(account, region, credentials, function['FunctionArn'])
            versions_count = min(args.max_lambda_versions, len(versions))
            serverless_functions_versions_count += versions_count

    if serverless_functions_count > 0 or args.verbose_mode:
        serverless_functions_count += serverless_functions_versions_count
        progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [Lambda]', region=region, account=account['Name'])
        increment_total('Serverless Functions', serverless_functions_count)


# Serverless Functions: Lambda Function Versions
def get_aws_lambda_function_versions(account, region, credentials, function_arn):
    """ Get AWS Lambda Function Versions for the specified Function """
    versions = []
    client = get_aws_client('lambda', region, credentials)
    try:
        response = client.list_versions_by_function(FunctionName=function_arn, MaxItems=args.max_lambda_versions)
        versions.extend(response['Versions'])
        versions = [v for v in versions if v['Version'] != '$LATEST']
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
    return versions


# Elastic IP Addresses
def get_aws_elastic_ips(region, credentials, account):
    """ Get AWS Elastic IP Addresses in the specified Account and Region """
    eip_count = 0
    client = get_aws_client('ec2', region, credentials)
    try:
        response = client.describe_addresses()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return
    for address in response.get('Addresses', []):
        eip_count += 1
        if 'PublicIp' in address:
            # Track association status for accurate Application Endpoint estimation:
            # - EIPs on EC2 instances are already counted via describe_instances
            # - Unattached EIPs have no resource behind them
            associated_instance = address.get('InstanceId', '')
            association_id = address.get('AssociationId', '')
            with vm_public_ips_lock:
                vm_public_ips.append({
                    'instance_id': address.get('AllocationId', 'N/A'),
                    'public_ip': address['PublicIp'],
                    'type': 'EIP',
                    'region': region,
                    'account': account['Name'],
                    'associated_instance': associated_instance,
                    'associated': bool(association_id),
                })

    if eip_count > 0 or args.verbose_mode:
        progress_print(resource_count=eip_count, resource_type='Elastic IP Addresses [EIP]', region=region, account=account['Name'])
        increment_total('Elastic IP Addresses', eip_count)


# Route53 Hosted Zones
def get_aws_route53_hosted_zones(region, credentials, account):
    """ Get AWS Route53 Hosted Zones in the specified Account """
    zones_count = 0
    client = get_aws_client('route53', region, credentials)
    try:
        response = client.list_hosted_zones()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return
    zones_count += len(response.get('HostedZones', []))
    while response.get('IsTruncated', False):
        try:
            response = client.list_hosted_zones(Marker=response['NextMarker'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break
        zones_count += len(response.get('HostedZones', []))

    if zones_count > 0 or args.verbose_mode:
        progress_print(resource_count=zones_count, resource_type='Route53 Hosted Zones', region=region, account=account['Name'])
        increment_total('Route53 Hosted Zones', zones_count)


# Route53 DNS Records
def get_aws_route53_dns_records(region, credentials, account):
    """ Get AWS Route53 DNS Records in the specified Account """
    records_count = 0
    hosted_zones = []
    client = get_aws_client('route53', region, credentials)

    # First, get all hosted zones
    try:
        response = client.list_hosted_zones()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return
    hosted_zones.extend(response.get('HostedZones', []))
    while response.get('IsTruncated', False):
        try:
            response = client.list_hosted_zones(Marker=response['NextMarker'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break
        hosted_zones.extend(response.get('HostedZones', []))

    # For each hosted zone, count the DNS records
    for zone in hosted_zones:
        zone_id = zone['Id']
        zone_name = zone.get('Name', '').rstrip('.')
        try:
            response = client.list_resource_record_sets(HostedZoneId=zone_id)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            continue
        for record in response.get('ResourceRecordSets', []):
            records_count += 1
            record_name = record.get('Name', '').rstrip('.')
            record_type = record.get('Type', '')
            # Only collect A, PTR, and CNAME records for public domains
            if record_type in ('A', 'PTR', 'CNAME'):
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': f"{record_name} ({record_type})",
                        'domain': record_name,
                        'type': 'Route53',
                        'region': zone_name,
                        'account': account['Name']
                    })
        while response.get('IsTruncated', False):
            try:
                response = client.list_resource_record_sets(
                    HostedZoneId=zone_id,
                    StartRecordName=response['NextRecordName'],
                    StartRecordType=response['NextRecordType']
                )
            except Exception as ex:  # pylint: disable=broad-exception-caught
                handle_api_error(ex, account['Id'])
                break
            for record in response.get('ResourceRecordSets', []):
                records_count += 1
                record_name = record.get('Name', '').rstrip('.')
                record_type = record.get('Type', '')
                # Only collect A, PTR, and CNAME records for public domains
                if record_type in ('A', 'PTR', 'CNAME'):
                    with public_domains_lock:
                        public_domains.append({
                            'resource_id': f"{record_name} ({record_type})",
                            'domain': record_name,
                            'type': 'Route53',
                            'region': zone_name,
                            'account': account['Name']
                        })

    if records_count > 0 or args.verbose_mode:
        progress_print(resource_count=records_count, resource_type='Route53 DNS Records', region=region, account=account['Name'])
        increment_total('Route53 DNS Records', records_count)


# API Gateways: REST APIs and HTTP APIs
def _rest_api_has_stages(client, api_id):
    """Check if a REST API has at least one deployed stage."""
    try:
        stages = client.get_stages(restApiId=api_id)
        return bool(stages.get('item'))
    except Exception:  # pylint: disable=broad-exception-caught
        return True  # Conservatively assume deployed if check fails


def _http_api_has_stages(client_v2, api_id):
    """Check if an HTTP API has at least one deployed stage."""
    try:
        stages = client_v2.get_stages(ApiId=api_id)
        return bool(stages.get('Items'))
    except Exception:  # pylint: disable=broad-exception-caught
        return True  # Conservatively assume deployed if check fails


def get_aws_api_gateways(region, credentials, account):
    """ Get AWS API Gateways (REST and HTTP APIs) in the specified Account and Region """
    api_count = 0

    # Get REST APIs (API Gateway v1)
    client = get_aws_client('apigateway', region, credentials)
    all_rest_apis = []
    try:
        response = client.get_rest_apis(limit=500)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        response = {}
        handle_api_error(ex, account['Id'])

    all_rest_apis.extend(response.get('items', []))
    while 'position' in response:
        try:
            response = client.get_rest_apis(limit=500, position=response['position'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            response = {}
            handle_api_error(ex, account['Id'])
            break
        all_rest_apis.extend(response.get('items', []))

    for api in all_rest_apis:
        api_id = api.get('id', '')
        api_name = api.get('name', '')
        # Skip APIs without deployed stages (unreachable, no Application Endpoint)
        if not _rest_api_has_stages(client, api_id):
            verbose_print(f"Skipping REST API {api_name} ({api_id}): no deployed stages")
            continue
        api_count += 1
        api_domain = f"{api_id}.execute-api.{region}.amazonaws.com"
        with public_domains_lock:
            public_domains.append({
                'resource_id': f"{api_name} ({api_id})",
                'domain': api_domain,
                'type': 'API Gateway (REST)',
                'region': region,
                'account': account['Name']
            })

    # Get HTTP APIs (API Gateway v2)
    client_v2 = get_aws_client('apigatewayv2', region, credentials)
    all_http_apis = []
    try:
        response = client_v2.get_apis(MaxResults='500')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        response = {}
        handle_api_error(ex, account['Id'])

    all_http_apis.extend(response.get('Items', []))
    while 'NextToken' in response:
        try:
            response = client_v2.get_apis(MaxResults='500', NextToken=response['NextToken'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            response = {}
            handle_api_error(ex, account['Id'])
            break
        all_http_apis.extend(response.get('Items', []))

    for api in all_http_apis:
        api_id = api.get('ApiId', '')
        api_name = api.get('Name', '')
        # Skip APIs without deployed stages
        if not _http_api_has_stages(client_v2, api_id):
            verbose_print(f"Skipping HTTP API {api_name} ({api_id}): no deployed stages")
            continue
        api_count += 1
        api_endpoint = api.get('ApiEndpoint', '')
        if api_endpoint:
            api_domain = api_endpoint.replace('https://', '').replace('http://', '').rstrip('/')
        else:
            api_domain = f"{api_id}.execute-api.{region}.amazonaws.com"
        with public_domains_lock:
            public_domains.append({
                'resource_id': f"{api_name} ({api_id})",
                'domain': api_domain,
                'type': 'API Gateway (HTTP)',
                'region': region,
                'account': account['Name']
            })

    if api_count > 0 or args.verbose_mode:
        progress_print(resource_count=api_count, resource_type='API Gateways [REST & HTTP]', region=region, account=account['Name'])
        increment_total('API Gateways', api_count)


# Data Buckets: S3 Buckets
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 10000 S3 Buckets per Account.
def _is_bucket_public(client, bucket_name):
    """Check if an S3 bucket is potentially publicly accessible.
    Returns True if the bucket does NOT have full public access block enabled."""
    try:
        pab = client.get_public_access_block(Bucket=bucket_name)
        config = pab.get('PublicAccessBlockConfiguration', {})
        # If all four block settings are True, the bucket is fully protected
        if (config.get('BlockPublicAcls', False) and
                config.get('IgnorePublicAcls', False) and
                config.get('BlockPublicPolicy', False) and
                config.get('RestrictPublicBuckets', False)):
            return False
        return True
    except client.exceptions.NoSuchPublicAccessBlockConfiguration:
        # No public access block configured — bucket could be public
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        # If check fails, conservatively assume public
        return True


def _is_account_fully_blocked(credentials, account_id):
    """Check if the account-level S3 public access block is fully enabled.
    Returns True if all four block settings are enabled at the account level."""
    try:
        client = get_aws_client('s3control', 'us-east-1', credentials)
        pab = client.get_public_access_block(AccountId=account_id)
        config = pab.get('PublicAccessBlockConfiguration', {})
        return (config.get('BlockPublicAcls', False) and
                config.get('IgnorePublicAcls', False) and
                config.get('BlockPublicPolicy', False) and
                config.get('RestrictPublicBuckets', False))
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def get_aws_s3_buckets(region, credentials, account):
    """ Get AWS S3 Buckets in the specified Account """
    buckets_count = 0
    buckets = []
    client = get_aws_client('s3', region, credentials)
    try:
        response = client.list_buckets()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return
    buckets.extend(response['Buckets'])
    buckets_count += len(response['Buckets'])
    while 'NextToken' in response:
        try:
            response = client.list_buckets(NextToken=response['NextToken'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            response = {}
            handle_api_error(ex, account['Id'])
            continue
        buckets.extend(response['Buckets'])
        buckets_count += len(response['Buckets'])
    buckets_count = min(buckets_count, 10000)

    # When --full-bucket-scan is enabled, check public access block settings
    # to only count publicly accessible buckets as Application Endpoints.
    # First check account-level block (one API call); if fully blocked, skip all.
    account_blocked = False
    if args.full_bucket_scan:
        account_blocked = _is_account_fully_blocked(credentials, account['Id'])
        if account_blocked:
            verbose_print(f"Account {account['Id']} has full S3 public access block — all buckets are private")

    # Collect S3 bucket domain names
    public_bucket_count = 0
    for bucket in buckets[:10000]:
        bucket_name = bucket['Name']
        bucket_domain = f"{bucket_name}.s3.amazonaws.com"

        if args.full_bucket_scan:
            if account_blocked or not _is_bucket_public(client, bucket_name):
                verbose_print(f"Skipping private bucket: {bucket_name}")
                continue
            public_bucket_count += 1

        with public_domains_lock:
            public_domains.append({
                'resource_id': bucket_name,
                'domain': bucket_domain,
                'type': 'S3',
                'region': 'global',
                'account': account['Name']
            })

    if args.full_bucket_scan and (public_bucket_count > 0 or args.verbose_mode):
        verbose_print(f"S3 public access scan: {public_bucket_count}/{buckets_count} buckets are publicly accessible")

    if buckets_count > 0 or args.verbose_mode:
        progress_print(resource_count=buckets_count, resource_type='Data Buckets [S3]', region=region, account=account['Name'])
        increment_total('Data Buckets', buckets_count)


# Load Balancers: ALB, NLB (ELBv2) and Classic (ELB)
def get_aws_load_balancers(region, credentials, account):
    """ Get internet-facing AWS Load Balancers (ALB/NLB/CLB) in the specified Account and Region """
    lb_count = 0

    # ELBv2: Application and Network Load Balancers
    client = get_aws_client('elbv2', region, credentials)
    try:
        response = client.describe_load_balancers()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        response = {}
        handle_api_error(ex, account['Id'])

    for lb in response.get('LoadBalancers', []):
        if lb.get('Scheme') != 'internet-facing':
            continue
        lb_count += 1
        dns_name = lb.get('DNSName', '')
        lb_type = lb.get('Type', 'application').upper()
        lb_name = lb.get('LoadBalancerName', '')
        if lb_type == 'APPLICATION':
            lb_label = 'ALB'
        elif lb_type == 'NETWORK':
            lb_label = 'NLB'
        else:
            lb_label = lb_type
        if dns_name:
            with public_domains_lock:
                public_domains.append({
                    'resource_id': lb_name,
                    'domain': dns_name,
                    'type': lb_label,
                    'region': region,
                    'account': account['Name']
                })

    while response.get('NextMarker'):
        try:
            response = client.describe_load_balancers(Marker=response['NextMarker'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            response = {}
            handle_api_error(ex, account['Id'])
            continue
        for lb in response.get('LoadBalancers', []):
            if lb.get('Scheme') != 'internet-facing':
                continue
            lb_count += 1
            dns_name = lb.get('DNSName', '')
            lb_type = lb.get('Type', 'application').upper()
            lb_name = lb.get('LoadBalancerName', '')
            if lb_type == 'APPLICATION':
                lb_label = 'ALB'
            elif lb_type == 'NETWORK':
                lb_label = 'NLB'
            else:
                lb_label = lb_type
            if dns_name:
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': lb_name,
                        'domain': dns_name,
                        'type': lb_label,
                        'region': region,
                        'account': account['Name']
                    })

    # Classic Load Balancers (ELB)
    client_classic = get_aws_client('elb', region, credentials)
    try:
        response = client_classic.describe_load_balancers()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        response = {}
        handle_api_error(ex, account['Id'])

    for lb in response.get('LoadBalancerDescriptions', []):
        if lb.get('Scheme') != 'internet-facing':
            continue
        lb_count += 1
        dns_name = lb.get('DNSName', '')
        lb_name = lb.get('LoadBalancerName', '')
        if dns_name:
            with public_domains_lock:
                public_domains.append({
                    'resource_id': lb_name,
                    'domain': dns_name,
                    'type': 'CLB',
                    'region': region,
                    'account': account['Name']
                })

    while response.get('NextMarker'):
        try:
            response = client_classic.describe_load_balancers(Marker=response['NextMarker'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            response = {}
            handle_api_error(ex, account['Id'])
            continue
        for lb in response.get('LoadBalancerDescriptions', []):
            if lb.get('Scheme') != 'internet-facing':
                continue
            lb_count += 1
            dns_name = lb.get('DNSName', '')
            lb_name = lb.get('LoadBalancerName', '')
            if dns_name:
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': lb_name,
                        'domain': dns_name,
                        'type': 'CLB',
                        'region': region,
                        'account': account['Name']
                    })

    if lb_count > 0 or args.verbose_mode:
        progress_print(resource_count=lb_count, resource_type='Load Balancers [ALB/NLB/CLB]', region=region, account=account['Name'])
        increment_total('Load Balancers', lb_count)


# CloudFront Distributions
def get_aws_cloudfront_distributions(region, credentials, account):
    """ Get AWS CloudFront Distributions in the specified Account """
    cf_count = 0
    client = get_aws_client('cloudfront', region, credentials)
    try:
        response = client.list_distributions()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return

    dist_list = response.get('DistributionList', {})
    for dist in dist_list.get('Items', []):
        cf_count += 1
        dist_id = dist.get('Id', '')
        domain_name = dist.get('DomainName', '')
        if domain_name:
            with public_domains_lock:
                public_domains.append({
                    'resource_id': dist_id,
                    'domain': domain_name,
                    'type': 'CloudFront',
                    'region': 'global',
                    'account': account['Name']
                })
        # Also collect CNAME aliases
        for alias in dist.get('Aliases', {}).get('Items', []):
            with public_domains_lock:
                public_domains.append({
                    'resource_id': f"{dist_id} (alias)",
                    'domain': alias,
                    'type': 'CloudFront',
                    'region': 'global',
                    'account': account['Name']
                })

    while dist_list.get('IsTruncated', False):
        try:
            response = client.list_distributions(Marker=dist_list['NextMarker'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break
        dist_list = response.get('DistributionList', {})
        for dist in dist_list.get('Items', []):
            cf_count += 1
            dist_id = dist.get('Id', '')
            domain_name = dist.get('DomainName', '')
            if domain_name:
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': dist_id,
                        'domain': domain_name,
                        'type': 'CloudFront',
                        'region': 'global',
                        'account': account['Name']
                    })
            for alias in dist.get('Aliases', {}).get('Items', []):
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': f"{dist_id} (alias)",
                        'domain': alias,
                        'type': 'CloudFront',
                        'region': 'global',
                        'account': account['Name']
                    })

    if cf_count > 0 or args.verbose_mode:
        progress_print(resource_count=cf_count, resource_type='CloudFront Distributions', region=region, account=account['Name'])
        increment_total('CloudFront Distributions', cf_count)


# EKS Clusters
def get_aws_eks_clusters(region, credentials, account):
    """ Get AWS EKS Clusters with public API endpoints in the specified Account and Region """
    eks_count = 0
    cluster_names = []
    client = get_aws_client('eks', region, credentials)
    try:
        response = client.list_clusters()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return
    cluster_names.extend(response.get('clusters', []))

    while response.get('nextToken'):
        try:
            response = client.list_clusters(nextToken=response['nextToken'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break
        cluster_names.extend(response.get('clusters', []))

    for cluster_name in cluster_names:
        try:
            desc = client.describe_cluster(name=cluster_name)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            continue
        cluster = desc.get('cluster', {})
        resources_vpc = cluster.get('resourcesVpcConfig', {})
        if not resources_vpc.get('endpointPublicAccess', False):
            continue
        eks_count += 1
        endpoint = cluster.get('endpoint', '')
        if endpoint:
            domain = endpoint.replace('https://', '').rstrip('/')
            with public_domains_lock:
                public_domains.append({
                    'resource_id': cluster_name,
                    'domain': domain,
                    'type': 'EKS',
                    'region': region,
                    'account': account['Name']
                })

    if eks_count > 0 or args.verbose_mode:
        progress_print(resource_count=eks_count, resource_type='EKS Clusters', region=region, account=account['Name'])
        increment_total('EKS Clusters', eks_count)


# Global Accelerators
def get_aws_global_accelerators(region, credentials, account):
    """ Get AWS Global Accelerators in the specified Account """
    ga_count = 0
    # Global Accelerator API is only available in us-west-2
    client = get_aws_client('globalaccelerator', 'us-west-2', credentials)
    try:
        response = client.list_accelerators()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return

    for accel in response.get('Accelerators', []):
        ga_count += 1
        accel_name = accel.get('Name', '')
        dns_name = accel.get('DnsName', '')
        if dns_name:
            with public_domains_lock:
                public_domains.append({
                    'resource_id': accel_name,
                    'domain': dns_name,
                    'type': 'GlobalAccelerator',
                    'region': 'global',
                    'account': account['Name']
                })
        for ip_set in accel.get('IpSets', []):
            for ip_addr in ip_set.get('IpAddresses', []):
                with vm_public_ips_lock:
                    vm_public_ips.append({
                        'instance_id': accel_name,
                        'public_ip': ip_addr,
                        'type': 'GlobalAccelerator',
                        'region': 'global',
                        'account': account['Name']
                    })

    while response.get('NextToken'):
        try:
            response = client.list_accelerators(NextToken=response['NextToken'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break
        for accel in response.get('Accelerators', []):
            ga_count += 1
            accel_name = accel.get('Name', '')
            dns_name = accel.get('DnsName', '')
            if dns_name:
                with public_domains_lock:
                    public_domains.append({
                        'resource_id': accel_name,
                        'domain': dns_name,
                        'type': 'GlobalAccelerator',
                        'region': 'global',
                        'account': account['Name']
                    })
            for ip_set in accel.get('IpSets', []):
                for ip_addr in ip_set.get('IpAddresses', []):
                    with vm_public_ips_lock:
                        vm_public_ips.append({
                            'instance_id': accel_name,
                            'public_ip': ip_addr,
                            'type': 'GlobalAccelerator',
                            'region': 'global',
                            'account': account['Name']
                        })

    if ga_count > 0 or args.verbose_mode:
        progress_print(resource_count=ga_count, resource_type='Global Accelerators', region=region, account=account['Name'])
        increment_total('Global Accelerators', ga_count)


# SageMaker Endpoints
def get_aws_sagemaker_endpoints(region, credentials, account):
    """ Get AWS SageMaker Endpoints in the specified Account and Region """
    sm_count = 0
    client = get_aws_client('sagemaker', region, credentials)
    try:
        response = client.list_endpoints(MaxResults=100)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        handle_api_error(ex, account['Id'])
        return

    endpoints = response.get('Endpoints', [])
    sm_count += len(endpoints)
    while response.get('NextToken'):
        try:
            response = client.list_endpoints(MaxResults=100, NextToken=response['NextToken'])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            handle_api_error(ex, account['Id'])
            break
        endpoints.extend(response.get('Endpoints', []))
        sm_count += len(response.get('Endpoints', []))

    # Each SageMaker endpoint creates Application Endpoints on
    # runtime.sagemaker.{region}.amazonaws.com (ports 443 and 8443).
    # Wiz counts each endpoint separately even though they share the same host.
    for ep in endpoints:
        ep_name = ep.get('EndpointName', '')
        sm_domain = f"runtime.sagemaker.{region}.amazonaws.com"
        with public_domains_lock:
            public_domains.append({
                'resource_id': ep_name,
                'domain': sm_domain,
                'type': 'SageMaker',
                'region': region,
                'account': account['Name']
            })

    if sm_count > 0 or args.verbose_mode:
        progress_print(resource_count=sm_count, resource_type='SageMaker Endpoints', region=region, account=account['Name'])
        increment_total('SageMaker Endpoints', sm_count)


####
# Main
####
# pylint: disable=too-many-branches, too-many-statements
def get_aws_resources(account, current_account_id, root_account_id):
    """ Get billable resources """
    exceptions = 0
    credentials = aws_get_credentials(account['Id'], current_account_id, root_account_id)
    verbose_print(f"credentials: obtained for account {account['Id']}")
    if not credentials:
        print(f"Failed to get credentials. Skipping {account['Id']} - {account['Name']}")
        return
    if args.input_regions:
        regions = get_aws_regions_from_file()
    else:
        regions = get_aws_regions(credentials)
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        # AWS APIs requiring a regional client.
        for region in regions:
            if enabled['Virtual Machines']:
                get_aws_ec2_instances(region=region['RegionName'], credentials=credentials, account=account)
            if enabled['Serverless Functions']:
                get_aws_lambda_functions(region=region['RegionName'], credentials=credentials, account=account)
            if enabled['Elastic IP Addresses']:
                get_aws_elastic_ips(region=region['RegionName'], credentials=credentials, account=account)
            if enabled['Load Balancers']:
                get_aws_load_balancers(region=region['RegionName'], credentials=credentials, account=account)
            if enabled['EKS Clusters']:
                get_aws_eks_clusters(region=region['RegionName'], credentials=credentials, account=account)
            if enabled['API Gateways']:
                get_aws_api_gateways(region=region['RegionName'], credentials=credentials, account=account)
            if enabled['SageMaker Endpoints']:
                get_aws_sagemaker_endpoints(region=region['RegionName'], credentials=credentials, account=account)
        # S3 APIs using a global control plane, so we use the "default" partition region.
        if enabled['Data Buckets']:
            get_aws_s3_buckets(region=select_default_region(), credentials=credentials, account=account)
        # Route53 APIs using a global control plane.
        if enabled['Route53 Hosted Zones']:
            get_aws_route53_hosted_zones(region=select_default_region(), credentials=credentials, account=account)
        if enabled['Route53 DNS Records']:
            get_aws_route53_dns_records(region=select_default_region(), credentials=credentials, account=account)
        # CloudFront APIs using a global control plane.
        if enabled['CloudFront Distributions']:
            get_aws_cloudfront_distributions(region=select_default_region(), credentials=credentials, account=account)
        # Global Accelerator API (only available in us-west-2).
        if enabled['Global Accelerators']:
            get_aws_global_accelerators(region='us-west-2', credentials=credentials, account=account)
    else:
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            # AWS APIs requiring a regional client.
            for region in regions:
                if enabled['Virtual Machines']:
                    futures.append(executor.submit(get_aws_ec2_instances, region=region['RegionName'], credentials=credentials, account=account))
                if enabled['Serverless Functions']:
                    futures.append(executor.submit(get_aws_lambda_functions, region=region['RegionName'], credentials=credentials, account=account))
                if enabled['Elastic IP Addresses']:
                    futures.append(executor.submit(get_aws_elastic_ips, region=region['RegionName'], credentials=credentials, account=account))
                if enabled['Load Balancers']:
                    futures.append(executor.submit(get_aws_load_balancers, region=region['RegionName'], credentials=credentials, account=account))
                if enabled['EKS Clusters']:
                    futures.append(executor.submit(get_aws_eks_clusters, region=region['RegionName'], credentials=credentials, account=account))
                if enabled['API Gateways']:
                    futures.append(executor.submit(get_aws_api_gateways, region=region['RegionName'], credentials=credentials, account=account))
                if enabled['SageMaker Endpoints']:
                    futures.append(executor.submit(get_aws_sagemaker_endpoints, region=region['RegionName'], credentials=credentials, account=account))
            # S3 APIs using a global control plane, so we use the "default" partition region.
            if enabled['Data Buckets']:
                futures.append(executor.submit(get_aws_s3_buckets, region=select_default_region(), credentials=credentials, account=account))
            # Route53 APIs using a global control plane.
            if enabled['Route53 Hosted Zones']:
                futures.append(executor.submit(get_aws_route53_hosted_zones, region=select_default_region(), credentials=credentials, account=account))
            if enabled['Route53 DNS Records']:
                futures.append(executor.submit(get_aws_route53_dns_records, region=select_default_region(), credentials=credentials, account=account))
            # CloudFront APIs using a global control plane.
            if enabled['CloudFront Distributions']:
                futures.append(executor.submit(get_aws_cloudfront_distributions, region=select_default_region(), credentials=credentials, account=account))
            # Global Accelerator API (only available in us-west-2).
            if enabled['Global Accelerators']:
                futures.append(executor.submit(get_aws_global_accelerators, region='us-west-2', credentials=credentials, account=account))
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                exceptions += 1


def output_results(accounts, scan_results=None):
    """ Output results """
    # Write CSV output files — wrapped in try/except so a filesystem error
    # (read-only directory, full disk, etc.) doesn't lose the console summary.
    csv_write_errors = []
    try:
        # Summary File
        with open(output_file, 'w', encoding='utf-8', newline='') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(['Resource Type', 'Resource Count'])
            for resource_type, resource_count in totals.items():
                csv_writer.writerow([resource_type, resource_count])
    except OSError as e:
        csv_write_errors.append(f"Failed to write {output_file}: {e}")

    try:
        # Log File
        with open(output_file_log, 'w', encoding='utf-8') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(['Resource Type', 'Resource Count', 'Account', 'Region'])
            for item in totals_log:
                csv_writer.writerow(item)
    except OSError as e:
        csv_write_errors.append(f"Failed to write {output_file_log}: {e}")

    # Error File
    if errors_log:
        try:
            with open(error_log_file, 'w', encoding='utf-8') as err_file:
                for error in errors_log:
                    err_file.write(error + "\n")
        except OSError as e:
            csv_write_errors.append(f"Failed to write {error_log_file}: {e}")

    try:
        # VM Public IPs File
        with open(output_file_ips, 'w', encoding='utf-8', newline='') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(['Instance ID', 'Public IP', 'Type', 'Region', 'Account'])
            for ip_info in vm_public_ips:
                csv_writer.writerow([ip_info['instance_id'], ip_info['public_ip'],
                                    ip_info['type'], ip_info['region'], ip_info['account']])
    except OSError as e:
        csv_write_errors.append(f"Failed to write {output_file_ips}: {e}")

    try:
        # Public Domains File
        with open(output_file_domains, 'w', encoding='utf-8', newline='') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(['Resource ID', 'Domain', 'Type', 'Region', 'Account'])
            for domain_info in public_domains:
                csv_writer.writerow([domain_info['resource_id'], domain_info['domain'],
                                    domain_info['type'], domain_info['region'], domain_info['account']])
    except OSError as e:
        csv_write_errors.append(f"Failed to write {output_file_domains}: {e}")

    # Summary
    print(f"\nResults across {len(accounts)} AWS Accounts (script version: {version})\n")

    if enabled['Virtual Machines']:
        print(f"{str(totals['Virtual Machines']).rjust(padding)} Virtual Machines [EC2]")
    if enabled['Serverless Functions']:
        print(f"{str(totals['Serverless Functions']).rjust(padding)} Serverless Functions [Lambda]")
    if enabled['Elastic IP Addresses']:
        print(f"{str(totals['Elastic IP Addresses']).rjust(padding)} Elastic IP Addresses [EIP]")
    if enabled['Load Balancers']:
        print(f"{str(totals['Load Balancers']).rjust(padding)} Load Balancers [ALB/NLB/CLB]")
    if enabled['CloudFront Distributions']:
        print(f"{str(totals['CloudFront Distributions']).rjust(padding)} CloudFront Distributions")
    if enabled['EKS Clusters']:
        print(f"{str(totals['EKS Clusters']).rjust(padding)} EKS Clusters (public endpoint)")
    if enabled['Global Accelerators']:
        print(f"{str(totals['Global Accelerators']).rjust(padding)} Global Accelerators")
    if enabled['Route53 Hosted Zones']:
        print(f"{str(totals['Route53 Hosted Zones']).rjust(padding)} Route53 Hosted Zones")
    if enabled['Route53 DNS Records']:
        print(f"{str(totals['Route53 DNS Records']).rjust(padding)} Route53 DNS Records")
    if enabled['API Gateways']:
        print(f"{str(totals['API Gateways']).rjust(padding)} API Gateways [REST & HTTP]")
    if enabled['SageMaker Endpoints']:
        print(f"{str(totals['SageMaker Endpoints']).rjust(padding)} SageMaker Endpoints")

    if enabled['Data Buckets']:
        print()
        print(f"{str(totals['Data Buckets']).rjust(padding)} Data Buckets (Public and Private) [S3]")

    if not args.data_mode:
        print()
        print("Data Security resources (Buckets) were skipped. Remove '--no-data' to include them.")

    # Print public IP summary to console
    if vm_public_ips:
        ip_type_counts = {}
        for ip_info in vm_public_ips:
            ip_type_counts[ip_info['type']] = ip_type_counts.get(ip_info['type'], 0) + 1
        type_breakdown = ', '.join(f"{count} {t}" for t, count in sorted(ip_type_counts.items()))
        print(f"\nVM Public IP Addresses: {len(vm_public_ips)} found ({type_breakdown})")

    # Print public domain summary to console
    if public_domains:
        domain_type_counts = {}
        for domain_info in public_domains:
            domain_type_counts[domain_info['type']] = domain_type_counts.get(domain_info['type'], 0) + 1
        type_breakdown = ', '.join(f"{count} {t}" for t, count in sorted(domain_type_counts.items()))
        print(f"Public Domains: {len(public_domains)} found ({type_breakdown})")

    # Summary paragraphs
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)

    # Paragraph 1: Resource counts and internet exposure
    total_resources = sum(totals.values())
    total_public_ips = len(vm_public_ips)
    total_public_domains = len(public_domains)

    # Count by type
    ec2_count = sum(1 for ip in vm_public_ips if ip['type'] == 'EC2')
    eip_count = sum(1 for ip in vm_public_ips if ip['type'] == 'EIP')
    ga_ip_count = sum(1 for ip in vm_public_ips if ip['type'] == 'GlobalAccelerator')
    lambda_domains = sum(1 for d in public_domains if d['type'] == 'Lambda')
    s3_domains = sum(1 for d in public_domains if d['type'] == 'S3')
    route53_domains = sum(1 for d in public_domains if d['type'] == 'Route53')
    apigw_rest_domains = sum(1 for d in public_domains if d['type'] == 'API Gateway (REST)')
    apigw_http_domains = sum(1 for d in public_domains if d['type'] == 'API Gateway (HTTP)')
    alb_domains = sum(1 for d in public_domains if d['type'] == 'ALB')
    nlb_domains = sum(1 for d in public_domains if d['type'] == 'NLB')
    clb_domains = sum(1 for d in public_domains if d['type'] == 'CLB')
    cf_domains = sum(1 for d in public_domains if d['type'] == 'CloudFront')
    eks_domains = sum(1 for d in public_domains if d['type'] == 'EKS')
    ga_domains = sum(1 for d in public_domains if d['type'] == 'GlobalAccelerator')
    sagemaker_domains = sum(1 for d in public_domains if d['type'] == 'SageMaker')
    ec2_dns_domains = sum(1 for d in public_domains if d['type'] == 'EC2-DNS')

    # Application Endpoint estimation:
    # Wiz creates Application Endpoints for internet-exposed resources with open ports.
    # The following are NOT Application Endpoints: S3 buckets, Route53 DNS records,
    # EKS API server endpoints.
    # Every associated EIP is a public IP that can receive inbound traffic, so each
    # counts as at least one Application Endpoint. IPs are deduplicated at the address
    # level so an EIP on an EC2 instance is not double-counted.

    # EC2 IPs with public addresses
    ec2_all_ips = {ip['public_ip'] for ip in vm_public_ips if ip['type'] == 'EC2'}
    ec2_accessible_ips = {ip['public_ip'] for ip in vm_public_ips
                          if ip['type'] == 'EC2' and ip.get('internet_accessible', True)}
    ec2_blocked_count = len(ec2_all_ips) - len(ec2_accessible_ips)

    # EIP analysis: every associated EIP is a public IP and potential Application Endpoint.
    # Unattached EIPs have no resource behind them and are not counted.
    eip_total = sum(1 for ip in vm_public_ips if ip['type'] == 'EIP')
    eip_associated_ips = {ip['public_ip'] for ip in vm_public_ips
                          if ip['type'] == 'EIP' and ip.get('associated', False)}
    eip_unattached = eip_total - len(eip_associated_ips)

    ga_ip_set = {ip['public_ip'] for ip in vm_public_ips if ip['type'] == 'GlobalAccelerator'}
    unique_ae_ips = ec2_accessible_ips | eip_associated_ips | ga_ip_set

    # ASM-relevant domains: services that create Application Endpoints
    asm_types = {'ALB', 'NLB', 'CLB', 'CloudFront', 'Lambda',
                 'API Gateway (REST)', 'API Gateway (HTTP)', 'GlobalAccelerator',
                 'EKS', 'S3', 'SageMaker', 'EC2-DNS'}
    asm_domain_count = sum(1 for d in public_domains if d['type'] in asm_types)

    # Non-ASM resources (tracked but not Application Endpoints)
    non_asm_types = {'Route53'}
    non_asm_domain_count = sum(1 for d in public_domains if d['type'] in non_asm_types)

    # Cloud-based AE estimate (always computed): 1 AE per unique IP + 1 AE per ASM domain
    cloud_ae = len(unique_ae_ips) + asm_domain_count

    if scan_results:
        # Port scan mode: count AE per IP:Port pair for scanned IPs
        port_scan_ip_ae = sum(len(ports) for ports in scan_results.values())

        # EC2-DNS AE: for each EC2-DNS domain, find the corresponding IP
        # and count the same number of open ports
        ec2_dns_port_ae = 0
        for d in public_domains:
            if d['type'] == 'EC2-DNS':
                # Extract IP from domain: ec2-1-2-3-4.region.compute.amazonaws.com
                parts = d['domain'].split('.')[0]  # "ec2-1-2-3-4"
                ip = parts.replace('ec2-', '').replace('-', '.')
                ec2_dns_port_ae += len(scan_results.get(ip, set()))

        # Other ASM domains (ALB, CloudFront, EKS, Lambda, API GW, SageMaker, S3, GA):
        # Still count as 1 AE per domain (ports are known/not scanned)
        other_asm_types = asm_types - {'EC2-DNS'}
        other_asm_domain_count = sum(1 for d in public_domains if d['type'] in other_asm_types)

        scan_ae = port_scan_ip_ae + ec2_dns_port_ae + other_asm_domain_count
        scan_delta = scan_ae - cloud_ae
        ae_estimate = scan_ae
    else:
        ae_estimate = cloud_ae

    print(f"""
        Resource Discovery Summary:
        Found {totals['Virtual Machines']} Virtual Machines, {totals['Serverless Functions']} Lambda Functions,
        {totals['Elastic IP Addresses']} Elastic IPs, {totals['Load Balancers']} Load Balancers,
        {totals['CloudFront Distributions']} CloudFront Distributions, {totals['EKS Clusters']} EKS Clusters,
        {totals['Global Accelerators']} Global Accelerators, {totals['SageMaker Endpoints']} SageMaker Endpoints,
        {totals['Route53 Hosted Zones']} Route53 Hosted Zones, {totals['Route53 DNS Records']} DNS Records,
        {totals['API Gateways']} API Gateways, and {totals['Data Buckets']} S3 Buckets.

        Internet Exposure (Application Endpoint sources):
        - {len(ec2_accessible_ips)} EC2 instances with internet-accessible public IPs{f' ({ec2_blocked_count} blocked by security groups)' if ec2_blocked_count else ''}
        - {ec2_dns_domains} EC2 reverse DNS names (one per public IP)
        - {len(eip_associated_ips)} Elastic IPs (associated){f', {eip_unattached} unattached' if eip_unattached else ''} (deduplicated with EC2 IPs above)
        - {alb_domains} ALB DNS names
        - {nlb_domains} NLB DNS names
        - {clb_domains} CLB DNS names
        - {cf_domains} CloudFront domains
        - {eks_domains} EKS API endpoints (public)
        - {ga_domains} Global Accelerator DNS names
        - {ga_ip_count} Global Accelerator static IPs
        - {lambda_domains} Lambda functions with public URLs
        - {apigw_rest_domains} REST API Gateways
        - {apigw_http_domains} HTTP API Gateways
        - {sagemaker_domains} SageMaker Endpoints
        - {s3_domains} S3 bucket domains

        Not counted as Application Endpoints:
        - {route53_domains} Route53 DNS records (discovery only)

        Cloud-Based AE Estimate: {cloud_ae}
        (1 AE per unique public IP + 1 AE per ASM domain)""")

    if scan_results:
        scanned_ip_count = len(scan_results)
        total_open_ports = sum(len(ports) for ports in scan_results.values())
        sign = "+" if scan_delta >= 0 else ""
        print(f"""
        Port Scan Results (masscan):
        - {total_open_ports} open ports found across {scanned_ip_count} IPs
        - {port_scan_ip_ae} IP:port AEs (replaces {len(unique_ae_ips)} cloud-based IP AEs)
        - {ec2_dns_port_ae} EC2 DNS:port AEs (replaces {ec2_dns_domains} cloud-based DNS AEs)
        - {other_asm_domain_count} other ASM domain AEs (unchanged)
        Port-Scan-Adjusted AE Estimate: {ae_estimate} ({sign}{scan_delta} from cloud estimate)""")

    print(f"""
        {"=" * 50}
        ESTIMATED APPLICATION ENDPOINTS: {ae_estimate}
        {"=" * 50}
        Note: Actual count may vary. Wiz counts AE per
        IP:Port and DNS:Port pair for each open port.{"" if not scan_results else chr(10) + "        Port scan: enabled (masscan)"}""")

    print("="*70)

    if csv_write_errors:
        print(f"\nWARNING: {len(csv_write_errors)} file(s) could not be written:")
        for err_msg in csv_write_errors:
            print(f"  - {err_msg}")
    else:
        print(f"\nDetails written to {output_file}, {output_file_log}, {output_file_ips}, and {output_file_domains}")

    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


def main():
    """ Calculon Compute! """
    root_account_id = None

    print("Getting the current AWS Account")
    current_account = get_aws_account()
    if not current_account:
        print("ERROR: Unable to get current AWS account. Check AWS credentials and configuration.")
        sys.exit(1)
    current_account_id = current_account['Account']
    if current_account['Arn'].endswith('root'):
        root_account_id = current_account['Account']
    if current_account_id == root_account_id:
        print(f"\nFound Management Account:\n-- {current_account_id} {current_account['Arn']}")
    else:
        print(f"\nFound Account:\n- {current_account_id}")

    # Validate permissions before scanning
    print("\nValidating AWS permissions ...")
    validation_credentials = aws_get_credentials(current_account_id, current_account_id, root_account_id)
    if validation_credentials:
        missing_permissions = validate_permissions(validation_credentials)
        if missing_permissions:
            print("\n" + "="*70)
            print("PERMISSION WARNINGS")
            print("="*70)
            print("The following permissions are missing. Affected resources will be skipped:\n")
            for permission, resources in missing_permissions:
                print(f"  Missing: {permission}")
                print(f"  Skipped: {', '.join(resources)}\n")
            print("="*70)
        else:
            print("All permissions validated successfully.")
    else:
        print("WARNING: Unable to obtain credentials for permission validation. Proceeding with best effort.")

    if args.port_scan:
        check_masscan()

    if args.all:
        if current_account_id == root_account_id:
            print(f"ERROR: The current AWS Account ({current_account['Arn']}) is a root account.")
            print("Roles may not be assumed by root accounts, and AssumeRole is required to scan Organization Member Accounts.")
            print("Exiting...")
            sys.exit(1)
        print("\nGetting AWS Accounts in the current AWS Organization")
        org_root_account_id, accounts = get_aws_organization()
        if org_root_account_id:
            print(f"\nFound Management Account:\n-- {org_root_account_id}")
        print(f"\nFound {len(accounts)} Accounts:")
        for account in accounts:
            print(f"-- {account['Id']} - {account['Name']}")
        print('')

    elif args.input_accounts:
        if current_account_id == root_account_id:
            print(f"ERROR: The current AWS Account ({current_account['Arn']}) is a root account.")
            print("Roles may not be assumed by root accounts, and AssumeRole is required to scan other Accounts.")
            print("Exiting...")
            sys.exit(1)
        print(f"\nGetting AWS Accounts from file: {accounts_file}")
        accounts = get_aws_accounts_from_file()
        print(f"\nFound {len(accounts)} Accounts:")

    else:
        if args.id:
            if current_account_id == root_account_id and args.id != current_account_id:
                print(f"ERROR: The current AWS Account ({current_account['Arn']}) is a root account.")
                print("Roles may not be assumed by root accounts, and AssumeRole is required to scan other Accounts.")
                print("Exiting...")
                sys.exit(1)
            print(f"\nGetting AWS Account: {args.id}")
            accounts = [{'Id': args.id, 'Name': args.id}]
            if root_account_id == args.id:
                print(f"\nFound Management Account:\n-- {args.id}")
        else:
            accounts = [{'Id': current_account_id, 'Name': current_account_id}]

    print("\nGetting Billable Resources for each AWS Account ...")
    for account in accounts:
        print(f"\nScanning {account['Id']} - {account['Name']}")
        get_aws_resources(account, current_account_id, root_account_id)

    scan_results = {}
    if args.port_scan:
        # Build set of IPs on EC2 instances blocked by security groups,
        # so we can exclude associated EIPs pointing to those same instances
        blocked_ec2_ips = set()
        for ip in vm_public_ips:
            if ip['type'] == 'EC2' and not ip.get('internet_accessible', True):
                blocked_ec2_ips.add(ip['public_ip'])

        # Collect all IPs eligible for scanning
        scan_ips = set()
        for ip in vm_public_ips:
            if ip['type'] == 'EC2' and ip.get('internet_accessible', True):
                scan_ips.add(ip['public_ip'])
            elif ip['type'] == 'EIP' and ip.get('associated', False):
                # Skip EIPs whose IP matches a security-group-blocked EC2 instance
                if ip['public_ip'] not in blocked_ec2_ips:
                    scan_ips.add(ip['public_ip'])
            elif ip['type'] == 'GlobalAccelerator':
                scan_ips.add(ip['public_ip'])
        if scan_ips:
            print(f"\nPort scanning {len(scan_ips)} public IPs with masscan ...")
            scan_results = run_port_scan(scan_ips)
            total_open = sum(len(ports) for ports in scan_results.values())
            print(f"Found {total_open} open ports across {len(scan_results)} IPs")
        else:
            print("\nNo public IPs eligible for port scanning.")

    output_results(accounts, scan_results)


####

if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
