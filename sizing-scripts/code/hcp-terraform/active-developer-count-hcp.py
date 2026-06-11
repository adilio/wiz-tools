#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Active Developer Count : HCP Terraform """

import argparse
import datetime
import hashlib
import os
import signal
import sys
import time

import requests


version='2.5.8'


####
# Command Line Arguments
####


parser = argparse.ArgumentParser(description = 'Count HCP Terraform Active Developers')
parser.add_argument(
    '--token',
    help = 'Specify the token to use to access HCP Terraform (required)',
    required = True
)
parser.add_argument(
    '--exclude-cvs',
    action = 'store_true',
    dest = 'exclude_configuration_versions',
    help = 'Exclude counting users in sources already counted by other active developer scripts (default: disabled)',
    default = False
)
parser.add_argument(
    '--decrypt',
    action = 'store_true',
    help = 'Decrypt email addresses in output files (default: disabled)',
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


headers = {
    "Authorization": f"Bearer {args.token}",
    "Content-Type":  "application/vnd.api+json"
}

output_file    = 'active-developers.txt'
number_of_days = 90

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


def error_print(details):
    """ Error output """
    print(f"\nERROR: {details}")


def days_ago_iso():
    """ Calculate a DateTime a number of days ago """
    dt_now = datetime.datetime.now()
    dt_off = datetime.timedelta(days=number_of_days)
    result = (dt_now - dt_off).isoformat()
    verbose_print(f"Days Ago: {number_of_days} is: {result}")
    return result


def output_results_across_version_control_systems():
    """  Output results across all scanned version control systems """
    developers = []
    for file in os.listdir():
        if file.endswith('-developers.txt') and file != output_file:
            with open(file, 'r', encoding='utf-8') as developers_file:
                developers.extend(developers_file.read().split())
    # Deduplicate developers.
    developers = sorted(set(developers))
    with open(output_file, 'w', encoding='utf-8') as developer_file:
        for developer_email in developers:
            # Encrypt sensitive data before writing to disk.
            if not args.decrypt:
                if developer_email is not None:
                    developer_email = hashlib.sha256(developer_email.encode()).hexdigest()
            developer_file.write(f"{developer_email}\n")
    print()
    print(f"- {len(developers)} Total Developers across all Version Control Systems scanned in this directory")
    print()
    print("To reset the Total Developers count, delete all of the '*-developers.txt' files in this directory")


####
# Customized Library Code
####


# https://developer.hashicorp.com/terraform/cloud-docs/api-docs


def paginated_api_call(url: str, params: dict = None):
    """ API Call with Pagination """
    result = {}
    verbose_print(url)
    links = {'next': url}
    while links['next']:
        response = requests.get(links['next'], params=params, headers=headers, timeout=360)
        if response.status_code == 200:
            response_dict = response.json()
            verbose_print(response_dict)
            objects = response_dict.get('data', [])
            for obj in objects:
                verbose_print(f"ID {obj['id']}")
                result[obj['id']] = obj
            links = response_dict.get('links', {'next': None})
        elif response.status_code == 429:
            verbose_print('Rate Limit Exceeded')
            time.sleep(1)
        else:
            error_print(f"{response.status_code}, {response.text}")
    return result


# An Organization contains one or more Projects.


def get_organizations():
    """ Organizations """
    verbose_print('get_organizations')
    url = "https://app.terraform.io/api/v2/organizations"
    return paginated_api_call(url)


# Users are added to Organizations by inviting them to join.
# Once accepted, they become members of the organization.
# The Organization Membership resource represents this membership.


def get_organization_memberships(organization_id: str):
    """ Memberships """
    verbose_print('get_organization_memberships')
    url = f"https://app.terraform.io/api/v2/organizations/{organization_id}/organization-memberships"
    membership_filter = {'filter[status]': 'active'}
    return paginated_api_call(url, membership_filter)


# Projects organize Workspaces into groups.


def get_organization_projects(organization_id: str):
    """ Projects """
    verbose_print('get_organization_projects')
    url = f"https://app.terraform.io/api/v2/organizations/{organization_id}/projects"
    return paginated_api_call(url)


# Run Tasks are reusable configurations that you can associate to any Workspace in an Organization.


def get_organization_run_tasks(organization_id: str):
    """ Run Tasks """
    verbose_print('get_organization_run_tasks')
    url = f"https://app.terraform.io/api/v2/organizations/{organization_id}/tasks"
    return paginated_api_call(url)


# Teams are groups of HCP Terraform User within an Organization.
# If a User belongs to at least one Team in an Organization, they are considered a member of that Organization.


def get_organization_teams(organization_id: str):
    """ Teams """
    verbose_print('get_organization_teams')
    url = f"https://app.terraform.io/api/v2/organizations/{organization_id}/teams"
    return paginated_api_call(url)


# A Workspace is a group of infrastructure resources managed by Terraform.


def get_organization_workspaces(organization_id: str):
    """ Workspaces """
    verbose_print('get_organization_workspaces')
    url = f"https://app.terraform.io/api/v2/organizations/{organization_id}/workspaces"
    return paginated_api_call(url)


# HCP Terraform is designed as an execution platform for Terraform, and can perform Terraform runs on its own disposable virtual machines.


def get_organization_runs(organization_id: str, query_params: dict = None):
    """ Runs """
    verbose_print('get_organization_runs')
    url = f"https://app.terraform.io/api/v2/organizations/{organization_id}/runs"
    return paginated_api_call(url, query_params)


def get_workspace_runs(workspace_id: str, query_params: dict = None):
    """ Runs """
    verbose_print('get_workspace_runs')
    url = f"https://app.terraform.io/api/v2/workspaces/{workspace_id}/runs"
    return paginated_api_call(url, query_params)


# User accounts belong to individual people.
# Each User can be part of one or more Teams, which are granted permissions on Workspaces within an Organization.
# A User can be a member of multiple Organizationa.


def get_user(user_id: str):
    """ User (does not include email) """
    verbose_print('get_user')
    url = f"https://app.terraform.io/api/v2/users/{user_id}"
    response = requests.get(url, headers=headers, timeout=360)
    if response.status_code == 200:
        user = response.json().get("data", {})
        verbose_print(f"ID: {user['id']} Name: {user['attributes']['username']}")
    else:
        user = {}
        error_print(f"{response.status_code}, {response.text}")
    return user


# A configuration version is a resource used to reference the uploaded configuration files.


def get_configuration_version(configuration_version_id: str):
    """ Configuration Version """
    verbose_print('get_configuration_version')
    url = f"https://app.terraform.io/api/v2/configuration-versions/{configuration_version_id}"
    response = requests.get(url, headers=headers, timeout=360)
    if response.status_code == 200:
        configuration_version = response.json().get("data", {})
        verbose_print(f"Configuration Version: {configuration_version}")
    else:
        configuration_version = {}
        error_print(f"{response.status_code}, {response.text}")
    return configuration_version


# An ingress attributes resource is used to reference commit information for configuration versions created in a workspace with a VCS repository.


def get_configuration_version_commit(configuration_version_id: str):
    """ Configuration Version """
    verbose_print('get_configuration_version')
    url = f"https://app.terraform.io/api/v2/configuration-versions/{configuration_version_id}/ingress-attributes"
    response = requests.get(url, headers=headers, timeout=360)
    if response.status_code == 200:
        configuration_version_commit = response.json().get("data", {})
        verbose_print(f"Configuration Version Commit: {configuration_version_commit}")
    else:
        configuration_version_commit = {}
        error_print(f"{response.status_code}, {response.text}")
    return configuration_version_commit


# Cannot output results across version control systems, as HCPT does not return email addresses.


def output_results(developers):
    """ Output Results """
    # Developer File
    developer_file_name = 'hcpt-developers.txt'
    with open(developer_file_name, 'w', encoding='utf-8') as developer_file:
        for developer in developers:
            # Encrypt sensitive data before writing to disk.
            if not args.decrypt:
                if developer is not None:
                    developer = hashlib.sha256(developer.encode()).hexdigest()
            developer_file.write(f"{developer}\n")

    # Summary
    print()
    print(f"\nResults (Active Developers in the last {number_of_days} days)\n")
    print(f"- {len(developers)} Developers")
    output_results_across_version_control_systems()


####
# Main
####


# pylint: disable=consider-using-dict-items,too-many-branches,too-many-locals,too-many-statements
def main():
    """ Calculon Compute! """

    days_ago = days_ago_iso()

    member_cache          = {}
    run_cache             = {}
    service_account_cache = {}
    user_cache            = {}
    developers            = {}

    # filter[source]
    # Optionally exclude counting users in sources already counted by our other active developer scripts.
    if args.exclude_configuration_versions:
        run_filter = {'filter[source]': 'tfe-ui,tfe-api'}
    else:
        run_filter = {'filter[source]': 'tfe-ui,tfe-api,tfe-configuration-version'}
    # filter[timeframe]
    # Include runs in the last year.
    # An integer year or the string "year" for the past year are valid values.
    # If omitted, the endpoint returns all runs since the creation of the workspace.
    run_filter['filter[timeframe]'] = 'year'

    print("Scanning HCP Terraform")

    print()
    print('Collecting Organizations, please wait ...')
    organizations = get_organizations()
    for organization_id in organizations:
        print()
        print(f"Organization: {organizations[organization_id]['attributes']['name']} ({organization_id})")
        print('    Collecting Organization Memberships, please wait ...')
        memberships = get_organization_memberships(organization_id)
        for membership_id in memberships:
            member = memberships[membership_id]
            user_id = member['relationships']['user']['data']['id']
            print(f"        Organization Member: {member['attributes']['email']} ({user_id})")
            member_cache[user_id] = member
        print('    Done (Organization Memberships)')

        print('    Collecting Workspaces, please wait ...')
        workspaces = get_organization_workspaces(organization_id)
        for workspace_id in workspaces:
            print(f"        Workspace: {workspaces[workspace_id]['attributes']['name']} ({workspace_id})")
            print( '            Collecting Workspace-Level Runs, please wait ...')
            runs = get_workspace_runs(workspace_id, run_filter)
            for run_id in runs:
                run = runs[run_id]
                # Do not recount runs returned by previous Organization or Workspace Run API calls.
                if run_id in run_cache:
                    continue
                run_cache[run_id] = run_id
                created_at = run['attributes']['created-at']
                if created_at < days_ago:
                    # Do not count runs older than the last number of days.
                    continue
                if run['attributes']['source'] in ['tfe-ui', 'tfe-ui,tfe-api']:
                    if 'created-by' not in run['relationships']:
                        continue
                    created_by = run['relationships']['created-by']['data']
                    # Validate if this check is necessary.
                    if created_by['type'] != 'users':
                        continue
                    if created_by['id'] in service_account_cache:
                        user = service_account_cache[created_by['id']]
                    elif created_by['id'] in user_cache:
                        user = user_cache[created_by['id']]
                    else:
                        user = get_user(created_by['id'])
                        if not user:
                            continue
                    if user['attributes']['is-service-account']:
                        service_account_cache[user['id']] = user
                    else:
                        user_cache[user['id']] = user
                    if user['id'] in member_cache:
                        # Use email from Organization Memberships, when available, to deduplicate across our other active developer scripts.
                        developers[member_cache[user['id']]['attributes']['email']] = user
                        print(f"                Workspace Run: {run_id} Created by User: {user['attributes']['username']} ({user['id']}/{member_cache[user['id']]['attributes']['email']}) Service Account: {user['attributes']['is-service-account']}")
                    else:
                        developers[user['id']] = user
                        print(f"                Workspace Run: {run_id} Created by User: {user['attributes']['username']} ({user['id']}) Service Account: {user['attributes']['is-service-account']}")
                elif run['attributes']['source'] == 'tfe-configuration-version':
                    if 'configuration-version' not in run['relationships']:
                        continue
                    configuration_version = run['relationships']['configuration-version']['data']
                    configuration_version = get_configuration_version(configuration_version['id'])
                    configuration_version_commit = get_configuration_version_commit(configuration_version['id'])
                    if configuration_version and configuration_version_commit and 'sender-username' in configuration_version_commit['attributes']:
                        developers[configuration_version_commit['attributes']['sender-username']] = configuration_version_commit
                        print(f"                Workspace Run: {run_id} Created by Commiter: {configuration_version_commit['attributes']['sender-username']} in {configuration_version['attributes']['source']} ")
            print('        Done (Workspace-Level Runs)')
        print('    Done (Workspaces)')

        print('    Collecting Organization-Level Runs, please wait ...')
        runs = get_organization_runs(organization_id, run_filter)
        for run_id in runs:
            run = runs[run_id]
            if run_id in run_cache:
                # Do not recount runs returned by previous Organization Run API calls.
                continue
            run_cache[run_id] = run_id
            created_at = run['attributes']['created-at']
            if created_at < days_ago:
                # Do not count runs older than the last number of days.
                continue
            if run['attributes']['source'] in ['tfe-ui', 'tfe-ui,tfe-api']:
                if 'created-by' not in run['relationships']:
                    continue
                created_by = run['relationships']['created-by']['data']
                # Validate if this check is necessary.
                if created_by['type'] != 'users':
                    continue
                if created_by['id'] in service_account_cache:
                    user = service_account_cache[created_by['id']]
                elif created_by['id'] in user_cache:
                    user = user_cache[created_by['id']]
                else:
                    user = get_user(created_by['id'])
                    if not user:
                        continue
                if user['attributes']['is-service-account']:
                    service_account_cache[user['id']] = user
                else:
                    user_cache[user['id']] = user
                if user['id'] in member_cache:
                    # Use email from Organization Memberships, when available, to deduplicate across our other active developer scripts.
                    developers[member_cache[user['id']]['attributes']['email']] = user
                    print(f"        Organization Run: {run_id} Created by User: {user['attributes']['username']} ({user['id']} / {member_cache[user['id']]['attributes']['email']}) Service Account: {user['attributes']['is-service-account']}")
                else:
                    developers[user['id']] = user
                    print(f"        Organization Run: {run_id} Created by User: {user['attributes']['username']} ({user['id']}) Service Account: {user['attributes']['is-service-account']}")
            elif run['attributes']['source'] == 'tfe-configuration-version':
                if 'configuration-version' not in run['relationships']:
                    continue
                configuration_version = run['relationships']['configuration-version']['data']
                configuration_version = get_configuration_version(configuration_version['id'])
                configuration_version_commit = get_configuration_version_commit(configuration_version['id'])
                if configuration_version and configuration_version_commit and 'sender-username' in configuration_version_commit['attributes']:
                    developers[configuration_version_commit['attributes']['sender-username']] = configuration_version_commit
                    print(f"        Organization Run: {run_id} Created by Commiter: {configuration_version_commit['attributes']['sender-username']} in {configuration_version['attributes']['source']} ")
        print('    Done (Organization-Level Runs)')
    print()
    print('Done (Organizations)')

    output_results(developers)


####


if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
