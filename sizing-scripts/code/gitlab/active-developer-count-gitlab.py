#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Active Developer Count : GitLab / Server """

import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import inspect
import os
import signal
import sys

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import gitlab
except ImportError:
    print("\nERROR: Missing required GitLab package. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade python-gitlab")
    sys.exit(1)


version='2.8.1'


####
# Command Line Arguments
####


DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)

parser = argparse.ArgumentParser(description = 'Count GitLab Active Developers')
parser.add_argument(
    '--token',
    help = 'Specify the token to use to access GitLab (required)',
    required = True
)
pgroup = parser.add_mutually_exclusive_group()
pgroup.add_argument(
    '--group',
    help = 'Count active developers in the specified GitLab group (optional)',
    default = None
)
pgroup.add_argument(
    '--proj', '--project',
    help = 'Count active developers in the specified GitLab project (optional)',
    default = None
)
parser.add_argument(
    '--url',
    help = 'Specify the URL to use for GitLab Server, format: https://{HOSTNAME} (optional)',
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
    help = 'Output verbose debugging information (default: disabled)',
    default = False
)
args = parser.parse_args()


####
# Configuration and Globals
####


output_file    = 'active-developers.txt'
error_log_file = 'gitlab-errors-log.txt'
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


def error_print(details, project=''):
    """ Error output """
    project  = f"Project/Group: {project} " if project else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {project} {function} {details}\n")
    errors_log.append(f"ERROR: {project} {function} {details}")


def days_ago():
    """ Calculate a DateTime a number of days ago """
    dt_now = datetime.datetime.now()
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


# https://python-gitlab.readthedocs.io/en/stable/api-usage-advanced.html#rate-limits
#
# python-gitlab obeys the rate limit of the GitLab server by default.
# On receiving a 429 response (Too Many Requests), python-gitlab sleeps for the amount of time in the Retry-After header that GitLab sends back.
# If GitLab does not return a response with the Retry-After header, python-gitlab will perform an exponential backoff.

# https://python-gitlab.readthedocs.io/en/stable/api-usage.html#pagination
#
# The python-gitlab list() methods return a generator object when passing the argument iterator=True
# But you cannot iterate over the generator object more than once, which this script does.


class GitLabProject():  # pylint: disable=too-few-public-methods
    """ Sparse copy of a python-gitlab Project object """
    def __init__(self, name_with_namespace, visibility):
        self.name_with_namespace = name_with_namespace
        self.visibility          = visibility

# https://python-gitlab.readthedocs.io/en/stable/index.html
# https://docs.gitlab.com/ee/api/rest/

def get_client():
    """ Get Client """
    result = None
    verbose_print("API: Get Client")
    try:
        if args.url:
            result = gitlab.Gitlab(private_token=args.token, url=args.url)
        else:
            result = gitlab.Gitlab(private_token=args.token)
        #if args.verbose_mode:
        #    result.enable_debug()
        result.auth()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to authenticate. Please verify your token (and URL, if specified).")
        error_print("You or your token may have insuffient permissions or scope.")
        error_print("Exiting...")
        sys.exit(1)
    return result

# https://python-gitlab.readthedocs.io/en/stable/gl_objects/users.html#current-user
# https://docs.gitlab.com/ee/api/users.html

def get_current_user(client):
    """ Get Current User """
    result = None
    verbose_print("API: Get Current User")
    try:
        result = client.user()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to get the Current User with the specified token.")
        error_print("Exiting...")
        sys.exit(1)
    verbose_print(f"Current User: {result}")
    return result


def get_user(client, user_id):
    """ Get User """
    result = None
    verbose_print("API: Get User")
    try:
        result = client.users.get(id=user_id)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    verbose_print(f"User: {result}")
    return result


def get_users(client):
    """ Get User """
    result = []
    verbose_print("API: Get User")
    try:
        result = client.users.list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
    verbose_print(f"Users: {result}")
    return result

# https://python-gitlab.readthedocs.io/en/stable/gl_objects/groups.html
# https://docs.gitlab.com/ee/api/groups.html

def get_group(client):
    """ Get Group """
    result = None
    verbose_print(f"API: Get Group: {args.group}")
    try:
        if args.group.isdigit():
            result = client.groups.get(args.group)
        else:
            result = client.groups.list(search=args.group, all_available=True, page=1, per_page=1)[0]
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, args.group)
        error_print(f"Unable to get Group: {args.group}")
        error_print("Your account or token may have insuffient permissions (read_user, read_api) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
        return None
    verbose_print(f"Group: {result}")
    return result

# https://python-gitlab.readthedocs.io/en/stable/gl_objects/projects.html
# https://docs.gitlab.com/ee/api/projects.html

def get_project(client, project_id_or_name):
    """ Get Project """
    result = None
    verbose_print(f"API: Get Project: {project_id_or_name}")
    try:
        if project_id_or_name.isdigit():
            result = client.projects.get(project_id_or_name)
        else:
            result = client.projects.list(search=project_id_or_name, archived=False, order_by='path', sort='asc', page=1, per_page=1)[0]
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id_or_name)
        error_print(f"Unable to get Project: {project_id_or_name}")
        error_print("Your account or token may have insuffient permissions (read_user, read_api) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
        return None
    verbose_print(f"Project: {result}")
    return result


def get_projects(client):
    """ Get Projects """
    result = []
    verbose_print("API: Get Projects")
    try:
        if args.url:
            result = client.projects.list(archived=False, membership=False, order_by='path', sort='asc', iterator=True)
        else:
            result = client.projects.list(archived=False, membership=True,  order_by='path', sort='asc', iterator=True)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to get Projects.")
        error_print("Your account or token may have insuffient permissions (read_user, read_api) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
    verbose_print(f"Projects: {result}")
    return result


def get_group_projects(group):
    """ Get Projects """
    result = []
    verbose_print(f"API: Get Projects in Group: {group.name}")
    try:
        result = group.projects.list(archived=False, include_subgroups=True, order_by='path', sort='asc', iterator=True)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, group.name)
        error_print(f"Unable to get Projects for Group: {group.name}")
        error_print("Your account or token may have insuffient permissions (read_user, read_api) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
    verbose_print(f"Group Projects: {result}")
    return result

# https://python-gitlab.readthedocs.io/en/stable/gl_objects/commits.html
# https://docs.gitlab.com/ee/api/commits.html


def get_commits(project):
    """ Get Commits """
    result = []
    verbose_print(f"API: Get Commits in Project: {project.name}")
    try:
        result = project.commits.list(since=days_ago(), iterator=True)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project.name)
        error_print(f"Unable to get Commits in Project: {project.name}")
        error_print("Your account or token may have insuffient permissions (read_repository) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
    return result


def get_members(project):
    """ Get Members of a Project """
    result = []
    verbose_print(f"API: Get Members of Project: {project.name}")
    try:
        result = project.members.list(all=True)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project.name)
        error_print(f"Unable to get Members of Project: {project.name}")
        error_print("Your account or token may have insuffient permissions (read_repository) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
    verbose_print(f"Project Members: {result}")
    return result


def get_members_all(project):
    """ Get All Members of a Project (including inherited members through ancestor groups)"""
    result = []
    verbose_print(f"API: Get All Members of Project: {project.name}")
    try:
        result = project.members_all.list(get_all=True)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project.name)
        error_print(f"Unable to get all Members (including inherited members through ancestors) of Project: {project.name}")
        error_print("Your account or token may have insuffient permissions (read_repository) or scope.")
        error_print("See: https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html#personal-access-token-scopes")
    verbose_print(f"Project Members: {result}")
    return result


# A committer is defined by committer_name and committer_email.
#   committer_name is not unique within a project.
#   committer_email is not returned by project members.
# So there is a gap here.
# Until we address that, identify developers by committer_email, with the potential for overcounting when a developer uses multiple commit email addresses.
# The alternative is to identify developers by committer_name, with the potential for undercounting when multiple developers have the same full name.


# pylint: disable=too-many-locals
def get_active_developers(project):
    """ Get Active Developers of a Project """
    verbose_print(f"Project: {project}")
    print(f"Found {project.visibility.title()} Project: {project.path_with_namespace} ({project.id})")

    repository_active_developers = {}
    exported_repository_developers = []

    members = get_members_all(project)
    project_members = {}
    for member in members:
        project_members[member.name] = {'id': member.id, 'name': member.name, 'username': member.username, 'state': member.state, 'membership_state': member.membership_state}

    commits = get_commits(project)
    for commit in commits:
        verbose_print(f"Commit: {commit}")
        try:
            committer_name   = commit.committer_name
            committer_email  = str(commit.committer_email).strip('"')
        except Exception:  # pylint: disable=broad-exception-caught
            verbose_print(f"    Skipping Commit, Missing Committer Details: {commit}")
            continue
        verbose_print(f"Commit Committer: {committer_name} - {committer_email} ")
        # Public Email:  Commit Committer: firstname lastname - firstname.lastname@wiz.io
        # NoReply Email: Commit Committer: firstname lastname - id-username@users.noreply.gitlab.com

        if committer_name not in project_members:
            print(f"    Skipping Commit, Committer not Member of Project: {committer_name} - {committer_email}")
            continue
        if project_members[committer_name]['state'] != 'active':
            print(f"    Skipping Commit, Committer not Active: {committer_name} - {committer_email}")
            continue

        if committer_email in repository_active_developers:
            repository_active_developers[committer_email]['commits'] += 1
        else:
            repository_active_developers[committer_email] = {
                'name':    committer_name,
                'commits': 1
            }

    for developer_email, developer in repository_active_developers.items():
        print(f"    Found Developer: ({developer_email}) Name: ({developer['name']}) with {developer['commits']} Commits")
        exported_repository_developers.append(developer_email)

    if len(repository_active_developers) > 0:
        print(f"    Total {len(repository_active_developers)} Developers in Project: {project.name_with_namespace}")
    developers_across_repos.extend(exported_repository_developers)
    developers_per_repo.append([args.group, project.name_with_namespace, len(repository_active_developers)])
    return repository_active_developers


def output_results(gitlab_projects):
    """ Output Results """
    # Developer File
    developers_across_repos_set = sorted(set(developers_across_repos)) # Deduplicate
    developer_file_name = f"gitlab{slugify([args.group, args.proj])}-developers.txt"
    with open(developer_file_name, 'w', encoding='utf-8') as developer_file:
        for developer_email in developers_across_repos_set:
            # Encrypt sensitive data before writing to disk.
            if not args.decrypt:
                developer_email = hashlib.sha256(developer_email.encode()).hexdigest()
            developer_file.write(f"{developer_email}\n")
    # Log File
    developer_log_file_name = f"gitlab{slugify([args.group, args.proj])}-developers-log.txt"
    with open(developer_log_file_name, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Group', 'Project', f"Developers (Last {number_of_days} Days)"])
        for item in developers_per_repo:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(error_log_file, 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    project_details = f" in Project: {args.proj}" if args.proj else f" across {len(gitlab_projects)} Projects"
    group_details   = f" in Group: {args.group}" if args.group else ""
    print(f"\nResults (Active Developers in the last {number_of_days} days)\n")
    print(f"- {len(developers_across_repos_set)} Developers{project_details}{group_details}")
    output_results_across_version_control_systems()
    # Sanity check for only public repositories, or no repositories.
    projects_count = 0
    private_projects_count = 0
    for project in gitlab_projects:
        projects_count +=1
        if project.visibility != 'public':
            private_projects_count +=1
    if projects_count == 0:
        print()
        print("NOTE: No Projects found.")
    if projects_count > 0 and private_projects_count == 0:
        print()
        print("NOTE: No Private Projects found. If unexpected, verify access.")
    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")


####
# Main
####


def main():
    """ Calculon Compute! """
    client = get_client()
    project_details = f" Project: {args.proj}" if args.proj else ""
    group_details = f" in Group: {args.group}" if args.group else ""
    print(f"Scanning{project_details}{group_details}")
    print()
    if args.group:
        group = get_group(client)
        if not group:
            print("Exiting...")
            sys.exit(1)
        projects = get_group_projects(group)
    else:
        if args.proj:
            project = get_project(client, args.proj)
            if not project:
                print("Exiting...")
                sys.exit(1)
            projects = [project]
        else:
            projects = get_projects(client)
    # The python-gitlab list() methods return a generator object when passing the argument iterator=True
    # But you cannot iterate over the generator object more than once, which this script does with projects in output_results().
    # Copy projects to gitlab_projects to allow for iteration more than once.
    gitlab_projects = []
    if args.debug_mode:
        for project in projects:
            gitlab_projects.append(GitLabProject(name_with_namespace=project.name_with_namespace, visibility=project.visibility))
            if args.group:
                get_active_developers(client.projects.get(project.id))
            else:
                get_active_developers(project)
    else:
        futures = {}
        failed_tasks = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            for project in projects:
                gitlab_projects.append(GitLabProject(name_with_namespace=project.name_with_namespace, visibility=project.visibility))
                if args.group:
                    p = client.projects.get(project.id)
                    futures[executor.submit(get_active_developers, p)] = project.name_with_namespace
                else:
                    futures[executor.submit(get_active_developers, project)] = project.name_with_namespace
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                failed_tasks += 1
                error_print(future.exception(), f"project={futures[future]}")
        if failed_tasks:
            error_print(f"{failed_tasks} project task(s) failed")
    output_results(gitlab_projects)


####


if __name__ == "__main__":
    signal.signal(signal.SIGINT,signal_handler)
    main()
