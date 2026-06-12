#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Active Developer Count : GitHub / Enterprise """

import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import inspect
import os
import signal
import sys
import time

from functools import wraps

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import github
except ImportError:
    print("\nERROR: Missing required GitHub package. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade PyGithub")
    sys.exit(1)


version='2.8.1'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count GitHub Active Developers')
parser.add_argument(
    '--token',
    help = 'Specify the token to use to access GitHub (required)',
    required = True
)
parser.add_argument(
    '--org', '--organization',
    help = 'Count active developers in the specified GitHub organization (optional)',
    default = None
)
parser.add_argument(
    '--repo', '--repository',
    help = 'Count active developers in the specified GitHub repository (optional)',
    default = None
)
parser.add_argument(
    '--url',
    help = 'Specify the URL to use for GitHub Enterprise, format: https://{HOSTNAME}/api/v3 (optional)',
    default = None
)
parser.add_argument(
    '--decrypt',
    action = 'store_true',
    help = 'Decrypt email addresses in output files (default: disabled)',
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
    help = 'Output verbose information (default: disabled)',
    default = False
)
args = parser.parse_args()


####
# Configuration and Globals
####


output_file    = 'active-developers.txt'
error_log_file = 'github-errors-log.txt'
number_of_days = 90

developers_per_repo     = []
developers_across_repos = []

errors_log = []


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


def error_print(details, repository=''):
    """ Error output """
    repository  = f"Repository: {repository} " if repository else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {repository} {function} {details}\n")
    errors_log.append(f"ERROR: {repository} {function} {details}")


def days_ago():
    """ Calculate a timezone-aware DateTime a number of days ago (UTC) """
    dt_now = datetime.datetime.now(datetime.timezone.utc)
    dt_off = datetime.timedelta(days=number_of_days)
    result = dt_now - dt_off
    verbose_print(f"Days Ago: {number_of_days} is: {result}")
    return result


def slugify(strings):
    """ Convert a list of strings into a safe/valid filename """
    result = ''
    strings = filter(None, strings)
    for string in strings:
        safe_string = ''.join(c for c in string if c.isalnum())
        result += f"-{safe_string}"
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
                developer_email = hashlib.sha256(developer_email.encode()).hexdigest()
            developer_file.write(f"{developer_email}\n")
    print()
    print(f"- {len(developers)} Total Developers across all Version Control Systems scanned in this directory")
    print()
    print("To reset the Total Developers count, delete all of the '*-developers.txt' files in this directory")


####
# Customized Library Code
####


def rate_limited_retry(function):
    """Rate Limit Decorator"""
    @wraps(function)
    def wrapper(*aargs, **kwargs):
        while True:
            try:
                return function(*aargs, **kwargs)
            except github.RateLimitExceededException as e:
                print("Rate limit exceeded. Waiting to retry...")
                reset_timestamp = int(e.headers.get('X-RateLimit-Reset', 0))
                reset_time = datetime.datetime.fromtimestamp(reset_timestamp, tz=datetime.timezone.utc)
                wait_seconds = max((reset_time - datetime.datetime.now(datetime.timezone.utc)).total_seconds() + 1, 1)
                time.sleep(wait_seconds)
            except Exception as e:
                print(f"An unexpected error occurred: {e}")
                raise
    return wrapper


# https://pygithub.readthedocs.io/en/latest/github.html

def get_client():
    """ Get Client """
    verbose_print("API: Get Client")
    #if args.verbose_mode:
    #    github.enable_console_debug_logging()
    try:
        token = github.Auth.Token(args.token)
        if args.url:
            result = github.Github(auth=token, base_url=args.url)
        else:
            result = github.Github(auth=token)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to authenticate. Please verify your token (and URL, if specified).")
        error_print("You or your token may have insuffient permissions or scope.")
        error_print("Exiting...")
        sys.exit(1)
    return result

# https://pygithub.readthedocs.io/en/latest/github.html#github.MainClass.Github.get_user
# Calls: GET /users/{user}

def get_current_user(client):
    """ Get Current User """
    verbose_print("API: Get Current User")
    try:
        result = client.get_user()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to get the Current User with the specified token.")
        error_print("Exiting...")
        sys.exit(1)
    verbose_print(f"Current User: {result}")
    return result

# https://pygithub.readthedocs.io/en/latest/github.html#github.MainClass.Github.get_organization
# Calls GET /orgs/{org}

@rate_limited_retry
def get_organization(client):
    """ Get Organization """
    result = None
    verbose_print(f"API: Get Organization: {args.org}")
    try:
        result = client.get_organization(args.org)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print(f"Unable to get Organization: {args.org}")
        error_print("It may be a User Account rather than an Organization Account.")
        error_print("Your account or token may have insuffient permissions or scope.")
    verbose_print(f"Organization: {result}")
    return result

# https://pygithub.readthedocs.io/en/latest/github_objects/Organization.html#github.Organization.Organization.get_repo
# Calls GET /orgs/{org}/repos
# https://pygithub.readthedocs.io/en/latest/github_objects/AuthenticatedUser.html#github.AuthenticatedUser.AuthenticatedUser.get_repo
# Calls GET /users/{user}/repos

@rate_limited_retry
def get_repository(org_or_user):
    """ Get Repository """
    result = None
    verbose_print(f"API: Get Repository: {args.repo}")
    try:
        result = org_or_user.get_repo(name=args.repo)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, args.repo)
        error_print(f"Unable to get Repository: {args.repo}")
        error_print("Your account or token may have insuffient permissions (Repository Permissions: Metadata Read-Only) or scope.")
        error_print("See: https://docs.github.com/en/rest/repos/repos#get-a-repository--fine-grained-access-tokens")
    verbose_print(f"Repository: {result}")
    return result

# https://pygithub.readthedocs.io/en/latest/github_objects/Organization.html#github.Organization.Organization.get_repos
# Calls GET /orgs/{org}/repos
# https://pygithub.readthedocs.io/en/latest/github_objects/AuthenticatedUser.html#github.AuthenticatedUser.AuthenticatedUser.get_repos
# Calls GET /user/repos

@rate_limited_retry
def get_repositories(org_or_user):
    """ Get Repositories """
    result = []
    verbose_print("API: Get Repositories")
    try:
        result = org_or_user.get_repos(type='all', sort='full_name')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to get Repositories.")
        error_print("Your account or token may have insuffient permissions (Repository Permissions: Metadata Read-Only) or scope.")
        error_print("See: https://docs.github.com/en/rest/repos/repos#get-a-repository--fine-grained-access-tokens")
    return result

# https://pygithub.readthedocs.io/en/latest/github_objects/Repository.html#github.Repository.Repository.get_commits
# Calls GET /repos/{owner}/{repo}/commits

@rate_limited_retry
def get_commits(repository):
    """ Get Commits """
    result = []
    verbose_print(f"API: Get Commits in Repository: {repository.name}")
    try:
        result = repository.get_commits(since=days_ago())
        # Note: get_commits() does not raise an exception upon permissions error, but totalCount does.
        total_count = result.totalCount  # pylint: disable=unused-variable
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, repository.name)
        error_print(f"Unable to get Commits in Repository: {repository.name}")
        error_print("Your account or token may have insuffient permissions (Repository Permissions: Contents Read-Only) or scope.")
        error_print("See: https://docs.github.com/en/rest/commits/commits#list-commits--fine-grained-access-tokens")
        result = []
    return result

# Requires read:org permissions, and the user must have push access to the repository to use this endpoint.
# See also: repository.has_in_collaborators(id)
# https://docs.github.com/en/rest/collaborators/collaborators?apiVersion=2022-11-28#list-repository-collaborators

@rate_limited_retry
def get_collaborators(repository):
    """ Get Collaborators of a Project """
    result = []
    verbose_print(f"API: Get Collaborators of Repository: {repository.name}")
    try:
        result = repository.get_collaborators()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, repository.name)
        error_print(f"Unable to get Collaborators of Repository: {repository.name}")
        error_print("Your account or token may have insuffient permissions (Repository Permissions: Contents Read-Only) or scope.")
        error_print("See: https://docs.github.com/en/rest/commits/commits#list-commits--fine-grained-access-tokens")
    return result


# pylint: disable=too-many-locals,too-many-statements
def get_active_developers(repository):
    """ Get Active Developers of a Repository """
    verbose_print(f"Repository: {repository}")
    visibility = "Private" if repository.private else "Public"
    print(f"Found {visibility} Repository: {repository.full_name}")

    repository_collaborators = {}
    repository_active_developers = {}
    exported_repository_developers = []

    collaborators = get_collaborators(repository)
    try:
        for collaborator in collaborators:
            repository_collaborators[collaborator.id] = {'id': collaborator.id, 'login': collaborator.login}
        org_access = True
    except Exception:  # pylint: disable=broad-exception-caught
        org_access = False
        print("    Unable to get Collaborators of Repository\n    Your account or token may have insuffient permissions (read:org).\n    Not checking Developers for Organization Membership.\n")

    commits = get_commits(repository)
    for commit in commits:
        verbose_print(f"Commit: {commit}")
        try:
            committer_id    = commit.author.id
            committer_name  = commit.commit.author.name
            committer_email = str(commit.commit.author.email).strip('"')
        except Exception:  # pylint: disable=broad-exception-caught
            verbose_print(f"    Skipping Commit, Commit Missing Author Details: {commit}")
            continue
        verbose_print(f"Commit Author: ({committer_id}) {committer_name} - {committer_email}")
        # Public Email:  Commit Author: (id) firstname lastname - firstname.lastname@wiz.io
        # NoReply Email: Commit Author: (id) firstname lastname - id+username@users.noreply.github.com
        #
        # Format for NoReply Email before July 18, 2017 is: USERNAME@users.noreply.github.com
        # Format for NoReply Email after  July 18, 2017 is: ID+USERNAME@users.noreply.github.com

        if org_access is True and committer_id not in repository_collaborators:
            print(f"    Skipping Commit, Commit Author not Collaborator in Repository: {committer_name} - {committer_email} ")
            continue

        if committer_id in repository_active_developers:
            repository_active_developers[committer_id]['commits'] += 1
            if committer_email in repository_active_developers[committer_id]['emails']:
                repository_active_developers[committer_id]['emails'][committer_email] += 1
            else:
                repository_active_developers[committer_id]['emails'][committer_email] = 1
        else:
            repository_active_developers[committer_id] = {
                'id':      committer_id,
                'name':    committer_name,
                'emails':  {committer_email: 1},
                'commits': 1
            }

    for developer_id, developer in repository_active_developers.items():
        developer_email_addresses = list(developer['emails'].keys())
        print(f"    Found Developer: ({developer_id}) {developer['name']} using {developer_email_addresses} with {developer['commits']} Commits")
        # Avoid populating developers_across_repos with @users.noreply emails addresses, unless its the only email address.
        # As @users.noreply email addresses are inherently unique to GitHub.
        if len(developer['emails']) == 1:
            # If there is only one email address use that address.
            developer_email_address = next(iter(developer['emails']))
            exported_repository_developers.append(developer_email_address)
        else:
            # Filter out @users.noreply email addresses.
            filtered_developer_email_addresses = {}
            for developer_email_address in developer['emails']:
                if 'users.noreply' not in developer_email_address:
                    filtered_developer_email_addresses[developer_email_address] = developer['emails'][developer_email_address]
            if len(filtered_developer_email_addresses) == 0:
                # If there are no filtered email addresses, use the most active address from the unfiltered list of addresses.
                developer_email_address_with_most_commits = max(developer['emails'], key=developer['emails'].get)
            else:
                # Use the most active address from the filtered list of addresses.
                developer_email_address_with_most_commits = max(filtered_developer_email_addresses, key=filtered_developer_email_addresses.get)
            exported_repository_developers.append(developer_email_address_with_most_commits)

    if len(repository_active_developers) > 0:
        print(f"    Total {len(repository_active_developers)} Developers in Repository: {repository.full_name}")
    developers_across_repos.extend(exported_repository_developers)
    developers_per_repo.append([args.org, repository.full_name, len(repository_active_developers)])


def output_results(repositories):
    """ Output Results """
    # Developer File
    developers_across_repos_set = sorted(set(developers_across_repos)) # Deduplicate
    developer_file_name = f"github{slugify([args.org, args.repo])}-developers.txt"
    with open(developer_file_name, 'w', encoding='utf-8') as developer_file:
        for developer_email in developers_across_repos_set:
            # Encrypt sensitive data before writing to disk.
            if not args.decrypt:
                developer_email = hashlib.sha256(developer_email.encode()).hexdigest()
            developer_file.write(f"{developer_email}\n")
    # Log File
    developer_log_file_name = f"github{slugify([args.org, args.repo])}-developers-log.txt"
    with open(developer_log_file_name, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Organization', 'Repository', f"Developers (Last {number_of_days} Days)"])
        for item in developers_per_repo:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(error_log_file, 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    repository_details = f" in Repository: {args.repo}" if args.repo else f" across {repositories.totalCount} Repositories"
    organization_details = f" in Organization: {args.org}" if args.org else ""
    print(f"\nResults (Active Developers in the last {number_of_days} days)\n")
    print(f"- {len(developers_across_repos_set)} Developers{repository_details}{organization_details}")
    output_results_across_version_control_systems()
    # Sanity check for only public repositories, or no repositories.
    repositories_count = 0
    private_repositories_count = 0
    for repository in repositories:
        repositories_count +=1
        if repository.private:
            private_repositories_count +=1
    if repositories_count == 0:
        print()
        print("NOTE: No Repositories found.")
    if repositories_count > 0 and private_repositories_count == 0:
        print()
        print("NOTE: No Private Repositories found. If unexpected, verify access.")
    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


####
# Main
####

# https://pygithub.readthedocs.io/en/latest/github.html

def main():
    """ Calculon Compute! """
    client = get_client()
    repository_details = f" Repository: {args.repo}" if args.repo else ""
    organization_details = f" in Organization: {args.org}" if args.org else ""
    print(f"Scanning{repository_details}{organization_details}")
    print()
    if args.org:
        organization = get_organization(client)
        if not organization:
            print("Exiting...")
            sys.exit(1)
        if args.repo:
            repository = get_repository(organization)
            if not repository:
                print("Exiting...")
                sys.exit(1)
            repositories = [repository]
        else:
            repositories = get_repositories(organization)
    else:
        user = get_current_user(client)
        if args.repo:
            repository = get_repository(user)
            if not repository:
                print("Exiting...")
                sys.exit(1)
            repositories = [repository]
        else:
            repositories = get_repositories(user)
    if args.debug_mode:
        for repository in repositories:
            get_active_developers(repository)
    else:
        futures = {}
        failed_tasks = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            for repository in repositories:
                futures[executor.submit(get_active_developers, repository)] = repository.full_name
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                failed_tasks += 1
                error_print(future.exception(), f"repo={futures[future]}")
        if failed_tasks:
            error_print(f"{failed_tasks} repository task(s) failed")
    output_results(repositories)


####


if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
