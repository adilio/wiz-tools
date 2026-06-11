#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Active Developer Count : Azure DevOps """

# Local status: modified from the Wiz-hosted script.
# Origin: https://downloads.wiz.io/customer-files/scripts/ADO/active-developer-count-ado.py
# Local changes: fixes repeated repository rescans across projects and adds
# progress, pagination, partial output, and guardrails for large organizations.

import argparse
import csv
import datetime
import hashlib
import os
import random
import signal
import sys
import time

from importlib import import_module

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import azure.devops.version
    from azure.devops.connection import Connection
    from msrest.authentication import BasicAuthentication
except ImportError:
    print("\nERROR: Missing required Azure DevOps SDK package. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade azure-devops")
    sys.exit(1)

# Avoid hardcoded import of the GitQueryCommitsCriteria model:
#   from azure.devops.vX_Y.git.models import GitQueryCommitsCriteria
# pylint: disable=c-extension-no-member
azure_devops_version = azure.devops.version.VERSION.split('.')
import_version = f"azure.devops.v{azure_devops_version[0]}_{azure_devops_version[1]}.git.models"
GitQueryCommitsCriteria = import_module(import_version).GitQueryCommitsCriteria


version='2.8.1'


####
# Command Line Arguments
####


parser = argparse.ArgumentParser(description = 'Count Azure DevOps Active Developers')
parser.add_argument(
    '--token',
    help = 'Specify the token to use to access Azure DevOps. Can also be set with ADO_TOKEN.',
    default = os.environ.get('ADO_TOKEN')
)
parser.add_argument(
    '--org', '--organization',
    help = 'Count active developers in the specified Azure DevOps organization (required)',
    required = True
)
parser.add_argument(
    '--proj', '--project',
    help = 'Count active developers in the specified Azure DevOps project (optional)',
    default = None
)
parser.add_argument(
    '--repo', '--repository',
    help = 'Count active developers in the specified Azure DevOps repository name or ID (optional)',
    default = None
)
parser.add_argument(
    '--output-dir',
    help = 'Directory for output files (default: current directory)',
    default = '.'
)
parser.add_argument(
    '--decrypt',
    action = 'store_true',
    help = 'Decrypt email addresses in output files (default: disabled)',
    default = False
)
parser.add_argument(
    '--mask-emails',
    action = 'store_true',
    help = 'Mask developer email addresses in terminal output (default: disabled)',
    default = False
)
parser.add_argument(
    '--days',
    help = 'Count active developers with commits in the last N days (default: 90)',
    type = int,
    default = 90
)
parser.add_argument(
    '--commit-page-size',
    help = 'Commits to request per Azure DevOps API page (default: 1000)',
    type = int,
    default = 1000
)
parser.add_argument(
    '--project-page-size',
    help = 'Projects to request per Azure DevOps API page (default: 100)',
    type = int,
    default = 100
)
parser.add_argument(
    '--max-commits-per-repo',
    help = 'Stop scanning a repository after N commits (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--max-repositories',
    help = 'Stop after scanning N repositories (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--max-run-minutes',
    help = 'Stop scanning after N minutes and write partial results (default: unlimited)',
    type = int,
    default = 0
)
parser.add_argument(
    '--include-disabled',
    action = 'store_true',
    help = 'Scan disabled repositories when Azure DevOps allows it (default: list and skip disabled repositories)',
    default = False
)
parser.add_argument(
    '--include-empty-repositories',
    action = 'store_true',
    help = 'Scan empty repositories when Azure DevOps returns them (default: skip when detectable)',
    default = False
)
parser.add_argument(
    '--progress-interval',
    help = 'Print progress every N repositories (default: 25)',
    type = int,
    default = 25
)
parser.add_argument(
    '--checkpoint-interval',
    help = 'Write partial output every N scanned repositories (default: 0, disabled)',
    type = int,
    default = 0
)
parser.add_argument(
    '--max-retries',
    help = 'Retry attempts for Azure DevOps API calls (default: 5)',
    type = int,
    default = 5
)
parser.add_argument(
    '--retry-delay',
    help = 'Initial retry delay in seconds for Azure DevOps API calls (default: 5)',
    type = int,
    default = 5
)
parser.add_argument(
    '--fail-fast',
    action = 'store_true',
    help = 'Exit on the first Azure DevOps API error (default: log and continue)',
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

if not args.token:
    print("ERROR: --token is required unless ADO_TOKEN is set.")
    sys.exit(1)
if args.days < 1:
    print("ERROR: --days must be at least 1")
    sys.exit(1)
if args.commit_page_size < 1 or args.commit_page_size > 5000:
    print("ERROR: --commit-page-size out of range: [1 .. 5000]")
    sys.exit(1)
if args.project_page_size < 1 or args.project_page_size > 1000:
    print("ERROR: --project-page-size out of range: [1 .. 1000]")
    sys.exit(1)
if args.max_commits_per_repo < 0:
    print("ERROR: --max-commits-per-repo must be 0 or greater")
    sys.exit(1)
if args.max_repositories < 0:
    print("ERROR: --max-repositories must be 0 or greater")
    sys.exit(1)
if args.max_run_minutes < 0:
    print("ERROR: --max-run-minutes must be 0 or greater")
    sys.exit(1)
if args.progress_interval < 1:
    print("ERROR: --progress-interval must be at least 1")
    sys.exit(1)
if args.checkpoint_interval < 0:
    print("ERROR: --checkpoint-interval must be 0 or greater")
    sys.exit(1)
if args.max_retries < 1:
    print("ERROR: --max-retries must be at least 1")
    sys.exit(1)
if args.retry_delay < 1:
    print("ERROR: --retry-delay must be at least 1")
    sys.exit(1)
os.makedirs(args.output_dir, exist_ok=True)


####
# Configuration and Globals
####


output_file    = 'active-developers.txt'
number_of_days = args.days
run_started_at = time.monotonic()

developers_per_repo     = []
developers_across_repos = set()
errors_log              = []
scan_stats              = {
    'projects_seen': 0,
    'projects_scanned': 0,
    'repositories_seen': 0,
    'repositories_scanned': 0,
    'repositories_skipped': 0,
    'repositories_failed': 0,
    'commits_scanned': 0,
    'capped_repositories': 0,
}


####
# Common Library Code
####


def signal_handler(_signal_received, _frame):
    """ Control-C """
    print("\nInterrupted. Writing partial results before exiting.")
    output_results(last_projects, last_repositories, partial=True)
    sys.exit(0)


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details):
    """ Error output """
    print(f"\nERROR: {details}")
    errors_log.append(str(details).replace("\n", " ").replace("\r", " "))
    if args.fail_fast:
        raise RuntimeError(details)


def elapsed_time():
    """ Return elapsed run time """
    elapsed = int(time.monotonic() - run_started_at)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def status_print(message):
    """ Status output """
    print(f"+{elapsed_time()} {message}")


def output_path(file_name):
    """ Return an output path in the configured output directory """
    return os.path.join(args.output_dir, file_name)


def normalize_identifier(value):
    """ Normalize names and IDs for matching """
    return str(value or '').strip().lower()


def normalize_email(value):
    """ Normalize email addresses for counting """
    return str(value or '').strip().strip('"').lower()


def mask_email(email):
    """ Mask email for terminal output unless explicit display is requested """
    if not args.mask_emails:
        return email
    if '@' not in email:
        return '<hidden>'
    local, domain = email.split('@', 1)
    return f"{local[:2]}***@{domain}"


def should_stop_for_runtime():
    """ Return whether the run exceeded the optional runtime budget """
    if not args.max_run_minutes:
        return False
    return (time.monotonic() - run_started_at) >= args.max_run_minutes * 60


def get_org_base_url():
    """ Return an Azure DevOps organization URL from --org """
    org = args.org.strip().rstrip('/')
    if org.startswith('http://') or org.startswith('https://'):
        return org
    return f"https://dev.azure.com/{org}"


def get_org_display_name():
    """ Return a short organization name for output """
    return get_org_base_url().rstrip('/').split('/')[-1]


def days_ago_iso():
    """ Calculate a DateTime a number of days ago """
    dt_now = datetime.datetime.now(datetime.timezone.utc)
    dt_off = datetime.timedelta(days=number_of_days)
    result = (dt_now - dt_off).isoformat()
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
    for file in os.listdir(args.output_dir):
        if file.endswith('-developers.txt') and file != output_file:
            with open(output_path(file), 'r', encoding='utf-8') as developers_file:
                developers.extend(developers_file.read().split())
    # Deduplicate developers.
    developers = sorted(set(developers))
    with open(output_path(output_file), 'w', encoding='utf-8') as developer_file:
        for developer_email in developers:
            # Encrypt sensitive data before writing to disk.
            if not args.decrypt:
                developer_email = hashlib.sha256(developer_email.encode()).hexdigest()
            developer_file.write(f"{developer_email}\n")
    print()
    print(f"- {len(developers)} Total Developers across all Version Control Systems scanned in {args.output_dir}")
    print()
    print(f"To reset the Total Developers count, delete all of the '*-developers.txt' files in {args.output_dir}")


####
# Customized Library Code
####


def get_ado_client():
    """ Get a connection to the Azure DevOps Organization """
    verbose_print("API: Get Core Client")
    try:
        credentials = BasicAuthentication('', args.token)
        connection = Connection(base_url=get_org_base_url(), creds=credentials)
        # Get a client (the "core" client provides access to projects, teams, etc.)
        result = connection.clients.get_core_client()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print(f"Unable to connect to Azure DevOps Organization: {args.org} with the specified token.")
        error_print("Exiting...")
        sys.exit(1)
    return result


def call_ado_api(description, func, *func_args, **func_kwargs):
    """ Call Azure DevOps with retry handling """
    for attempt in range(1, args.max_retries + 1):
        try:
            return func(*func_args, **func_kwargs)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            if attempt >= args.max_retries:
                error_print(f"{description} failed after {attempt} attempt(s): {ex}")
                return None
            sleep_seconds = args.retry_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            status_print(f"[WAIT] {description} failed on attempt {attempt}/{args.max_retries}. Retrying in {sleep_seconds:.1f}s.")
            time.sleep(sleep_seconds)


def get_git_client():
    """ Get a connection to Git in the Azure DevOps Organization """
    verbose_print("API: Get Git Client")
    try:
        credentials = BasicAuthentication('', args.token)
        connection = Connection(base_url=get_org_base_url(), creds=credentials)
        # Get a client (the "git" client provides access to repositories, commits, etc.)
        result = connection.clients.get_git_client()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print(f"Unable to connect to Git in Azure DevOps Organization: {args.org} with the specified token.")
        error_print("You or your token may have insuffient permissions (Permissions: Code (Read)) or scope.")
        error_print("Exiting...")
        sys.exit(1)
    return result


def get_projects(ado_client):
    """ Get Projects in an Organization """
    result = []
    verbose_print("API: Get Projects")
    try:
        skip = 0
        while True:
            projects = call_ado_api(
                f"Get Projects skip={skip}",
                ado_client.get_projects,
                top=args.project_page_size,
                skip=skip
            ) or []
            result.extend(projects)
            if len(projects) < args.project_page_size:
                break
            skip += args.project_page_size
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Unable to get Projects.")
    return result


def get_repositories(git_client, project):
    """ Get Repositories """
    result = []
    verbose_print(f"API: Get Repositories in Project: {project.name}")
    try:
        result = call_ado_api(
            f"Get Repositories in Project: {project.name}",
            git_client.get_repositories,
            project=project.id
        ) or []
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print(f"Unable to get Repositories in Project: {project.name}")
    return result


def iter_commits(git_client, project, repository, repository_scan_state):
    """ Yield commits from a repository """
    verbose_print(f"API: Get Commits in Repository: {repository.name}")
    try:
        criteria = GitQueryCommitsCriteria()
        criteria.from_date = days_ago_iso()
        skip = 0
        yielded = 0
        while True:
            commits = call_ado_api(
                f"Get Commits in Repository: {project.name}/{repository.name} skip={skip}",
                git_client.get_commits,
                repository.id,
                criteria,
                project=project.id,
                skip=skip,
                top=args.commit_page_size
            )
            if commits is None:
                repository_scan_state['failed'] = True
                break
            if not commits:
                break
            for commit in commits:
                if args.max_commits_per_repo and yielded >= args.max_commits_per_repo:
                    if not repository_scan_state['commit_cap_reached']:
                        scan_stats['capped_repositories'] += 1
                        repository_scan_state['commit_cap_reached'] = True
                    return
                yielded += 1
                yield commit
            if args.max_commits_per_repo and yielded >= args.max_commits_per_repo:
                if not repository_scan_state['commit_cap_reached']:
                    scan_stats['capped_repositories'] += 1
                    repository_scan_state['commit_cap_reached'] = True
                status_print(f"[WARN] Commit cap reached for {project.name}/{repository.name}: {args.max_commits_per_repo}")
                break
            if len(commits) < args.commit_page_size:
                break
            skip += args.commit_page_size
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print(f"Unable to get Commits in Repository: {repository.name}")
        repository_scan_state['failed'] = True


def get_active_developers(git_client, project, repository):
    """ Get Active Developers of a Repository """
    repository_active_developers = {}
    commits_scanned = 0
    repository_scan_state = {'commit_cap_reached': False, 'failed': False}

    for commit in iter_commits(git_client, project, repository, repository_scan_state):
        commits_scanned += 1
        scan_stats['commits_scanned'] += 1
        verbose_print(f"Commit: {commit}")

        try:
            commit_author_name  = commit.author.name
            commit_author_email = normalize_email(commit.author.email)
        except Exception:  # pylint: disable=broad-exception-caught
            verbose_print(f"    Skipping Commit, Commit Missing Author Details: {commit}")
            continue
        if not commit_author_email:
            verbose_print(f"    Skipping Commit, Commit Missing Author Email: {commit}")
            continue

        if commit_author_email in repository_active_developers:
            repository_active_developers[commit_author_email]['commits'] += 1
        else:
            repository_active_developers[commit_author_email] = {
                'name':    commit_author_name,
                'commits': 1
            }

    for developer_email, developer in repository_active_developers.items():
        print(f"        Found Developer: {developer['name']} ({mask_email(developer_email)}) with {developer['commits']} Commits")

    if len(repository_active_developers) > 0:
        print(f"        Total {len(repository_active_developers)} Developers in Project: {project.name} in Repository: {repository.name}")
        developers_across_repos.update(repository_active_developers.keys())
    if repository_scan_state['failed']:
        status = "failed"
        scan_stats['repositories_failed'] += 1
    elif repository_scan_state['commit_cap_reached']:
        status = "commit_cap_reached"
    else:
        status = "scanned"
    developers_per_repo.append([get_org_display_name(), project.name, repository.name, len(repository_active_developers), commits_scanned, status])
    return repository_active_developers

# pylint: disable=too-many-locals
def output_results(projects, repositories, partial=False):
    """ Output Results """
    # Developer File
    developers_across_repos_set = sorted(developers_across_repos) # Deduplicate
    developer_file_name = f"azure_devops{slugify([get_org_display_name(), args.proj, args.repo])}-developers.txt"
    with open(output_path(developer_file_name), 'w', encoding='utf-8') as developer_file:
        for developer_email in developers_across_repos_set:
            # Encrypt sensitive data before writing to disk.
            if not args.decrypt:
                developer_email = hashlib.sha256(developer_email.encode()).hexdigest()
            developer_file.write(f"{developer_email}\n")

    # Log File
    developer_log_file_name = f"azure_devops{slugify([get_org_display_name(), args.proj, args.repo])}-developers-log.txt"
    with open(output_path(developer_log_file_name), 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Organization', 'Project', 'Repository', f"Developers (Last {number_of_days} Days)", 'Commits Scanned', 'Status'])
        for item in developers_per_repo:
            csv_writer.writerow(item)

    # Summary
    repository_details = f" in Repository: {args.repo}" if args.repo else f" across {len(repositories)} Repositories"
    project_details = f" in Project: {args.proj}" if args.proj else f" across {len(projects)} Projects"
    organization_details  = f" in Organization: {get_org_display_name()}"
    label = "Partial Results" if partial else "Results"
    print(f"\n{label} (Active Developers in the last {number_of_days} days)\n")
    print(f"- {len(developers_across_repos_set)} Developers{repository_details}{project_details}{organization_details}")
    print(f"- {scan_stats['projects_scanned']} Projects scanned; {scan_stats['repositories_scanned']} Repositories scanned; {scan_stats['repositories_skipped']} Repositories skipped")
    print(f"- {scan_stats['commits_scanned']} Commits scanned")
    if scan_stats['capped_repositories']:
        print(f"- {scan_stats['capped_repositories']} Repositories reached --max-commits-per-repo")
    output_results_across_version_control_systems()
    if errors_log:
        error_log_file_name = f"azure_devops{slugify([get_org_display_name(), args.proj, args.repo])}-errors-log.txt"
        with open(output_path(error_log_file_name), 'w', encoding='utf-8') as error_file:
            for error in errors_log:
                error_file.write(f"{error}\n")
        print(f"\nErrors written to {error_log_file_name}")
    # Sanity check for only public repositories, or no repositories.
    projects_count = 0
    private_projects_count = 0
    for project in projects:
        projects_count +=1
        if getattr(project, 'visibility', '').lower() == 'private':
            private_projects_count +=1
    if projects_count == 0:
        print()
        print("NOTE: No Projects found.")
    if projects_count > 0 and private_projects_count == 0:
        print()
        print("NOTE: No Private Projects found. If unexpected, verify access.")


def project_matches(project):
    """ Return whether a project matches the requested filter """
    if not args.proj:
        return True
    wanted = normalize_identifier(args.proj)
    return wanted in {normalize_identifier(getattr(project, 'name', '')), normalize_identifier(getattr(project, 'id', ''))}


def repository_matches(repository):
    """ Return whether a repository matches the requested filter """
    if not args.repo:
        return True
    wanted = normalize_identifier(args.repo)
    return wanted in {normalize_identifier(getattr(repository, 'name', '')), normalize_identifier(getattr(repository, 'id', ''))}


def repository_is_empty(repository):
    """ Best-effort empty repository detection """
    if args.include_empty_repositories:
        return False
    size = getattr(repository, 'size', None)
    if size is not None and size == 0:
        return True
    return False


####
# Main
####


def main():
    """ Calculon Compute! """
    global last_projects, last_repositories  # pylint: disable=global-statement
    ado_client = get_ado_client()
    git_client = get_git_client()

    project_details = f" Project: {args.proj}" if args.proj else ""
    repository_details = f" Repository: {args.repo}" if args.repo else ""
    status_print(f"[INFO] Scanning Organization: {get_org_display_name()}{project_details}{repository_details}")
    print()
    projects = get_projects(ado_client)
    last_projects = projects
    repositories = []
    repositories_scanned = 0
    for project in projects:
        verbose_print(f"Project: {project}")
        scan_stats['projects_seen'] += 1
        if not project_matches(project):
            continue
        scan_stats['projects_scanned'] += 1
        visibility = str(getattr(project, 'visibility', 'unknown')).title()
        print(f"Found {visibility} Project: {project.name}")
        project_repositories = get_repositories(git_client, project)
        repositories.extend(project_repositories)
        last_repositories = repositories
        for repository in project_repositories:
            verbose_print(f"Repository: {repository}")
            scan_stats['repositories_seen'] += 1
            if not repository_matches(repository):
                continue
            repository_state = "Disabled" if getattr(repository, 'is_disabled', False) else "Enabled"
            print(f"    Found {repository_state} Repository: {repository.name}")
            if getattr(repository, 'is_disabled', False) and not args.include_disabled:
                print(f"        Skipping disabled repository: {repository.name}")
                scan_stats['repositories_skipped'] += 1
                developers_per_repo.append([get_org_display_name(), project.name, repository.name, 0, 0, 'skipped_disabled'])
                continue
            if repository_is_empty(repository):
                print(f"        Skipping empty repository: {repository.name}")
                scan_stats['repositories_skipped'] += 1
                developers_per_repo.append([get_org_display_name(), project.name, repository.name, 0, 0, 'skipped_empty'])
                continue
            if args.max_repositories and repositories_scanned >= args.max_repositories:
                status_print(f"[WARN] Repository cap reached: {args.max_repositories}")
                output_results(projects, repositories, partial=True)
                return
            if should_stop_for_runtime():
                status_print(f"[WARN] Runtime cap reached: {args.max_run_minutes} minute(s).")
                output_results(projects, repositories, partial=True)
                return
            repositories_scanned += 1
            scan_stats['repositories_scanned'] += 1
            if repositories_scanned == 1 or repositories_scanned % args.progress_interval == 0:
                status_print(f"[SCAN] {repositories_scanned} repositories scanned; {len(developers_across_repos)} unique developers found so far.")
            try:
                get_active_developers(git_client, project, repository)
            except Exception as ex:  # pylint: disable=broad-exception-caught
                scan_stats['repositories_failed'] += 1
                error_print(f"Repository scan failed: {project.name}/{repository.name}: {ex}")
                developers_per_repo.append([get_org_display_name(), project.name, repository.name, 0, 0, 'failed'])
            if args.checkpoint_interval and repositories_scanned % args.checkpoint_interval == 0:
                output_results(projects, repositories, partial=True)
    output_results(projects, repositories)


####


if __name__ == "__main__":
    last_projects = []
    last_repositories = []
    signal.signal(signal.SIGINT,signal_handler)
    try:
        main()
    except KeyboardInterrupt:
        signal_handler(None, None)
