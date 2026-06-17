#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines

""" Wiz : AWS Log Volume Estimator for Defend """

import argparse
import concurrent.futures
import csv
import inspect
import os
import signal
import sys
import gzip
import json
import random
import threading
from datetime import datetime, timedelta, timezone
import io
from collections import defaultdict

# As a single script download, we do not publish a requirements.txt.

try:
    import boto3
    from botocore.config import Config
    import botocore
except ImportError:
    print("\nERROR: Missing required AWS SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade boto3 botocore")
    sys.exit(1)

version='1.2.2'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Estimate AWS Log Volume for Wiz Defend')

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
    help = 'Exit upon first error (default: disabled)',
    default = False
)
parser.add_argument(
    '--verbose',
    action = 'store_true',
    dest = 'verbose_mode',
    help = 'Output verbose debugging information (default: disabled)',
    default = False
)

defend_group = parser.add_argument_group('Log Volume Estimation Arguments')

defend_group.add_argument(
    '--defend-detailed',
    action='store_true',
    dest='defend_detailed',
    help='Enable detailed AWS CloudTrail log analysis by sampling and processing log files. If false, only total bucket size is estimated for CloudTrail. (default: False)',
    default=False
)
defend_group.add_argument(
    '--defend-cloudtrail-logs-bucket',
    dest='defend_cloudtrail_logs_bucket',
    help='S3 bucket containing AWS CloudTrail logs',
    default=None
)
defend_group.add_argument(
    '--defend-cloudtrail-logs-bucket-prefix',
    dest='defend_cloudtrail_logs_bucket_prefix',
    default='AWSLogs/',
    help='Prefix path in the CloudTrail logs bucket (default: AWSLogs/)'
)
defend_group.add_argument(
    '--defend-cloudtrail-logs-bucket-days',
    dest='defend_cloudtrail_logs_bucket_days',
    type=int,
    default=30,
    help='Number of days of CloudTrail logs to analyze (default: 30)'
)
defend_group.add_argument(
    '--defend-cloudtrail-logs-bucket-sample-size',
    dest='defend_cloudtrail_logs_bucket_sample_size',
    type=int,
    default=200,
    help='Number of CloudTrail log files to sample (default: 200)'
)
defend_group.add_argument(
    '--defend-cloudtrail-logs-compression-factor',
    dest='defend_cloudtrail_logs_compression_factor',
    type=float,
    default=10.0,
    help='Assumed compression factor for CloudTrail logs when not doing detailed analysis (uncompressed_size / compressed_size) (default: 10.0)'
)
defend_group.add_argument(
    '--defend-vpc-flow-logs-bucket',
    dest='defend_vpc_flow_logs_bucket',
    help='S3 bucket containing VPC Flow Logs. Only basic estimation is supported.',
    default=None
)
defend_group.add_argument(
    '--defend-vpc-flow-logs-compression-factor',
    dest='defend_vpc_flow_logs_compression_factor',
    type=float,
    default=10.0,
    help='Assumed compression factor for VPC Flow Logs when doing basic analysis (default: 10.0)'
)
defend_group.add_argument(
    '--defend-route53-resolver-logs-bucket',
    dest='defend_route53_resolver_logs_bucket',
    help='S3 bucket containing Route 53 Resolver Query Logs. Only basic estimation is supported.',
    default=None
)
defend_group.add_argument(
    '--defend-route53-resolver-logs-compression-factor',
    dest='defend_route53_resolver_logs_compression_factor',
    type=float,
    default=10.0,
    help='Assumed compression factor for Route 53 Resolver Query Logs when doing basic analysis (default: 10.0)'
)

args = parser.parse_args()

if args.max_workers < 1 or args.max_workers > 255:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 255]")
    sys.exit(1)

####
# Configuration and Globals
####

debug_mode_error_occurred = threading.Event()

output_file     = 'aws-defend-log-volume.csv'
error_log_file  = 'aws-defend-errors-log.txt'
padding_desc = 45

totals = {
    'CloudTrail Logs Bucket/Prefix': None,
    'CloudTrail Logs Analysis Mode': None,
    'CloudTrail Logs - Management Logs Ingestion (Basic) GB': 0.0,
    'CloudTrail Logs - Management Logs Ingestion (Write) GB': 0.0,
    'CloudTrail Logs - Management Logs Ingestion (ReadOnly) GB': 0.0,
    'CloudTrail Logs - Storage Logs Ingestion (S3) GB': 0.0,
    'CloudTrail Logs - Other Operations GB': 0.0,
    'CloudTrail Logs Categories Available': False,

    'VPC Flow Logs Bucket/Prefix': None,
    'VPC Flow Logs Analysis Mode': 'Basic',
    'VPC Flow Logs - Network Logs Ingestion (Basic) GB': 0.0,

    'Route 53 Resolver Query Logs Bucket/Prefix': None,
    'Route 53 Resolver Query Logs Analysis Mode': 'Basic',
    'Route 53 Resolver Query Logs - Network Logs Ingestion (Basic) GB': 0.0
}

errors_log = []

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


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details, account_context=''):
    """ Error output """
    context_str  = f"Context: {account_context} " if account_context else ""
    function = ''
    try:
        function = f"{inspect.stack()[1].function}()"
    except IndexError:
        function = 'UnknownFunction'
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except (TypeError, ValueError):
        pass
    print(f"\nERROR: {context_str} {function} {details}\n")
    errors_log.append(f"ERROR: {context_str} {function} {details}")

def debug_exit(msg):
    """ Exit in debug mode """
    if args.debug_mode:
        print(f"\nDEBUG MODE: {msg}")
        debug_mode_error_occurred.set()


def get_bucket_region(bucket_name):
    """Get the region of an S3 bucket"""
    try:
        s3 = boto3.client('s3', config=aws_api_config)
        response = s3.head_bucket(Bucket=bucket_name)
        bucket_region = response.get('ResponseMetadata', {}).get('HTTPHeaders', {}).get('x-amz-bucket-region')
        if bucket_region:
            return bucket_region

        location_response = s3.get_bucket_location(Bucket=bucket_name)
        location = location_response.get('LocationConstraint')
        return location if location else 'us-east-1'
    except Exception as e: # pylint: disable=broad-except
        error_print(f"Could not determine region for bucket {bucket_name}: {e}")
        debug_exit(f"Could not determine region for bucket {bucket_name}: {e}")
        return 'us-east-1'

# Categorize Event Helper Function


def aws_categorize_event(event):
    """ Categorize a CloudTrail event based on its eventCategory and readOnly fields """
    event_category = event.get('eventCategory', '')
    read_only = event.get('readOnly', None)

    if event_category == 'Management':
        if read_only is False:
            return 'CloudTrail - Management (Write)'
        if read_only is True:
            return 'CloudTrail - Management (ReadOnly)'
    elif event_category == 'Data':
        return 'CloudTrail - Data (S3)'

    return "CloudTrail - Other Operations"

# Process AWS CloudTrail Events Sample (Only if --defend-detailed)


def aws_process_events_sample(bucket, obj_key, obj_size):
    """
    Process a single CloudTrail log file.
    Returns a dictionary with stats or None on failure.
    """
    if debug_mode_error_occurred.is_set():
        return None

    if 'digest' in obj_key.lower():
        verbose_print(f"Skipping digest file that slipped through: {obj_key}")
        return None

    result = {
        'compressed_size': obj_size,
        'uncompressed_size': 0,
        'events': 0,
        'event_size': 0,
        'event_types': defaultdict(lambda: {'count': 0, 'size': 0}),
        'categories': defaultdict(lambda: {'count': 0, 'size': 0})
    }

    try:
        s3 = boto3.client('s3', config=aws_api_config)
        response = s3.get_object(Bucket=bucket, Key=obj_key)
        content = response['Body'].read()

        return process_gzipped_content(content, obj_key, result)

    except botocore.exceptions.ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_msg = e.response.get('Error', {}).get('Message', str(e))

        if error_code == 'AccessDenied':
            error_print(f"Access denied reading S3 object {obj_key}", account_context='CloudTrail Sample Processing')
            error_print(f"Permission required: s3:GetObject on bucket {bucket}", account_context='CloudTrail Sample Processing')
            debug_exit(f"Access denied: {obj_key}")
        else:
            error_print(f"AWS API error processing S3 object {obj_key}: {error_code} - {error_msg}",
                        account_context='CloudTrail Sample Processing')
            debug_exit(f"AWS API error: {error_code} - {error_msg}")
        return None
    except (boto3.exceptions.Boto3Error) as e:
        error_print(f"AWS error processing S3 object {obj_key}: {e}",
                    account_context='CloudTrail Sample Processing')
        debug_exit(f"AWS error: {e}")
        return None
    except IOError as e:
        error_print(f"IO error processing S3 object {obj_key}: {e}",
                    account_context='CloudTrail Sample Processing')
        debug_exit(f"IO error: {e}")
        return None

def process_gzipped_content(content, obj_key, result):
    """ Helper function to process gzipped CloudTrail content """
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as f:
            log_data_str = f.read().decode('utf-8')
            log_data = json.loads(log_data_str)
            result['uncompressed_size'] = len(log_data_str.encode('utf-8'))

            if 'Records' in log_data:
                process_cloudtrail_records(log_data['Records'], result)
            else:
                verbose_print(f"No 'Records' key found in {obj_key}")

        return result

    except json.JSONDecodeError as json_e:
        error_print(f"JSON decode error in {obj_key}: {json_e}",
                    account_context='CloudTrail Sample Processing')
        debug_exit(f"JSON decode error: {json_e}")
        return None
    except (gzip.BadGzipFile, OSError) as gz_e:
        error_print(f"Decompression error in {obj_key}: {gz_e}",
                    account_context='CloudTrail Sample Processing')
        debug_exit(f"Decompression error: {gz_e}")
        return None
    except UnicodeDecodeError as decode_e:
        error_print(f"Unicode decode error in {obj_key}: {decode_e}",
                    account_context='CloudTrail Sample Processing')
        debug_exit(f"Unicode decode error: {decode_e}")
        return None

def process_cloudtrail_records(events, result):
    """ Process CloudTrail event records and update the result dictionary """
    result['events'] = len(events)

    for event in events:
        event_type = event.get('eventName', 'Unknown')
        try:
            event_str = json.dumps(event)
            event_size = len(event_str.encode('utf-8'))
        except (TypeError, ValueError):
            event_size = 500

        category = aws_categorize_event(event)

        result['event_types'][event_type]['count'] += 1
        result['event_types'][event_type]['size'] += event_size

        result['categories'][category]['count'] += 1
        result['categories'][category]['size'] += event_size

        result['event_size'] += event_size

# Estimate Log Volume Basic (CloudTrail, VPC Flow Logs, Route 53 Resolver Logs)


def estimate_bucket_volume_basic(bucket, bucket_type):
    """ Basic log volume estimation using S3 CloudWatch metrics. """
    region_for_cw = get_bucket_region(bucket)

    try:
        cloudwatch = boto3.client('cloudwatch', config=aws_api_config, region_name=region_for_cw)
    except boto3.exceptions.Boto3Error as e_cw_client:
        error_print(f"Failed to create CloudWatch client for basic estimation: {e_cw_client}",
                    account_context='Log Volume Basic Estimation')
        debug_exit(f"Failed to create CloudWatch client: {e_cw_client}")
        return

    end_time_cw = datetime.now(timezone.utc)
    start_time = end_time_cw - timedelta(days=30)
    metric_period_seconds = int(30 * 24 * 60 * 60)

    local_metric_value_bytes = 0

    incoming_bytes_data = get_incoming_bytes_metric(
        cloudwatch,bucket, start_time, end_time_cw, metric_period_seconds
    )

    if incoming_bytes_data and incoming_bytes_data.get('metric_data_found', False):
        local_metric_value_bytes = incoming_bytes_data.get('metric_value_bytes', 0)
    else:
        bucket_size_data = get_bucket_size_bytes_metric(
            cloudwatch, bucket, start_time, end_time_cw, 30
        )
        if bucket_size_data and bucket_size_data.get('metric_data_found', False):
            local_metric_value_bytes = bucket_size_data.get('metric_value_bytes', 0)

    if bucket_type == 'defend_cloudtrail_logs_bucket':
        totals['CloudTrail Logs Analysis Mode'] = 'Basic'
        totals['CloudTrail Logs Bucket/Prefix'] = args.defend_cloudtrail_logs_bucket
        totals['CloudTrail Logs - Management Logs Ingestion (Basic) GB'] = local_metric_value_bytes * args.defend_cloudtrail_logs_compression_factor / (1024**3)
    if bucket_type == 'defend_vpc_flow_logs_bucket':
        totals['VPC Flow Logs Analysis Mode'] = 'Basic'
        totals['VPC Flow Logs Bucket/Prefix'] = args.defend_vpc_flow_logs_bucket
        totals['VPC Flow Logs - Network Logs Ingestion (Basic) GB'] = local_metric_value_bytes * args.defend_vpc_flow_logs_compression_factor / (1024**3)
    if bucket_type == 'defend_route53_resolver_logs_bucket':
        totals['Route 53 Resolver Query Logs Analysis Mode'] = 'Basic'
        totals['Route 53 Resolver Query Logs Bucket/Prefix'] = args.defend_route53_resolver_logs_bucket
        totals['Route 53 Resolver Query Logs - Network Logs Ingestion (Basic) GB'] = local_metric_value_bytes * args.defend_route53_resolver_logs_compression_factor / (1024**3)

    return


def get_incoming_bytes_metric(cloudwatch, bucket_name, start_time, end_time, period_seconds):
    """Get IncomingBytes metric from CloudWatch"""
    result = {
        'metric_name_used': 'IncomingBytes',
        'metric_value_bytes': 0,
        'metric_data_found': False,
        'metric_value_interpretation': f"Sum of compressed data ingested over the last {period_seconds / (24 * 60 * 60)} days."
    }
    dimensions = [{'Name': 'BucketName', 'Value': bucket_name}, {'Name': 'FilterId', 'Value': 'EntireBucket'}]
    try:
        verbose_print(f"Querying CloudWatch for 'IncomingBytes' with dimensions: {dimensions}, Start: {start_time}, End: {end_time}, Period: {period_seconds}")
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/S3', MetricName='IncomingBytes', Dimensions=dimensions,
            StartTime=start_time, EndTime=end_time, Period=period_seconds,
            Statistics=['Sum'], Unit='Bytes'
        )
        verbose_print(f"CloudWatch get_metric_statistics response for IncomingBytes: {response}")
        if response and 'Datapoints' in response and response['Datapoints']:
            for dp in response['Datapoints']:
                if 'Sum' in dp:
                    result['metric_value_bytes'] = dp['Sum']
                    result['metric_data_found'] = True
                    break
            if not result['metric_data_found'] and response['Datapoints']:
                error_print(f"CloudWatch returned datapoints for IncomingBytes for s3://{bucket_name}, but no 'Sum' statistic was found.", account_context='Log Volume Basic Estimation')
        else:
            verbose_print(f"No 'IncomingBytes' datapoints found for s3://{bucket_name} ... Request metrics might not be enabled.")
    except boto3.exceptions.Boto3Error as e_incoming:
        error_print(f"Failed to get 'IncomingBytes' metric for s3://{bucket_name}: {e_incoming}", account_context='Log Volume Basic Estimation')
        error_print("This could be due to permissions, or 'Request metrics' not being enabled/available.", account_context='Log Volume Basic Estimation')
        debug_exit(f"Failed to get IncomingBytes metric: {e_incoming}")
    return result


def get_bucket_size_bytes_metric(cloudwatch, bucket_name, start_time, end_time, days_for_diff):
    """Get BucketSizeBytes metric and calculate growth over time as a fallback"""
    verbose_print("\nAttempting fallback to 'BucketSizeBytes' metric...")
    result = {'metric_name_used': 'BucketSizeBytes', 'metric_value_bytes': 0, 'metric_data_found': False, 'days_for_incoming_bytes_sum': "N/A (latest total)"}
    dimensions = [{'Name': 'BucketName', 'Value': bucket_name}, {'Name': 'StorageType', 'Value': 'StandardStorage'}]
    try:
        current_size_data = get_bucket_size_point(cloudwatch, dimensions, start_time, end_time)
        if not current_size_data:
            error_print(f"No current 'BucketSizeBytes' (StandardStorage) datapoints found for s3://{bucket_name}.", account_context='Log Volume Basic Estimation')
            result['metric_value_interpretation'] = f"Could not retrieve any 'BucketSizeBytes' data for s3://{bucket_name}."
            return result
        result['metric_data_found'] = True
        target_date_days_ago = end_time - timedelta(days=days_for_diff)
        historical_start = target_date_days_ago - timedelta(days=1)
        historical_end = target_date_days_ago + timedelta(days=1)
        historical_size_data = get_bucket_size_point(cloudwatch, dimensions, historical_start, historical_end, target_date=target_date_days_ago)
        if historical_size_data:
            result['metric_value_bytes'] = current_size_data['size'] - historical_size_data['size']
            result['metric_value_interpretation'] = f"Difference in 'BucketSizeBytes' (StandardStorage) over approx. {days_for_diff} days."
            result['days_for_incoming_bytes_sum'] = f"approx {days_for_diff} for difference"
            result['calculated_difference'] = True
        else:
            result['metric_value_bytes'] = current_size_data['size']
            result['metric_value_interpretation'] = f"Latest total compressed size of objects in 'StandardStorage' as of {current_size_data['timestamp']}. Unable to fetch historical data for difference."
            result['calculated_difference'] = False
    except boto3.exceptions.Boto3Error as e_bucket_size:
        error_print(f"Failed to get 'BucketSizeBytes' metric for s3://{bucket_name}: {e_bucket_size}", account_context='Log Volume Basic Estimation')
        result['metric_value_interpretation'] = "Error during 'BucketSizeBytes' processing."
        debug_exit(f"Failed to get BucketSizeBytes metric: {e_bucket_size}")
    return result


def get_bucket_size_point(cloudwatch, dimensions, start_time, end_time, target_date=None):
    """Helper function to get a specific bucket size datapoint"""
    verbose_print(f"Querying CloudWatch for 'BucketSizeBytes': Start: {start_time}, End: {end_time}")
    try:
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/S3', MetricName='BucketSizeBytes', Dimensions=dimensions,
            StartTime=start_time, EndTime=end_time, Period=24 * 60 * 60,
            Statistics=['Average'], Unit='Bytes'
        )
        verbose_print(f"CloudWatch response for BucketSizeBytes: {response}")
        if not (response and 'Datapoints' in response and response['Datapoints']):
            return None
        valid_datapoints = [dp for dp in response['Datapoints'] if 'Average' in dp]
        if not valid_datapoints:
            return None
        if target_date:
            selected_datapoint = min(valid_datapoints, key=lambda dp: abs(dp['Timestamp'] - target_date))
        else:
            selected_datapoint = sorted(valid_datapoints, key=lambda x: x['Timestamp'], reverse=True)[0]
        return {'size': selected_datapoint['Average'], 'timestamp': selected_datapoint['Timestamp']}
    except boto3.exceptions.Boto3Error as e:
        error_print(f"Error getting bucket size datapoint: {e}", account_context='Log Volume Basic Estimation')
        debug_exit(f"Error getting bucket size datapoint: {e}")
        return None

# Estimate AWS CloudTrail Volume Detailed


def estimate_cloudtrail_volume_detailed():
    """ Estimate CloudTrail log volume with sampling approach. """
    try:
        s3 = boto3.client('s3', config=aws_api_config)
    except boto3.exceptions.Boto3Error as e:
        error_print(f"Failed to create S3 client for CloudTrail: {e}", account_context='CloudTrail Estimation')
        debug_exit(f"Failed to create S3 client: {e}")
        return

    base_paths_for_daily_scan = discover_cloudtrail_base_paths(s3, args.defend_cloudtrail_logs_bucket, args.defend_cloudtrail_logs_bucket_prefix)
    all_daily_s3_prefixes_to_scan = generate_daily_prefixes(base_paths_for_daily_scan, args.defend_cloudtrail_logs_bucket_days)

    sampled_objects, total_objects_found, total_compressed_size_in_period = collect_objects(
        s3, args.defend_cloudtrail_logs_bucket, all_daily_s3_prefixes_to_scan,
        datetime.now(timezone.utc) - timedelta(days=args.defend_cloudtrail_logs_bucket_days)
    )

    sample_results = analyze_sample_objects(args.defend_cloudtrail_logs_bucket, sampled_objects)

    totals['CloudTrail Logs Bucket/Prefix'] = f"{args.defend_cloudtrail_logs_bucket}/{args.defend_cloudtrail_logs_bucket_prefix}"
    totals['CloudTrail Logs Analysis Mode'] = 'Detailed'

    processed_category_stats = {}
    if sample_results['successful_samples'] > 0 and sample_results['aggregated_event_size'] > 0:
        avg_compression_ratio_from_sample = sample_results['sampled_uncompressed_size'] / sample_results['sampled_compressed_size'] if sample_results['sampled_compressed_size'] > 0 else 1.0
        estimated_total_uncompressed_data_in_period = total_compressed_size_in_period * avg_compression_ratio_from_sample

        for category, stats in sample_results['aggregated_categories'].items():
            proportion_of_category_size = stats['size'] / sample_results['aggregated_event_size']
            category_uncompressed_size_in_period = estimated_total_uncompressed_data_in_period * proportion_of_category_size
            normalized_size_for_30_days = 0.0
            if args.defend_cloudtrail_logs_bucket_days > 0:
                average_daily_size = category_uncompressed_size_in_period / args.defend_cloudtrail_logs_bucket_days
                normalized_size_for_30_days = average_daily_size * 30.0
            elif total_objects_found == 0 and args.defend_cloudtrail_logs_bucket_days == 0 :
                normalized_size_for_30_days = 0.0
            else:
                normalized_size_for_30_days = category_uncompressed_size_in_period
            processed_category_stats[category] = {'size': normalized_size_for_30_days, 'proportion': proportion_of_category_size}

    if processed_category_stats:
        totals['CloudTrail Logs Categories Available'] = True
        for category, stats_data in processed_category_stats.items():
            size_gb = stats_data.get('size', 0) / (1024**3)
            if category == 'CloudTrail - Management (Write)':
                totals['CloudTrail Logs - Management Logs Ingestion (Write) GB'] = size_gb
            elif category == 'CloudTrail - Management (ReadOnly)':
                totals['CloudTrail Logs - Management Logs Ingestion (ReadOnly) GB'] = size_gb
            elif category == 'CloudTrail - Storage (S3)':
                totals['CloudTrail Logs - Storage Logs Ingestion (S3) GB'] = size_gb
            elif category == 'CloudTrail - Other Operations':
                totals['CloudTrail Logs - Other Operations GB'] = size_gb
    else:
        totals['CloudTrail Logs Categories Available'] = False
        totals['CloudTrail Logs - Management Logs Ingestion (Write) GB'] = 0.0
        totals['CloudTrail Logs - Management Logs Ingestion (ReadOnly) GB'] = 0.0
        totals['CloudTrail Logs - Storage Logs Ingestion (S3) GB'] = 0.0
        totals['CloudTrail Logs - Other Operations GB'] = 0.0
    return


def discover_cloudtrail_base_paths(s3_client, bucket_name, prefix):
    """ Discover CloudTrail base paths for scanning. """
    print(f"\nDiscovering CloudTrail base paths in s3://{bucket_name}/{prefix}. This can take some time...")

    def _is_digest_path(path):
        """Check if a path is a CloudTrail digest path"""
        path_lower = path.lower()
        return 'cloudtrail-digest' in path_lower or 'cloudtraildigest' in path_lower

    def _list_common_prefixes(bucket, current_prefix):
        common_prefixes_found = []
        try:
            paginator = s3_client.get_paginator('list_objects_v2')
            if current_prefix and not current_prefix.endswith('/'):
                current_prefix += '/'
            page_iterator = paginator.paginate(Bucket=bucket, Prefix=current_prefix, Delimiter='/')
            for page in page_iterator:
                if 'CommonPrefixes' in page:
                    common_prefixes_found.extend([
                        cp['Prefix'] for cp in page['CommonPrefixes']
                        if not _is_digest_path(cp['Prefix'])
                    ])
        except boto3.exceptions.Boto3Error as e:
            error_print(f"Error listing common prefixes under {current_prefix}: {e}", account_context='CloudTrail Discovery')
            debug_exit(f"Error listing common prefixes: {e}")
        return common_prefixes_found

    base_paths = []
    discovery_start_prefix = prefix
    if discovery_start_prefix and not discovery_start_prefix.endswith('/'):
        discovery_start_prefix += '/'

    level1_prefixes = _list_common_prefixes(bucket_name, discovery_start_prefix)
    verbose_print(f"Discovery Level 1 (Account/Org IDs) found under '{discovery_start_prefix}': {level1_prefixes}")

    if not level1_prefixes:
        verbose_print(f"No common prefixes found directly under '{discovery_start_prefix}'. Checking if it's a valid base itself.")
        test_year_prefix = f"{prefix.rstrip('/')}/{datetime.now(timezone.utc).year}/"
        try:
            resp_test = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=test_year_prefix, MaxKeys=1)
            if resp_test.get('KeyCount', 0) > 0 or resp_test.get('CommonPrefixes'):
                base_paths.append(prefix.rstrip('/') + '/')
                verbose_print(f"Treating '{prefix}' as a direct base path for logs.")
                return sorted(list(set(base_paths)))
        except (boto3.exceptions.Boto3Error, botocore.exceptions.ClientError) as e_test:
            verbose_print(f"Test list for '{test_year_prefix}' failed: {e_test}")
        if not base_paths:
            error_print(f"Warning: No common prefixes found under '{discovery_start_prefix}'. Using '{prefix}' as base.", account_context="CloudTrail Discovery")
            base_paths.append(prefix.rstrip('/') + '/')
        return sorted(list(set(base_paths)))

    for l1_prefix in level1_prefixes:
        level2_prefixes = _list_common_prefixes(bucket_name, l1_prefix)
        verbose_print(f"Discovery Level 2 found under '{l1_prefix}': {level2_prefixes}")

        if "o-" in l1_prefix.split('/')[-2]:
            for org_member_acc_prefix in level2_prefixes:
                level3_org_prefixes = _list_common_prefixes(bucket_name, org_member_acc_prefix)
                for service_dir_prefix in level3_org_prefixes:
                    if "cloudtrail" in service_dir_prefix.lower() and not _is_digest_path(service_dir_prefix):
                        region_prefixes = _list_common_prefixes(bucket_name, service_dir_prefix)
                        if not region_prefixes:
                            base_paths.append(service_dir_prefix)
                        else:
                            base_paths.extend(region_prefixes)
        else:
            for service_or_cloudtrail_dir in level2_prefixes:
                if "cloudtrail" in service_or_cloudtrail_dir.lower() and not _is_digest_path(service_or_cloudtrail_dir):
                    region_prefixes = _list_common_prefixes(bucket_name, service_or_cloudtrail_dir)
                    if not region_prefixes:
                        base_paths.append(service_or_cloudtrail_dir)
                    else:
                        base_paths.extend(region_prefixes)

    if not base_paths:
        print(f"Warning: No specific CloudTrail region paths found under '{prefix}'. Using '{prefix.rstrip('/') + '/'}' as fallback.")
        base_paths.append(prefix.rstrip('/') + '/')

    base_paths = [bp for bp in base_paths if not _is_digest_path(bp)]

    return sorted(list(set(base_paths)))


def generate_daily_prefixes(base_paths, days):
    """Generate daily prefixes for scanning based on base paths."""
    daily_prefixes = []
    end_date = datetime.now(timezone.utc)
    for base_path in base_paths:
        for i in range(days):
            d = end_date - timedelta(days=i)
            day_prefix = f"{base_path.rstrip('/')}/{d.year}/{d.month:02d}/{d.day:02d}/"
            daily_prefixes.append(day_prefix)
    return sorted(list(set(daily_prefixes)))


def collect_objects(s3_client, bucket_name, daily_prefixes_to_scan, date_threshold):
    """ Collect CloudTrail log objects and their sizes from a list of daily prefixes. """
    sample_size = args.defend_cloudtrail_logs_bucket_sample_size
    reservoir = []
    items_seen = 0
    total_compressed_size_seen = 0
    reservoir_lock = threading.Lock()

    def _fetch_and_sample_objects(daily_prefix):
        nonlocal items_seen, total_compressed_size_seen
        try:
            if debug_mode_error_occurred.is_set():
                return None

            verbose_print(f"Scanning S3 daily prefix: s3://{bucket_name}/{daily_prefix}")
            paginator = s3_client.get_paginator('list_objects_v2')
            page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=daily_prefix)

            for page in page_iterator:
                if debug_mode_error_occurred.is_set():
                    return None

                if 'Contents' in page:
                    for obj in page['Contents']:
                        if obj['LastModified'] >= date_threshold and obj.get('Key', '').endswith('.gz'):
                            with reservoir_lock:
                                items_seen += 1
                                total_compressed_size_seen += obj.get('Size', 0)

                                if len(reservoir) < sample_size:
                                    reservoir.append(obj)
                                else:
                                    j = random.randint(0, items_seen - 1)
                                    if j < sample_size:
                                        reservoir[j] = obj
        except boto3.exceptions.Boto3Error as e:
            error_print(f"Error listing objects for daily prefix s3://{bucket_name}/{daily_prefix}: {e}", account_context='CloudTrail ParallelList')
            debug_exit(f"Error listing objects: {e}")

        return None

    if daily_prefixes_to_scan:
        parallel_scan_prefixes(daily_prefixes_to_scan, _fetch_and_sample_objects)
    else:
        print("No S3 daily prefixes to scan based on discovery and days parameter.")

    print(f"Scanned {items_seen} CloudTrail log files from S3 and built a sample of {len(reservoir)} files.")
    return reservoir, items_seen, total_compressed_size_seen


def parallel_scan_prefixes(prefixes_to_scan, fetch_func_per_prefix):
    """ Scan multiple S3 prefixes in parallel. """
    aggregated_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_to_prefix_map = {executor.submit(fetch_func_per_prefix, prefix_item): prefix_item for prefix_item in prefixes_to_scan}
        processed_count = 0
        total_prefixes = len(prefixes_to_scan)
        for future in concurrent.futures.as_completed(future_to_prefix_map):
            if debug_mode_error_occurred.is_set():
                print("\n\nDEBUG MODE: Stopping all processing due to error")
                executor.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)

            scanned_prefix_item = future_to_prefix_map[future]
            processed_count += 1
            print(f"Completed S3 prefix scan {processed_count}/{total_prefixes}: {scanned_prefix_item[:50]}...", end='\r')
            try:
                result_from_prefix = future.result()
                if result_from_prefix is not None:
                    aggregated_results.append(result_from_prefix)
            except boto3.exceptions.Boto3Error as exc:
                error_print(f"Exception processing result for S3 prefix {scanned_prefix_item}: {exc}", account_context='CloudTrail ParallelScan')
                debug_exit(f"AWS error during parallel scan: {exc}")
                if debug_mode_error_occurred.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.exit(1)
            except Exception as exc:  # pylint: disable=broad-except
                error_print(f"Unexpected exception processing result for S3 prefix {scanned_prefix_item}: {exc}", account_context='CloudTrail ParallelScan')
                debug_exit(f"Unexpected error during parallel scan: {exc}")
                if debug_mode_error_occurred.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.exit(1)

    print("\nS3 prefix scanning complete.")
    return aggregated_results


def analyze_sample_objects(bucket_name, all_objects):
    """ Sample and analyze CloudTrail log objects. """
    sampled_objects_to_analyze = all_objects
    actual_sample_count_to_process = len(sampled_objects_to_analyze)

    if actual_sample_count_to_process == 0:
        print("No objects found to sample or sample size is zero.")

    results = {
        'sampled_compressed_size': 0, 'sampled_uncompressed_size': 0,
        'aggregated_total_events': 0, 'aggregated_event_size': 0,
        'aggregated_event_types': defaultdict(lambda: {'count': 0, 'size': 0}),
        'aggregated_categories': defaultdict(lambda: {'count': 0, 'size': 0}),
        'successful_samples': 0, 'attempted_sample_count': len(sampled_objects_to_analyze)
    }
    if not sampled_objects_to_analyze:
        print(f"No samples to analyze (attempted: {results['attempted_sample_count']}).")
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_to_obj_key_map = {executor.submit(aws_process_events_sample, bucket_name, obj['Key'], obj['Size']): obj['Key'] for obj in sampled_objects_to_analyze}
        completed_futures = 0
        total_futures_to_process = len(future_to_obj_key_map)
        for future in concurrent.futures.as_completed(future_to_obj_key_map):
            if debug_mode_error_occurred.is_set():
                print("\n\nDEBUG MODE: Stopping all processing due to error")
                executor.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)

            obj_key_processed = future_to_obj_key_map[future]
            completed_futures += 1
            print(f"Processing samples: {completed_futures}/{total_futures_to_process} (File: {obj_key_processed.split('/')[-1]})", end="\r")
            try:
                single_file_result = future.result()
                if single_file_result:
                    process_sample_result(single_file_result, results)
                else:
                    verbose_print(f"Sample processing failed for {obj_key_processed}, results excluded.")
            except (boto3.exceptions.Boto3Error, IOError, json.JSONDecodeError, gzip.BadGzipFile, OSError, UnicodeDecodeError) as e_future:
                error_print(f"Specific error processing sample {obj_key_processed}: {e_future}", account_context='CloudTrail Sample Analysis')
                debug_exit(f"Error processing sample: {e_future}")
                if debug_mode_error_occurred.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.exit(1)
            except Exception as e_future: # pylint: disable=broad-except
                error_print(f"General unexpected error processing future for sample {obj_key_processed}: {e_future}", account_context='CloudTrail Sample Analysis')
                debug_exit(f"Unexpected error processing sample: {e_future}")
                if debug_mode_error_occurred.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.exit(1)
    print(f"\nSample processing complete. Successfully analyzed {results['successful_samples']}/{results['attempted_sample_count']} sampled files.")
    return results


def process_sample_result(single_file_result_dict, aggregated_results_dict):
    """ Process and aggregate a single successfully processed sample file's result. """
    aggregated_results_dict['successful_samples'] += 1
    aggregated_results_dict['sampled_compressed_size'] += single_file_result_dict['compressed_size']
    aggregated_results_dict['sampled_uncompressed_size'] += single_file_result_dict['uncompressed_size']
    aggregated_results_dict['aggregated_total_events'] += single_file_result_dict['events']
    aggregated_results_dict['aggregated_event_size'] += single_file_result_dict['event_size']
    for category, stats in single_file_result_dict['categories'].items():
        aggregated_results_dict['aggregated_categories'][category]['count'] += stats['count']
        aggregated_results_dict['aggregated_categories'][category]['size'] += stats['size']
    for event_type, stats in single_file_result_dict['event_types'].items():
        aggregated_results_dict['aggregated_event_types'][event_type]['count'] += stats['count']
        aggregated_results_dict['aggregated_event_types'][event_type]['size'] += stats['size']


####
# Main
####

def output_results():
    """ Output results """
    try:
        with open(output_file, 'w', encoding='utf-8', newline='') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(['Log Source Type', 'Billable Category', 'Specific Metric', 'Bucket/Prefix Details', 'Estimated 30-Day Uncompressed Volume (GB)'])

            if args.defend_cloudtrail_logs_bucket:
                bucket_prefix_detail_ct = totals.get('CloudTrail Logs Bucket/Prefix', 'N/A')
                analysis_mode_ct = totals.get('CloudTrail Logs Analysis Mode', 'N/A')

                if analysis_mode_ct == 'Error':
                    csv_writer.writerow(['AWS CloudTrail', f'(Analysis Mode: {analysis_mode_ct})', '', bucket_prefix_detail_ct, ''])

                if analysis_mode_ct == 'Basic':
                    csv_writer.writerow([
                        'AWS CloudTrail',
                        'Management Logs Ingestion GB',
                        'Basic Estimation (Total)',
                        bucket_prefix_detail_ct,
                        f"{totals.get('CloudTrail Logs - Management Logs Ingestion (Basic) GB', 0.0):.2f}"
                    ])
                elif analysis_mode_ct == 'Detailed':
                    if totals.get('CloudTrail Logs Categories Available'):
                        csv_writer.writerow([
                            'AWS CloudTrail', 'Management Logs Ingestion GB', 'Write Events', bucket_prefix_detail_ct,
                            f"{totals.get('CloudTrail Logs - Management Logs Ingestion (Write) GB', 0.0):.2f}"
                        ])
                        csv_writer.writerow([
                            'AWS CloudTrail', 'Management Logs Ingestion GB', 'ReadOnly Events', bucket_prefix_detail_ct,
                            f"{totals.get('CloudTrail Logs - Management Logs Ingestion (ReadOnly) GB', 0.0):.2f}"
                        ])
                        csv_writer.writerow([
                            'AWS CloudTrail', 'Storage Logs Ingestion GB', 'S3 Data Events', bucket_prefix_detail_ct,
                            f"{totals.get('CloudTrail Logs - Storage Logs Ingestion (S3) GB', 0.0):.2f}"
                        ])
                        csv_writer.writerow([
                            'AWS CloudTrail', 'N/A (Other)', 'Other Operations', bucket_prefix_detail_ct,
                            f"{totals.get('CloudTrail Logs - Other Operations GB', 0.0):.2f}"
                        ])
                    else:
                        csv_writer.writerow(['AWS CloudTrail', 'Detailed Categories', 'No data processed or available', bucket_prefix_detail_ct, ''])
                elif analysis_mode_ct == 'Error':
                    csv_writer.writerow(['AWS CloudTrail', 'Error', 'Error during processing', bucket_prefix_detail_ct, ''])


            if args.defend_vpc_flow_logs_bucket:
                bucket_prefix_detail_vpc = totals.get('VPC Flow Logs Bucket/Prefix', 'N/A')
                csv_writer.writerow([
                    'AWS VPC Flow Logs', 'AWS VPC Flow Logs Ingestion GB', 'Basic Estimation', bucket_prefix_detail_vpc,
                    f"{totals.get('VPC Flow Logs - Network Logs Ingestion (Basic) GB', 0.0):.2f}"
                ])

            if args.defend_route53_resolver_logs_bucket:
                bucket_prefix_detail_r53 = totals.get('Route 53 Resolver Query Logs Bucket/Prefix', 'N/A')
                csv_writer.writerow([
                    'AWS Route 53 Resolver Query Logs', 'Network Logs Ingestion GB', 'Basic Estimation', bucket_prefix_detail_r53,
                    f"{totals.get('Route 53 Resolver Query Logs - Network Logs Ingestion (Basic) GB', 0.0):.2f}"
                ])
    except IOError as e_csv:
        error_print(f"Failed to write to CSV file {output_file}: {e_csv}")
        debug_exit(f"Failed to write CSV file: {e_csv}")

    if errors_log:
        try:
            with open(error_log_file, 'w', encoding='utf-8') as err_file:
                for error in errors_log:
                    err_file.write(error + "\n")
        except IOError as e_err:
            print(f"CRITICAL: Failed to write to error log file {error_log_file}: {e_err}")
            for error_item in errors_log:
                print(error_item)
            debug_exit(f"Failed to write error log file: {e_err}")

    print(f"\nAWS Log Volume Estimation Results (script version: {version})\n")
    if not (args.defend_cloudtrail_logs_bucket or args.defend_vpc_flow_logs_bucket or args.defend_route53_resolver_logs_bucket):
        print("No log buckets specified for estimation. Use one of the --defend-*-logs-bucket arguments.")
        print(f"\nReview {output_file} (if created) and {error_log_file} for any errors or details.")
        return

    print("Wiz Defend Ingestion: AWS Log Volume Estimation (Uncompressed, Normalized to 30 days)\n")

    if args.defend_cloudtrail_logs_bucket:
        print("Log Source: AWS CloudTrail")
        print(f"  Bucket/Prefix: {totals.get('CloudTrail Logs Bucket/Prefix', 'Not Specified')}")
        analysis_mode_ct = totals.get('CloudTrail Logs Analysis Mode', 'N/A')
        print(f"  Analysis Mode: {analysis_mode_ct}")
        if analysis_mode_ct == 'Basic':
            print("  Billable Category: Management Logs Ingestion GB")
            metric_display_name = "Basic Estimation (Total Management Logs)"
            print(f"    {metric_display_name.ljust(padding_desc)}: {totals.get('CloudTrail Logs - Management Logs Ingestion (Basic) GB', 0.0):.2f} GB")
            print(f"      (Based on S3 metrics and compression factor {args.defend_cloudtrail_logs_compression_factor}x)")
        elif analysis_mode_ct == 'Detailed':
            if totals.get('CloudTrail Logs Categories Available'):
                print("  Billable Category: Management Logs Ingestion GB")
                print(f"    {'Write Events'.ljust(padding_desc)}: {totals.get('CloudTrail Logs - Management Logs Ingestion (Write) GB', 0.0):.2f} GB")
                print(f"    {'ReadOnly Events'.ljust(padding_desc)}: {totals.get('CloudTrail Logs - Management Logs Ingestion (ReadOnly) GB', 0.0):.2f} GB")
                print("  Billable Category: Storage Logs Ingestion GB")
                print(f"    {'S3 Data Events'.ljust(padding_desc)}: {totals.get('CloudTrail Logs - Storage Logs Ingestion (S3) GB', 0.0):.2f} GB")
                print("  Non-Billable Category: Other Operations")
                print(f"    {'CloudTrail - Other Operations'.ljust(padding_desc)}: {totals.get('CloudTrail Logs - Other Operations GB', 0.0):.2f} GB")
                print(f"      (Detailed figures normalized to 30 days from {args.defend_cloudtrail_logs_bucket_days} analyzed days)")
            else:
                print(f"  {'Detailed Categories:'.ljust(padding_desc)} No data processed or available from the sample.")
        elif analysis_mode_ct == 'Error':
            print(f"  Could not process CloudTrail data for {totals.get('CloudTrail Logs Bucket/Prefix')}.")
        print("")

    if args.defend_vpc_flow_logs_bucket:
        print("Log Source: AWS VPC Flow Logs")
        print(f"  Bucket: {totals.get('VPC Flow Logs Bucket/Prefix', 'Not Specified')}")
        print("  Analysis Mode: Basic")
        print("  Billable Category: AWS VPC Flow Logs Ingestion GB")
        metric_display_name = "Basic Estimation (Total VPC Flow Logs)"
        print(f"    {metric_display_name.ljust(padding_desc)}: {totals.get('VPC Flow Logs - Network Logs Ingestion (Basic) GB', 0.0):.2f} GB")
        print(f"      (Based on S3 metrics and compression factor {args.defend_vpc_flow_logs_compression_factor}x)")
        print("")

    if args.defend_route53_resolver_logs_bucket:
        print("Log Source: AWS Route 53 Resolver Query Logs")
        print(f"  Bucket: {totals.get('Route 53 Resolver Query Logs Bucket/Prefix', 'Not Specified')}")
        print("  Analysis Mode: Basic")
        print("  Billable Category: Network Logs Ingestion GB")
        metric_display_name = "Basic Estimation (Total Network Logs)"
        print(f"    {metric_display_name.ljust(padding_desc)}: {totals.get('Route 53 Resolver Query Logs - Network Logs Ingestion (Basic) GB', 0.0):.2f} GB")
        print(f"      (Based on S3 metrics and compression factor {args.defend_route53_resolver_logs_compression_factor}x)")
        print("")

    if args.defend_cloudtrail_logs_bucket and totals.get('CloudTrail Logs Analysis Mode') == 'Basic':
        print("---\nDisclaimer and Recommendations\n")
        print("* \033[1mAbout Basic Mode:\033[0m The \033[1mBasic Estimation\033[0m shown above relies on S3 CloudWatch metrics (representing compressed data size) and applies an assumed compression factor.")
        print("  This provides a general estimate but may not precisely reflect the true uncompressed log volume.\n")
        print("* \033[1mFor a More Accurate Analysis:\033[0m To get a more precise breakdown of CloudTrail log volumes, you can run the script in \033[1mDetailed Analysis\033[0m mode.")
        print("  This mode samples, downloads, and analyzes the actual log files to categorize events more accurately. This will also differ from the actual log volume due to sampling. To use it, add the `--defend-detailed` flag to your command:")
        print("    \033[3mpython3 log-volume-estimation-aws.py --defend-cloudtrail-logs-bucket <your-bucket-name> --defend-detailed\033[0m\n")
        print("* \033[1mExecution Time:\033[0m Please be aware that running this script, especially in detailed mode, can take a considerable amount of time.")
        print("  The detailed analysis involves numerous API calls to list and download objects from your S3 bucket, which may also incur minor AWS API costs.")
        print("---")

    print(f"\nDetails written to {output_file}")
    if errors_log:
        print(f"\nExceptions or errors occurred. Review {error_log_file} or rerun with '--debug'.")
    else:
        print(f"No errors reported to {error_log_file}.")

def main():
    """ Main execution function """
    print("Starting AWS Log Volume Estimator for Wiz Defend...")

    if not (args.defend_cloudtrail_logs_bucket or args.defend_vpc_flow_logs_bucket or args.defend_route53_resolver_logs_bucket):
        print("\nError: No log buckets specified.")
        print("Please provide at least one of the following arguments:")
        print("  --defend-cloudtrail-logs-bucket <bucket-name>")
        print("  --defend-vpc-flow-logs-bucket <bucket-name>")
        print("  --defend-route53-resolver-logs-bucket <bucket-name>")
        print("\nUse -h or --help for a full list of options.")
        sys.exit(1)

    if args.defend_cloudtrail_logs_bucket:
        print(f"\nEstimating AWS CloudTrail log volume for bucket: {args.defend_cloudtrail_logs_bucket}")
        if args.defend_detailed:
            print("Mode: Detailed Analysis (sampling logs)")
            estimate_cloudtrail_volume_detailed()
        else:
            print("Mode: Basic Estimation (using S3 CloudWatch metrics)")
            estimate_bucket_volume_basic(args.defend_cloudtrail_logs_bucket, "defend_cloudtrail_logs_bucket")

    if args.defend_vpc_flow_logs_bucket:
        print(f"\nEstimating AWS VPC Flow Logs volume for bucket: {args.defend_vpc_flow_logs_bucket}")
        print("Mode: Basic Estimation (using S3 CloudWatch metrics)")
        estimate_bucket_volume_basic(args.defend_vpc_flow_logs_bucket, "defend_vpc_flow_logs_bucket")

    if args.defend_route53_resolver_logs_bucket:
        print(f"\nEstimating AWS Route 53 Resolver Query Logs volume for bucket: {args.defend_route53_resolver_logs_bucket}")
        print("Mode: Basic Estimation (using S3 CloudWatch metrics)")
        estimate_bucket_volume_basic(args.defend_route53_resolver_logs_bucket, "defend_route53_resolver_logs_bucket")

    output_results()
    print("\nEstimation complete.")

####

if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
