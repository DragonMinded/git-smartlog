#!/usr/bin/env python3
import argparse
import configparser
import git
import json
import locale
import logging
import os
import subprocess
import sys
from smartlog.builder import TreeBuilder
from smartlog.printer import TreePrinter, TreeNodePrinter, RefMap
from time import time
from typing import Dict, List, Optional

CONFIG_FNAME = "smartlog"

logging.basicConfig()

logger = logging.getLogger("smartlog")
logger.setLevel(logging.ERROR)

def parse_args():
    parser = argparse.ArgumentParser(description="Git Smartlog")
    parser.add_argument("-a", "--all", action="store_true", help="Force display all commits, regardless of time")
    return parser.parse_args()

def resolve_head(config, repo, options: List[str]) -> Optional[str]:
    for name in options:
        head_refname = config.get("remote", "head", fallback=f"origin/{name}")
        try:
            head_ref = repo.refs[head_refname]
            return head_ref
        except IndexError:
            pass

    return None

def infer_default_branch(config, repo) -> Optional[str]:
    # Grab the default encoding to decode shell output.
    _, encoding = locale.getdefaultlocale()

    # First, try to ask for the remote, take the first one we get.
    try:
        rawdata = subprocess.check_output([
            'git',
            'remote',
        ])
    except Exception as e:
        return None

    origins = [x.strip() for x in rawdata.decode(encoding).splitlines()]

    # Prioritize default "upstream" and "origin" first if it exists.
    if "upstream" in origins:
        origins = ["upstream", *[x for x in origins if x != "upstream"]]
    if "origin" in origins:
        origins = ["origin", *[x for x in origins if x != "origin"]]

    try:
        default_origin = origins[0]
        rawdata = subprocess.check_output([
            'git',
            'remote',
            'show',
            default_origin,
        ])
    except Exception as e:
        return None

    remotes = [x.strip() for x in rawdata.decode(encoding).splitlines() if "HEAD" in x]

    for remote in remotes:
        if remote[:13] == "HEAD branch: ":
            return remote[13:].strip()

    return None

class GitHubPRStatus:
    def __init__(
        self,
        id: str,
        branch: str,
        state: str,
        decision: str,
        checks: Dict[str, str],
        title: str,
        url: str,
    ) -> None:
        # GitHub PR ID.
        self.id = id
        # Full branch name, including origin/
        self.branch = branch
        # State, with valid values OPEN, MERGED and CLOSED
        self.state = state
        # Team decision, with valid values APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED and no decision
        self.decision = decision
        # Map of name of check to status, with values being PASSED, SKIPPED, FAILED or RUNNING
        self.checks = checks
        # Title of PR
        self.title = title
        # URL of PR
        self.url = url

    def __repr__(self) -> str:
        return f"GitHubPRStatus({self.branch}, {self.state}, {self.decision}, {self.checks}, {self.title}, {self.url})"

    def __str__(self) -> str:
        return self.__repr__()

def pull_gh_commits() -> Dict[str, GitHubPRStatus]:
    try:
        rawdata = subprocess.check_output([
            'gh',
            'pr',
            'status',
            '--json',
            'number,state,reviewDecision,title,headRefName,statusCheckRollup,url'
        ])
    except Exception as e:
        return {}

    jsondata = json.loads(rawdata)
    if 'createdBy' not in jsondata:
        return {}

    retval: Dict[str, GitHubPRStatus] = {}
    for pr in jsondata['createdBy']:
        checks: Dict[str, str] = {}
        if 'statusCheckRollup' in pr:
            for check in (pr['statusCheckRollup'] or []):
                if check['status'] == 'COMPLETED':
                    if check['conclusion'] == 'SUCCESS':
                        checks[check['name']] = 'PASSED'
                    elif check['conclusion'] in {'SKIPPED', 'NEUTRAL'}:
                        checks[check['name']] = 'SKIPPED'
                    else:
                        checks[check['name']] = 'FAILED'
                else:
                    checks[check['name']] = 'RUNNING'

        branch = 'origin/' + pr['headRefName']
        retval[branch] = GitHubPRStatus(
            id=str(pr['number']),
            branch=branch,
            state=pr['state'],
            decision=pr['reviewDecision'] or None,
            checks=checks,
            title=pr['title'],
            url=pr['url'],
        )
    return retval

def main():
    start_time = time()

    args = parse_args()

    # Compute minimum commit time for displayed commits
    if args.all:
        date_limit = None
    else:
        date_limit = time() - (14 * 24 * 3600)  # 14 days

    # Attempt to open the git repo in the current working directory
    cwd = os.getcwd()
    try:        
        repo = git.Repo(cwd, search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print("Could not find a git repository at {}".format(cwd))
        exit(1)

    # Attempt to pull github information for repo as well.
    prs = pull_gh_commits()

    # Load the smartlog config file
    config = configparser.ConfigParser(allow_no_value = True)
    config.read(os.path.join(repo.git_dir, CONFIG_FNAME))
    
    refmap = RefMap(repo.head)

    # First, try to suss out remote branch default.
    default_branch = infer_default_branch(config, repo)

    # Now, try to infer the actual head ref from that. Fall back to common branch names
    # across git.
    head_ref = resolve_head(config, repo, [*([default_branch] if default_branch is not None else []), *["main", "trunk", "master"]])
    if head_ref is None:
        print(f"Unable to find head branch!")
        exit(1)
    refmap.add(head_ref)

    tree_builder = TreeBuilder(repo, head_ref.commit, date_limit = date_limit)

    # Add current head commit
    tree_builder.add(repo.head.commit, ignore_date_limit = True)

    # Add all local branches (and remote tracking too)
    for ref in repo.heads:
        logger.debug("Adding local branch {}".format(ref.name))
        tree_builder.add(ref.commit)
        refmap.add(ref)

        try:
            remote_ref = ref.tracking_branch()
            if remote_ref is not None:
                logger.debug("Adding remote tracking branch {}".format(remote_ref.name))
                if remote_ref.commit != ref.commit:
                    tree_builder.add(remote_ref.commit)
                refmap.add(remote_ref)
        except ValueError:
            pass

    # Add any extra remote branches from the config file
    if config.has_section("extra_refs"):
        for key in config["extra_refs"]:
            try:
                ref = repo.refs[key]
                refmap.add(ref)
                tree_builder.add(ref.commit)
            except IndexError:
                print(f"Unable to find {key} ref. Check configuration in .git/{CONFIG_FNAME} file")

    node_printer = TreeNodePrinter(repo, refmap, prs)
    tree_printer = TreePrinter(repo, node_printer)
    tree_printer.print_tree(tree_builder.root_node)

    if tree_builder.skip_count > 0: 
        print("Skipped {} old commits. Use `-a` argument to display them.".format(tree_builder.skip_count))

    print("Finished in {:.2f} s.".format(time() - start_time))

if __name__ == "__main__":
    main()
