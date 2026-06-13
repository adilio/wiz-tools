#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines

""" Wiz : Resource Count : Snowflake """

import argparse
import csv
import inspect
import os
import re
import signal
import sys
import time

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import snowflake.connector
except ImportError:
    print("\nERROR: Missing required Snowflake packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade snowflake-connector-python")
    sys.exit(1)


version='2.8.0'


####
# Command Line Arguments
####


parser = argparse.ArgumentParser(description = 'Count Snowflake Resources')
parser.add_argument(
    '--all',
    action = 'store_true',
    dest = 'all',
    help = 'Count resources in all Accounts in the Snowflake Organization (default: disabled)',
    default = False
)
parser.add_argument(
    '--account',
    help = 'Count resources using the specified Snowflake Account (format: ORGANIZATION-ACCOUNT, env as SNOWFLAKE_ACCOUNT)',
    default = os.environ.get('SNOWFLAKE_ACCOUNT')
)
parser.add_argument(
    '--role',
    help = 'Count resources using the specified Snowflake Role (optional, default: ACCOUNTADMIN)',
    default = 'ACCOUNTADMIN'
)
parser.add_argument(
    '--warehouse',
    help = 'Count resources using the specified Snowflake Warehouse (optional)',
)
parser.add_argument(
    '--private_key_file',
    help = 'Count resources using the specified Snowflake Private Key (env as SNOWFLAKE_PRIVATE_KEY_FILE)',
    default = os.environ.get('SNOWFLAKE_PRIVATE_KEY_FILE')
)
parser.add_argument(
    '--private_key_file_pwd',
    help = 'Snowflake Private Key Password (optional, env as SNOWFLAKE_PRIVATE_KEY_FILE_PWD)',
    default = os.environ.get('SNOWFLAKE_PRIVATE_KEY_FILE_PWD')
)
parser.add_argument(
    '--token',
    help = 'Count resources using the specified Snowflake Token (env as SNOWFLAKE_TOKEN)',
    default = os.environ.get('SNOWFLAKE_TOKEN')
)
parser.add_argument(
    '--username',
    help = 'Count resources in the specified Snowflake Username (env as SNOWFLAKE_USERNAME)',
    dest = 'user_name',
    default = os.environ.get('SNOWFLAKE_USERNAME'),
)
parser.add_argument(
    '--password',
    dest = 'pass_word',
    help = 'Count resources using the specified Snowflake Password (env as SNOWFLAKE_PASSWORD)',
    default = os.environ.get('SNOWFLAKE_PASSWORD')
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
    '--max-accounts',
    dest = 'max_accounts',
    help = 'Stop after scanning N accounts (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--checkpoint-interval',
    dest = 'checkpoint_interval',
    help = 'Write partial output every N completed accounts (default: 0, disabled)',
    type = int,
    default = 0
)
parser.add_argument(
    '--start-after-account',
    dest = 'start_after_account',
    help = 'Skip accounts until after this account name, useful for resuming sorted --all scans',
    default = None
)
parser.add_argument(
    '--include-account-regex',
    dest = 'include_account_regex',
    help = 'Only scan accounts whose name matches this regular expression',
    default = None
)
parser.add_argument(
    '--exclude-account-regex',
    dest = 'exclude_account_regex',
    help = 'Skip accounts whose name matches this regular expression',
    default = None
)
args = parser.parse_args()

# Required arguments are too complex for argparse when using both defaults and env vars.

if not args.account:
    print()
    print("ERROR: Must specify --account")
    sys.exit(1)

_parts = args.account.split('-', 1)
if len(_parts) < 2:
    args_organization, args_account = '', args.account
else:
    args_organization, args_account = _parts
if not (args_organization and args_account):
    print()
    print("ERROR: Must specify --account in format: ORGANIZATION-ACCOUNT")
    sys.exit(1)

# Support key-pair, token, and user/password authentication
# https://docs.snowflake.com/en/user-guide/key-pair-auth
# https://docs.snowflake.com/en/user-guide/oauth-intro

if not (args.private_key_file or args.token or args.user_name):
    print()
    print("ERROR: Must specify specify --private_key_file or --token or --username")
    sys.exit(1)

if args.user_name and not args.pass_word:
    print()
    print("ERROR: Must specify both --username and --password")
    sys.exit(1)

include_account_pattern = re.compile(args.include_account_regex) if args.include_account_regex else None
exclude_account_pattern = re.compile(args.exclude_account_regex) if args.exclude_account_regex else None


####
# Configuration and Globals
####

output_file     = 'snowflake-resources.csv'
output_file_log = 'snowflake-resources-log.csv'
error_log_file  = 'snowflake-errors-log.txt'
padding = 6

totals = {
    'Snowflake Database Schemas': 0
}

totals_log = []
errors_log = []
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


def account_matches_filters(account_name):
    if include_account_pattern and not include_account_pattern.search(account_name):
        return False
    if exclude_account_pattern and exclude_account_pattern.search(account_name):
        return False
    return True


def signal_handler(_signal_received, _frame):
    """ Control-C """
    status_print("[INTERRUPTED] Writing partial results before exiting.")
    output_results(last_account_names, last_database_names, partial=True)
    sys.exit(0)


def progress_print(resource_count, resource_type, account='', database=''):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type} in {account} {database}".split()))
    totals_log.append([resource_type, resource_count, account, database])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


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
    errors_log.append(f"ERROR: {account} {function} {details}")


####
# Customized Library Code
####


def get_accounts(connection_params):
    """ Get Snowflake Accounts in this Organization """
    try:
        connection = snowflake.connector.connect(**connection_params)
        cursor = connection.cursor()
        cursor.execute("SHOW ACCOUNTS")
        result = cursor.fetchall()
        verbose_print(result)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, connection_params['account'])
        error_print("Error getting Snowflake Accounts in this Organization.")
        return []
    cursor.close()
    connection.close()
    return result


def get_databases(connection, account):
    """ Get Snowflake Databases in this Account """
    try:
        cursor = connection.cursor()
        # https://docs.snowflake.com/en/sql-reference/account-usage/databases
        cursor.execute("SELECT DATABASE_NAME,TYPE FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES WHERE DELETED IS NULL AND TYPE != 'IMPORTED DATABASE' ORDER BY DATABASE_NAME")
        result = cursor.fetchall()
        cursor.close()
        verbose_print(result)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        ex_str = str(ex)
        if 'No active warehouse' in ex_str or '250001' in ex_str:
            print()
            print("ERROR: No active warehouse selected for this Snowflake session.")
            print("       Specify a warehouse with --warehouse <WAREHOUSE_NAME> and retry.")
            sys.exit(1)
        error_print(ex, account)
        error_print("Error getting Snowflake Databases in this Account.")
        return []
    return list(result)


def get_schemas(connection, account, database):
    """ Get schemas in this Snowflake Database """
    try:
        cursor = connection.cursor()
        # https://docs.snowflake.com/en/sql-reference/info-schema/schemata
        safe_db = database.replace('"', '""')
        cursor.execute(f'SELECT CATALOG_NAME,SCHEMA_NAME,SCHEMA_OWNER,IS_TRANSIENT,IS_MANAGED_ACCESS FROM "{safe_db}".INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME')
        result = cursor.fetchall()
        cursor.close()
        verbose_print(result)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, account)
        error_print("Error getting Snowflake Schemas in this Database.")
        return []
    schemas = [schema for schema in result if schema[1] != 'INFORMATION_SCHEMA']
    schema_count = len(schemas)
    if schema_count > 0 or args.verbose_mode:
        progress_print(resource_count=schema_count, resource_type='Snowflake Database Schemas', account=account, database=database)
        totals['Snowflake Database Schemas'] += schema_count
    return schemas


def output_results(account_names, database_names, partial=False):
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
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Account', 'Database'])
        for item in totals_log:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(output_path(error_log_file), 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    label = "Partial results" if partial else "Results"
    print(f"\n{label} across {len(database_names)} Snowflake Databases in {len(account_names)} Snowflake Accounts (script version: {version})\n")
    if partial:
        print("Scan interrupted; results above cover completed accounts only.\n")
    print(f"{str(totals['Snowflake Database Schemas']).rjust(padding)} Snowflake Database Schemas")
    print(f"\nDetails written to {output_file} and {output_file_log}")

    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} for error details.")


def main():
    """ Calculon Compute! """
    global last_account_names, last_database_names  # pylint: disable=global-statement
    account_names  = []
    database_names = []

    organization, account = args.account.split('-', 1)
    connection_params= {'account': args.account, 'role': args.role, 'warehouse': args.warehouse,
                        'login_timeout': 30, 'network_timeout': 60}
    # Support key-pair, token, and user/password authentication
    if args.private_key_file:
        connection_params['private_key_file'] = args.private_key_file
        if args.private_key_file_pwd :
            connection_params['private_key_file_pwd'] = args.private_key_file_pwd
    elif args.token:
        connection_params['token']    = args.token
    else:
        connection_params['user']     = args.user_name
        connection_params['password'] = args.pass_word

    if args.all:
        print()
        print(f"Getting all Snowflake Accounts in Organization: {organization}")
        print()
        accounts = get_accounts(connection_params)
        for account in accounts:
            account_name = account[1]
            account_names.append(account_name)
            organization_account_name = f"{organization}-{account_name}"
            print(f"- Found Account: {organization_account_name}")
    else:
        print()
        print(f"Using Account {args.account}")
        account_names = [account]
    print()

    last_account_names = []
    last_database_names = []
    past_start_after = not args.start_after_account
    scanned_count = 0
    print("Getting Databases for each Snowflake Account ...")
    print()

    try:
        for index, account_name in enumerate(account_names, start=1):
            organization_account_name = f"{organization}-{account_name}"
            if not past_start_after:
                if account_name == args.start_after_account:
                    past_start_after = True
                else:
                    status_print(f"[SKIP] Account {index}/{len(account_names)}: {organization_account_name} (before --start-after-account)")
                continue
            if not account_matches_filters(organization_account_name):
                status_print(f"[SKIP] Account {index}/{len(account_names)}: {organization_account_name}")
                continue
            if max_runtime_reached():
                status_print(f"[STOP] Max runtime of {args.max_run_minutes}m reached after {scanned_count} account(s).")
                output_results(last_account_names, last_database_names, partial=True)
                return
            if args.max_accounts and scanned_count >= args.max_accounts:
                status_print(f"[STOP] Reached --max-accounts {args.max_accounts}.")
                output_results(last_account_names, last_database_names, partial=True)
                return
            status_print(f"[SCAN] Account {index}/{len(account_names)}: {organization_account_name}")
            account_params = dict(connection_params)
            account_params['account'] = organization_account_name
            try:
                connection = snowflake.connector.connect(**account_params)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                error_print(ex, organization_account_name)
                error_print("Error connecting to Snowflake Account. Skipping.")
                continue
            try:
                databases = get_databases(connection, organization_account_name)
                for database in databases:
                    database_name = database[0]
                    database_names.append(database_name)
                    last_database_names.append(database_name)
                    get_schemas(connection, organization_account_name, database_name)
                    print()
            finally:
                connection.close()
            status_print(f"[DONE] Account complete: {organization_account_name} ({len(databases)} database(s))")
            last_account_names.append(account_name)
            scanned_count += 1
            if args.checkpoint_interval and scanned_count % args.checkpoint_interval == 0:
                status_print(f"[CHECKPOINT] {scanned_count} account(s) complete.")
                output_results(last_account_names, last_database_names, partial=True)
    except Exception:
        output_results(last_account_names, last_database_names, partial=True)
        raise

    output_results(account_names, database_names)


####


if __name__ == "__main__":
    last_account_names = []
    last_database_names = []
    signal.signal(signal.SIGINT,signal_handler)
    main()
