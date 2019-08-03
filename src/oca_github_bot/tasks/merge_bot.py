# Copyright (c) ACSONE SA/NV 2019
# Distributed under the MIT License (http://opensource.org/licenses/MIT).

import random
import subprocess

from .. import github
from ..build_wheels import build_and_check_wheel, build_and_publish_wheel
from ..config import (
    GITHUB_CHECK_SUITES_IGNORED,
    GITHUB_STATUS_IGNORED,
    MERGE_BOT_INTRO_MESSAGES,
    SIMPLE_INDEX_ROOT,
    switchable,
)
from ..manifest import bump_manifest_version, git_modified_addon_dirs
from ..queue import getLogger, task
from ..version_branch import make_merge_bot_branch, parse_merge_bot_branch
from .main_branch_bot import main_branch_bot_actions

_logger = getLogger(__name__)

LABEL_MERGED = "merged 🎉"


def _git_call(cmd):
    subprocess.check_output(cmd, universal_newlines=True, stderr=subprocess.STDOUT)


def _git_delete_branch(remote, branch):
    try:
        # delete merge bot branch
        _git_call(["git", "push", remote, f":{branch}"])
    except subprocess.CalledProcessError as e:
        if "unable to delete" in e.output:
            # remote branch may not exist on remote
            pass
        else:
            raise


def _get_merge_bot_intro_message():
    i = random.randint(0, len(MERGE_BOT_INTRO_MESSAGES) - 1)
    return MERGE_BOT_INTRO_MESSAGES[i]


def _merge_bot_merge_pr(org, repo, merge_bot_branch, dry_run=False):
    pr, target_branch, username, bumpversion = parse_merge_bot_branch(merge_bot_branch)
    # first check if the merge bot branch is still on top of the target branch
    _git_call(["git", "checkout", target_branch])
    r = subprocess.call(
        ["git", "merge-base", "--is-ancestor", target_branch, merge_bot_branch]
    )
    if r != 0:
        _logger.info(
            f"{merge_bot_branch} can't be fast forwarded on {target_branch}, "
            f"rebasing again."
        )
        intro_message = (
            f"It looks like something changed on `{target_branch}` in the meantime.\n"
            f"Let me try again (no action is required from you)."
        )
        merge_bot_start(
            org,
            repo,
            pr,
            username,
            bumpversion,
            dry_run=dry_run,
            intro_message=intro_message,
        )
        return False
    # Get modified addons list on the PR and not on the merge bot branch
    # because travis .pot generation may sometimes touch
    # other addons unrelated to the PR and we don't want to bump
    # version on those. This is also the least surprising approach, bumping
    # version only on addons visibly modified on the PR, and not on
    # other addons that may be modified by the bot for reasons unrelated
    # to the PR.
    _git_call(["git", "fetch", "origin", f"refs/pull/{pr}/head:tmp-pr-{pr}"])
    _git_call(["git", "checkout", f"tmp-pr-{pr}"])
    modified_addon_dirs = git_modified_addon_dirs(".", target_branch)
    # Run main branch bot actions before bump version.
    # Do not run the main branch bot if there are no modified addons,
    # because it is dedicated to addons repos.
    _git_call(["git", "checkout", merge_bot_branch])
    if modified_addon_dirs:
        main_branch_bot_actions(org, repo, target_branch)
    for addon_dir in modified_addon_dirs:
        # TODO wlc lock and push
        # TODO msgmerge and commit
        if bumpversion:
            bump_manifest_version(addon_dir, bumpversion, git_commit=True)
            build_and_check_wheel(addon_dir)
    # create the merge commit
    _git_call(["git", "checkout", target_branch])
    msg = f"Merge PR #{pr} into {target_branch}\n\nSigned-off-by {username}"
    _git_call(["git", "merge", "--no-ff", "--m", msg, merge_bot_branch])
    if dry_run:
        _logger.info(f"DRY-RUN git push in {org}/{repo}@{target_branch}")
    else:
        _logger.info(f"git push in {org}/{repo}@{target_branch}")
        _git_call(["git", "push", "origin", target_branch])
    # build and publish wheel
    if bumpversion and modified_addon_dirs and SIMPLE_INDEX_ROOT:
        for addon_dir in modified_addon_dirs:
            build_and_publish_wheel(addon_dir, SIMPLE_INDEX_ROOT, dry_run)
    # TODO wlc unlock modified_addons
    _git_delete_branch("origin", merge_bot_branch)
    with github.login() as gh:
        gh_pr = gh.pull_request(org, repo, pr)
        merge_sha = github.git_get_head_sha()
        github.gh_call(
            gh_pr.create_comment,
            f"Congratulations, your PR was merged at {merge_sha}. "
            f"Thanks a lot for contributing to {org}. ❤️\n\n"
            f"PS: Don't worry if GitHub says there are "
            f"unmerged commits: it is due to a rebase before merge. "
            f"All commits of this PR have been merged into `{target_branch}`.",
        )
        gh_issue = github.gh_call(gh_pr.issue)
        if dry_run:
            _logger.info(f"DRY-RUN add {LABEL_MERGED} label to PR {gh_pr.url}")
        else:
            _logger.info(f"add {LABEL_MERGED} label to PR {gh_pr.url}")
            github.gh_call(gh_issue.add_labels, LABEL_MERGED)
        github.gh_call(gh_pr.close)
    return True


@task()
@switchable("merge_bot")
def merge_bot_start(
    org, repo, pr, username, bumpversion=None, dry_run=False, intro_message=None
):
    with github.login() as gh:
        if not github.git_user_can_push(gh.repository(org, repo), username):
            gh_pr = gh.pull_request(org, repo, pr)
            github.gh_call(
                gh_pr.create_comment,
                f"Sorry @{username} "
                f"you do not have push permission, so I can't merge for you.",
            )
            return
        gh_pr = gh.pull_request(org, repo, pr)
        target_branch = gh_pr.base.ref
        try:
            with github.temporary_clone(org, repo, target_branch):
                # create merge bot branch from PR and rebase it on target branch
                merge_bot_branch = make_merge_bot_branch(
                    pr, target_branch, username, bumpversion
                )
                _git_call(
                    ["git", "fetch", "origin", f"pull/{pr}/head:{merge_bot_branch}"]
                )
                _git_call(["git", "checkout", merge_bot_branch])
                # TODO for each modified addon, wlc lock / commit / push
                # TODO then pull target_branch again
                _git_call(["git", "rebase", "--autosquash", target_branch])
                # push and let tests run again
                _git_delete_branch("origin", merge_bot_branch)
                _git_call(["git", "push", "origin", merge_bot_branch])
                if not intro_message:
                    intro_message = _get_merge_bot_intro_message()
                github.gh_call(
                    gh_pr.create_comment,
                    f"{intro_message}\n"
                    f"Rebased to [{merge_bot_branch}]"
                    f"(https://github.com/{org}/{repo}/commits/{merge_bot_branch})"
                    f", awaiting test results.",
                )
        except subprocess.CalledProcessError as e:
            cmd = " ".join(e.cmd)
            github.gh_call(
                gh_pr.create_comment,
                f"@{username} The merge process could not start, because "
                f"command `{cmd}` failed with output:\n```\n{e.output}\n```",
            )
            raise
        except Exception as e:
            github.gh_call(
                gh_pr.create_comment,
                f"@{username} The merge process could not start, because "
                f"of exception {e}.",
            )
            raise


def _get_commit_success(gh_commit):
    """ Test commit status, using both status and check suites APIs """
    success = None  # None means don't know / in progress
    old_travis = False
    gh_status = github.gh_call(gh_commit.status)
    for status in gh_status.statuses:
        if status.context in GITHUB_STATUS_IGNORED:
            # ignore
            continue
        if status.state == "success":
            success = True
            # <hack>
            if status.context.startswith("continuous-integration/travis-ci"):
                old_travis = True
            # </hack>
        elif status.state == "pending":
            # in progress
            return None
        else:
            return False
    gh_check_suites = github.gh_call(gh_commit.check_suites)
    for check_suite in gh_check_suites:
        if check_suite.app.name in GITHUB_CHECK_SUITES_IGNORED:
            # ignore
            continue
        if check_suite.conclusion == "success":
            success = True
        elif not check_suite.conclusion:
            # not complete
            # <hack>
            if check_suite.app.name == "Travis CI" and old_travis:
                # ignore incomplete new Travis when old travis status is ok
                continue
            # </hack>
            return None
        else:
            return False
    return success


@task()
@switchable("merge_bot")
def merge_bot_status(org, repo, merge_bot_branch, sha):
    with github.temporary_clone(org, repo, merge_bot_branch):
        head_sha = github.git_get_head_sha()
        if head_sha != sha:
            # the branch has evolved, this means that this status
            # does not correspond to the last commit of the bot, ignore it
            return
        pr, _, username, _ = parse_merge_bot_branch(merge_bot_branch)
        with github.login() as gh:
            gh_repo = gh.repository(org, repo)
            gh_pr = gh.pull_request(org, repo, pr)
            gh_commit = github.gh_call(gh_repo.commit, sha)
            success = _get_commit_success(gh_commit)
            if success is None:
                # checks in progress
                return
            elif success:
                try:
                    _merge_bot_merge_pr(org, repo, merge_bot_branch)
                except subprocess.CalledProcessError as e:
                    cmd = " ".join(e.cmd)
                    github.gh_call(
                        gh_pr.create_comment,
                        f"@{username} The merge process could not be "
                        f"finalized, because "
                        f"command `{cmd}` failed with output:\n```\n{e.output}\n```",
                    )
                    raise
                except Exception as e:
                    github.gh_call(
                        gh_pr.create_comment,
                        f"@{username} The merge process could not be "
                        f"finalized because an exception was raised: {e}.",
                    )
                    raise
            else:
                github.gh_call(
                    gh_pr.create_comment,
                    f"@{username} your merge command was aborted due to failed "
                    f"check(s), which you can inspect on "
                    f"[this commit of {merge_bot_branch}]"
                    f"(https://github.com/{org}/{repo}/commits/{sha}).\n\n"
                    f"After fixing the problem, you can re-issue a merge command. "
                    f"Please refrain from merging manually as it will most probably "
                    f"make the target branch red.",
                )
                _git_call(["git", "push", "origin", f":{merge_bot_branch}"])
