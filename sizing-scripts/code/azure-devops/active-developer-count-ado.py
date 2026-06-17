#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Active Developer Count : Azure DevOps """

# Local status: modified from the Wiz-hosted script.
# Origin: https://downloads.wiz.io/customer-files/scripts/ADO/active-developer-count-ado.py
# Local changes: fixes repeated repository rescans across projects and adds
# progress, pagination, smarter retry handling, partial output, and guardrails
# for large organizations.

import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import os
import random
import signal
import sys
import threading
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
DEFAULT_MAX_WORKERS = min(8, (os.cpu_count() or 1) + 4)


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
    '--show-developers',
    action = 'store_true',
    help = 'Print each discovered developer to the terminal (default: disabled for faster Cloud Shell runs)',
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
    '--max-workers',
    help = f'Maximum repositories to scan concurrently (default: {DEFAULT_MAX_WORKERS}, use 1 for sequential)',
    type = int,
    default = DEFAULT_MAX_WORKERS
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
    '--commit-max-retries',
    help = 'Retry attempts for commit page API calls (default: 2)',
    type = int,
    default = 2
)
parser.add_argument(
    '--retry-delay',
    help = 'Initial retry delay in seconds for Azure DevOps API calls (default: 5)',
    type = int,
    default = 5
)
parser.add_argument(
    '--max-retry-delay',
    help = 'Maximum retry delay in seconds between Azure DevOps API calls (default: 15)',
    type = int,
    default = 15
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
if args.max_workers < 1 or args.max_workers > 64:
    print("ERROR: --max-workers out of range: [1 .. 64]")
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
if args.commit_max_retries < 1:
    print("ERROR: --commit-max-retries must be at least 1")
    sys.exit(1)
if args.retry_delay < 1:
    print("ERROR: --retry-delay must be at least 1")
    sys.exit(1)
if args.max_retry_delay < 1:
    print("ERROR: --max-retry-delay must be at least 1")
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
data_lock               = threading.Lock()
print_lock              = threading.Lock()
thread_local            = threading.local()
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


def error_print(details, context=''):
    """ Error output """
    context = f"{context}: " if context else ""
    message = f"+{elapsed_time()} ERROR: {context}{format_exception(details)}"
    console_print(f"\n{message}")
    append_error(message)
    if args.fail_fast:
        raise RuntimeError(f"{context}{details}")


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
    with print_lock:
        print(f"+{elapsed_time()} {message}")


def console_print(message=''):
    """ Thread-safe terminal output """
    with print_lock:
        print(message)


def append_error(message):
    """ Append an error safely across worker threads """
    with data_lock:
        errors_log.append(message)


def append_repo_log(row):
    """ Append a repository log row safely across worker threads """
    with data_lock:
        developers_per_repo.append(row)


def add_developers(developers):
    """ Add developers to the global set safely across worker threads """
    with data_lock:
        developers_across_repos.update(developers)


def increment_stat(name, amount=1):
    """ Increment a scan stat safely across worker threads """
    with data_lock:
        scan_stats[name] += amount


def get_developer_count():
    """ Return current unique developer count safely across worker threads """
    with data_lock:
        return len(developers_across_repos)


def format_exception(details):
    """ Return a compact exception description for terminal and log output """
    if isinstance(details, str):
        return details.replace("\n", " ").replace("\r", " ")
    try:
        error_type = type(details).__name__
        detail_text = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        return "Error"
    status_code = get_exception_status_code(details)
    status_text = f" status={status_code}" if status_code else ""
    return f"{error_type}{status_text}: {detail_text}"


def get_exception_status_code(details):
    """ Best-effort extraction of HTTP status code from Azure DevOps SDK errors """
    for attr_name in ('status_code', 'status'):
        status_code = getattr(details, attr_name, None)
        if status_code:
            return str(status_code)
    response = getattr(details, 'response', None)
    if response is not None:
        status_code = getattr(response, 'status_code', None) or getattr(response, 'status', None)
        if status_code:
            return str(status_code)
    return ''


def is_retryable_ado_exception(details):
    """ Return whether an Azure DevOps error is likely transient """
    status_code = get_exception_status_code(details)
    if status_code:
        return status_code in {'408', '409', '429', '500', '502', '503', '504'}
    detail_text = str(details).lower()
    transient_patterns = [
        'timeout',
        'timed out',
        'temporarily',
        'too many requests',
        'rate limit',
        'connection reset',
        'connection aborted',
        'service unavailable',
    ]
    non_retryable_patterns = [
        'unauthorized',
        'forbidden',
        'not found',
        'does not exist',
        'empty',
        'no default branch',
        'tf401019',
        'tf401180',
        'vs403403',
    ]
    if any(pattern in detail_text for pattern in non_retryable_patterns):
        return False
    return any(pattern in detail_text for pattern in transient_patterns)


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


def call_ado_api(description, func, *func_args, max_attempts=None, error_state=None, **func_kwargs):
    """ Call Azure DevOps with retry handling """
    max_attempts = max_attempts or args.max_retries
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*func_args, **func_kwargs)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_summary = format_exception(ex)
            if error_state is not None:
                error_state['error'] = error_summary
            if not is_retryable_ado_exception(ex):
                error_print(ex, f"{description} failed with non-retryable error")
                return None
            if attempt >= max_attempts:
                error_print(ex, f"{description} failed after {attempt} retryable attempt(s)")
                return None
            sleep_seconds = min(args.max_retry_delay, args.retry_delay * (2 ** (attempt - 1))) + random.uniform(0, 1)
            status_print(f"[WAIT] {description} failed on attempt {attempt}/{max_attempts}: {error_summary}. Retrying in {sleep_seconds:.1f}s.")
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


def get_thread_git_client():
    """ Get a per-thread Git client for concurrent repository scans """
    if not hasattr(thread_local, 'git_client'):
        thread_local.git_client = get_git_client()
    return thread_local.git_client


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
        error_print(ex, "Unable to get Projects")
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
        error_print(ex, f"Unable to get Repositories in Project: {project.name}")
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
                max_attempts=args.commit_max_retries,
                error_state=repository_scan_state,
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
                        increment_stat('capped_repositories')
                        repository_scan_state['commit_cap_reached'] = True
                    return
                yielded += 1
                yield commit
            if args.max_commits_per_repo and yielded >= args.max_commits_per_repo:
                if not repository_scan_state['commit_cap_reached']:
                    increment_stat('capped_repositories')
                    repository_scan_state['commit_cap_reached'] = True
                status_print(f"[WARN] Commit cap reached for {project.name}/{repository.name}: {args.max_commits_per_repo}")
                break
            if len(commits) < args.commit_page_size:
                break
            skip += args.commit_page_size
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, f"Unable to get Commits in Repository: {project.name}/{repository.name}")
        repository_scan_state['failed'] = True


def get_active_developers(git_client, project, repository):
    """ Get Active Developers of a Repository """
    repository_active_developers = {}
    commits_scanned = 0
    repository_scan_state = {'commit_cap_reached': False, 'failed': False, 'error': ''}

    for commit in iter_commits(git_client, project, repository, repository_scan_state):
        commits_scanned += 1
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

    if args.show_developers:
        for developer_email, developer in repository_active_developers.items():
            console_print(f"        Found Developer: {developer['name']} ({mask_email(developer_email)}) with {developer['commits']} Commits")

    if len(repository_active_developers) > 0:
        console_print(f"        Total {len(repository_active_developers)} Developers in Project: {project.name} in Repository: {repository.name}")
        add_developers(repository_active_developers.keys())
    if repository_scan_state['failed']:
        status = "failed"
        increment_stat('repositories_failed')
    elif repository_scan_state['commit_cap_reached']:
        status = "commit_cap_reached"
    else:
        status = "scanned"
    increment_stat('commits_scanned', commits_scanned)
    append_repo_log([get_org_display_name(), project.name, repository.name, len(repository_active_developers), commits_scanned, status, repository_scan_state['error']])
    return repository_active_developers

# pylint: disable=too-many-locals
def output_results(projects, repositories, partial=False):
    """ Output Results """
    with data_lock:
        developers_across_repos_set = sorted(developers_across_repos)
        developers_per_repo_snapshot = list(developers_per_repo)
        errors_log_snapshot = list(errors_log)
        scan_stats_snapshot = dict(scan_stats)
    # Developer File
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
        csv_writer.writerow(['Organization', 'Project', 'Repository', f"Developers (Last {number_of_days} Days)", 'Commits Scanned', 'Status', 'Error'])
        for item in developers_per_repo_snapshot:
            csv_writer.writerow(item)

    # Summary
    repository_details = f" in Repository: {args.repo}" if args.repo else f" across {len(repositories)} Repositories"
    project_details = f" in Project: {args.proj}" if args.proj else f" across {len(projects)} Projects"
    organization_details  = f" in Organization: {get_org_display_name()}"
    label = "Partial Results" if partial else "Results"
    print(f"\n{label} (Active Developers in the last {number_of_days} days)\n")
    print(f"- {len(developers_across_repos_set)} Developers{repository_details}{project_details}{organization_details}")
    print(f"- {scan_stats_snapshot['projects_scanned']} Projects scanned; {scan_stats_snapshot['repositories_scanned']} Repositories scanned; {scan_stats_snapshot['repositories_skipped']} Repositories skipped")
    print(f"- {scan_stats_snapshot['commits_scanned']} Commits scanned")
    if scan_stats_snapshot['capped_repositories']:
        print(f"- {scan_stats_snapshot['capped_repositories']} Repositories reached --max-commits-per-repo")
    output_results_across_version_control_systems()
    if errors_log_snapshot:
        error_log_file_name = f"azure_devops{slugify([get_org_display_name(), args.proj, args.repo])}-errors-log.txt"
        with open(output_path(error_log_file_name), 'w', encoding='utf-8') as error_file:
            for error in errors_log_snapshot:
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


def print_pre_scan_summary(candidate_repositories):
    """ Print rough scan size before commit-level scanning starts """
    print()
    print("Pre-scan summary")
    print(f"- {scan_stats['projects_scanned']} matching Projects inspected")
    print(f"- {scan_stats['repositories_seen']} Repositories found")
    print(f"- {len(candidate_repositories)} Repositories queued for commit scanning")
    print(f"- {scan_stats['repositories_skipped']} Repositories skipped before commit scanning")
    print(f"- Parallelism: up to {args.max_workers} repositories at a time")
    print(f"- Commit page size: up to {args.commit_page_size} commits per API call")
    if args.max_commits_per_repo:
        max_pages = (args.max_commits_per_repo + args.commit_page_size - 1) // args.commit_page_size
        print(f"- Commit cap: up to {args.max_commits_per_repo} commits per repository, about {max_pages} page(s) per repository")
    else:
        print("- Commit depth: uncapped; large active repositories may require multiple commit pages")
    print("- This is a scan-size estimate, not an active developer count.")
    print()


def scan_repository(project, repository):
    """ Scan one repository for active developers """
    try:
        return get_active_developers(get_thread_git_client(), project, repository)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        increment_stat('repositories_failed')
        error_print(ex, f"Repository scan failed: {project.name}/{repository.name}")
        append_repo_log([get_org_display_name(), project.name, repository.name, 0, 0, 'failed', format_exception(ex)])
        return {}


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
    candidate_repositories = []
    repository_cap_reached = False
    for project in projects:
        if repository_cap_reached:
            break
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
                append_repo_log([get_org_display_name(), project.name, repository.name, 0, 0, 'skipped_disabled', ''])
                continue
            if repository_is_empty(repository):
                print(f"        Skipping empty repository: {repository.name}")
                scan_stats['repositories_skipped'] += 1
                append_repo_log([get_org_display_name(), project.name, repository.name, 0, 0, 'skipped_empty', ''])
                continue
            if args.max_repositories and len(candidate_repositories) >= args.max_repositories:
                status_print(f"[WARN] Repository cap reached: {args.max_repositories}")
                repository_cap_reached = True
                break
            candidate_repositories.append((project, repository))

    print_pre_scan_summary(candidate_repositories)
    if not candidate_repositories:
        output_results(projects, repositories)
        return

    completed_count = 0
    next_repository_index = 0
    futures = {}

    def submit_next(executor):
        nonlocal next_repository_index
        if next_repository_index >= len(candidate_repositories):
            return False
        if should_stop_for_runtime():
            return False
        project, repository = candidate_repositories[next_repository_index]
        next_repository_index += 1
        increment_stat('repositories_scanned')
        future = executor.submit(scan_repository, project, repository)
        futures[future] = (project, repository)
        return True

    status_print(f"[SCAN] Starting commit scan for {len(candidate_repositories)} repositories")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        while len(futures) < args.max_workers and submit_next(executor):
            pass
        while futures:
            done, _not_done = concurrent.futures.wait(
                futures,
                timeout=1,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                if should_stop_for_runtime():
                    status_print(f"[WARN] Runtime cap reached: {args.max_run_minutes} minute(s). Cancelling queued work.")
                    for future in futures:
                        future.cancel()
                    output_results(projects, repositories, partial=True)
                    return
                continue
            for future in done:
                project, repository = futures.pop(future)
                completed_count += 1
                if future.cancelled():
                    append_repo_log([get_org_display_name(), project.name, repository.name, 0, 0, 'cancelled', 'Runtime cap reached'])
                    continue
                try:
                    future.result()
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    increment_stat('repositories_failed')
                    error_print(ex, f"Repository scan failed: {project.name}/{repository.name}")
                    append_repo_log([get_org_display_name(), project.name, repository.name, 0, 0, 'failed', format_exception(ex)])
                if completed_count == 1 or completed_count % args.progress_interval == 0 or completed_count == len(candidate_repositories):
                    status_print(f"[SCAN] {completed_count}/{len(candidate_repositories)} repositories completed; {get_developer_count()} unique developers found so far.")
                if args.checkpoint_interval and completed_count % args.checkpoint_interval == 0:
                    output_results(projects, repositories, partial=True)
            while len(futures) < args.max_workers and submit_next(executor):
                pass
            if should_stop_for_runtime() and not futures:
                status_print(f"[WARN] Runtime cap reached: {args.max_run_minutes} minute(s).")
                output_results(projects, repositories, partial=True)
                return
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
    except Exception:  # pylint: disable=broad-exception-caught
        output_results(last_projects, last_repositories, partial=True)
        raise
