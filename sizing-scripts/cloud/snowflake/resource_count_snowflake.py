#!/usr/bin/env python3

# pylint: disable=invalid-name, too-many-lines

""" Wiz : Resource Count : Snowflake """

import argparse
import csv
import inspect
import os
import signal
import sys

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
args = parser.parse_args()

# Required arguments are too complex for argparse when using both defaults and env vars.

if not args.account:
    print()
    print("ERROR: Must specify --account")
    sys.exit(1)

args_organization, args_account = args.account.split('-')
if not args_organization and args_account:
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


####
# Common Library Code
####


def signal_handler(_signal_received, _frame):
    """ Control-C """
    print("\nExiting")
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


def get_databases(connection_params, account):
    """ Get Snowflake Databases in this Account """
    try:
        connection_params['account'] = account
        connection = snowflake.connector.connect(**connection_params)
        cursor = connection.cursor()
        # https://docs.snowflake.com/en/sql-reference/account-usage/databases
        # cursor.execute("SELECT DATABASE_NAME FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES WHERE DELETED IS NULL AND TYPE NOT IN ('APPLICATION','IMPORTED DATABASE') ORDER BY DATABASE_NAME")
        cursor.execute("SELECT DATABASE_NAME,TYPE FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES WHERE DELETED IS NULL AND TYPE != 'IMPORTED DATABASE' ORDER BY DATABASE_NAME")
        result = cursor.fetchall()
        verbose_print(result)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, connection_params['account'])
        error_print("Error getting Snowflake Databases in this Account.")
        return []
    cursor.close()
    connection.close()
    databases = []
    for database in result:
        databases.append(database)
    return databases


def get_schemas(connection_params, account, database):
    """ Get schemas in this Snowflake Database """
    try:
        connection_params['account'] = account
        connection = snowflake.connector.connect(**connection_params)
        cursor = connection.cursor()
        # https://docs.snowflake.com/en/sql-reference/info-schema/schemata
        cursor.execute(f"SELECT CATALOG_NAME,SCHEMA_NAME,SCHEMA_OWNER,IS_TRANSIENT, IS_MANAGED_ACCESS FROM {database}.INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME")
        result = cursor.fetchall()
        verbose_print(result)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, connection_params['account'])
        error_print("Error getting Snowflake Schemas in this Database.")
        return []
    cursor.close()
    connection.close()
    schemas = []
    for schema in result:
        # Drop INFORMATION_SCHEMA as per Wiz Inventory.
        if schema[1] == 'INFORMATION_SCHEMA':
            continue
        schemas.append(schema)
    schema_count = len(schemas)
    if schema_count > 0  or args.verbose_mode:
        progress_print(resource_count=schema_count, resource_type='Snowflake Database Schemas', account=account, database=database)
        totals['Snowflake Database Schemas'] += schema_count
    return schemas


def output_results(account_names, database_names):
    """ Output results """
    # Summary File
    with open(output_file, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals.items():
            csv_writer.writerow([resource_type, resource_count])
    # Log File
    with open(output_file_log, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Account', 'Database'])
        for item in totals_log:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(error_log_file, 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    print(f"\nResults across {len(database_names)} Snowflake Databases in {len(account_names)} Snowflake Accounts (script version: {version})\n")
    print(f"{str(totals['Snowflake Database Schemas']).rjust(padding)} Snowflake Database Schemas")
    print(f"\nDetails written to {output_file} and {output_file_log}")

    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


def main():
    """ Calculon Compute! """
    account_names  = []
    database_names = []

    organization, account = args.account.split('-')
    connection_params= {'account': args.account, 'role': args.role, 'warehouse': args.warehouse}
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

    print("Getting Databases for each Snowflake Account ...")
    print()

    for account_name in account_names:
        organization_account_name = f"{organization}-{account_name}"
        databases = get_databases(connection_params, organization_account_name)
        for database in databases:
            database_name = database[0]
            database_names.append(database_name)
            get_schemas(connection_params, organization_account_name, database_name)
            print()

    output_results(account_names, database_names)


####


if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
