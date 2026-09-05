"""Trusted Step-7 exact-HEAD publication broker.

The task and model are never publication authorities.  This module derives
external identities from the registered profile and SQLite task row, then
records the external result through one atomic host transaction.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
from collections.abc import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows fallback
    fcntl = None
    import msvcrt

from control_db import ControlPlaneDatabase, ControlPlaneError, StateConflict
from prepare_workspace import (Profile, github, process_owns_workspace,
                               require_physical_namespace, read_secret)
from rulesets import (RulesetError, fetch_all_rulesets, fetch_ruleset_details,
                      require_default_branch_ruleset, security_fingerprint)
from workspace_boundary import (GIT_EXECUTABLE, physical_directory, run_git,
                                sterile_environment, validate_repository)
from publication_key import PublicationKeyError, verified_private_key


class PublicationError(RuntimeError):
    pass


class PublicationBusy(PublicationError):
    pass


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def safe_git_environment() -> dict[str, str]:
    """Return the same sterile host environment used by publication Git."""
    return sterile_environment(pathlib.Path.cwd())


def canonical_publication_remote(profile: Profile) -> str:
    """Derive the only SSH publication remote from the registered repository."""
    if not isinstance(profile.repository, str) or not REPOSITORY_RE.fullmatch(profile.repository):
        raise PublicationError("registered repository identity is not owner/name")
    return "git@github.com:" + profile.repository + ".git"


@contextlib.contextmanager
def publication_lock(profile: Profile, identifier: str) -> Iterator[pathlib.Path]:
    """Serialize one task without treating a lock pathname as identity."""
    if not re.fullmatch(r"T-[0-9]{6}", identifier):
        raise PublicationError("publication task identity is invalid")
    root = require_physical_namespace(pathlib.Path(profile.state_root))
    physical_directory(root.parent)
    root.mkdir(parents=True, exist_ok=True)
    physical_directory(root)
    lock_dir = root / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / ("publication-" + identifier + ".lock")
    stream = path.open("a+b")
    try:
        try:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - native Windows fallback
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError) as exc:
            raise PublicationBusy("another publisher owns this task lock") from exc
        yield path
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _host_git(repository: pathlib.Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if not GIT_EXECUTABLE or not os.path.isabs(GIT_EXECUTABLE):
        raise PublicationError("host Git executable is unavailable")
    # Git on Windows rejects the NUL device as an include pathname.  A
    # host-created empty file keeps the include channel explicitly inert on
    # every platform without consulting repository configuration.
    empty_config = tempfile.NamedTemporaryFile(prefix="symphony-empty-git-config-", delete=False)
    empty_config.close()
    try:
        command = [GIT_EXECUTABLE, "--no-pager", "--no-replace-objects",
                   "-c", "core.hooksPath=" + os.devnull,
                   "-c", "core.fsmonitor=false", "-c", "core.attributesFile=" + os.devnull,
                   "-c", "credential.helper=", "-c", "include.path=" + empty_config.name,
                   "-c", "protocol.ext.allow=never", "-c", "maintenance.auto=0", "-C", str(repository)]
        return subprocess.run(command + list(args), env=env, text=True,
                              capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=120, check=False)
    finally:
        pathlib.Path(empty_config.name).unlink(missing_ok=True)


def _remote_head(remote: str, branch: str, env: dict[str, str]) -> str | None:
    if not GIT_EXECUTABLE or not os.path.isabs(GIT_EXECUTABLE):
        raise PublicationError("host Git executable is unavailable")
    result = subprocess.run(
        [GIT_EXECUTABLE, "ls-remote", "--refs", remote, "refs/heads/" + branch],
        env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
        timeout=120, check=False,
    )
    if result.returncode:
        raise PublicationError("publication remote branch could not be inspected")
    rows = [line.split("\t") for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2 or not SHA_RE.fullmatch(rows[0][0]):
        raise PublicationError("publication remote branch response is malformed")
    return rows[0][0]


def verify_workspace(profile: Profile, task: dict[str, object], workspace: pathlib.Path) -> str:
    """Re-prove the retained Step-6 workspace before reading credentials."""
    try:
        workspace = validate_repository(workspace)
        branch = run_git(workspace, "branch", "--show-current")
        origin = run_git(workspace, "config", "--get", "remote.origin.url")
        head = run_git(workspace, "rev-parse", "HEAD")
        commit = run_git(workspace, "cat-file", "-t", "HEAD")
        clean = run_git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        head_sha = head.stdout.strip()
        ancestry = run_git(workspace, "merge-base", "--is-ancestor", str(task["base_sha"]), head_sha)
    except Exception as exc:
        raise PublicationError("trusted publication workspace could not be verified") from exc
    if branch.stdout.strip() != task["branch"]:
        raise PublicationError("publication workspace is not on the host-owned task branch")
    if origin.stdout.strip() != profile.git_remote:
        raise PublicationError("publication workspace origin differs from the registered remote")
    if head.returncode or head_sha != task["current_head"]:
        raise PublicationError("publication workspace HEAD differs from SQLite current_head")
    if commit.returncode or commit.stdout.strip() != "commit":
        raise PublicationError("publication HEAD is not a commit")
    if clean.stdout:
        raise PublicationError("publication workspace is not clean")
    if ancestry.returncode:
        raise PublicationError("publication HEAD is not based on the recorded task base")
    return head_sha


def _ruleset_snapshot(profile: Profile, task: dict[str, object], token: str, github_call=github) -> dict[str, object]:
    repository = github_call(profile, token, "GET", "")
    default = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(default, str) or not default:
        raise RulesetError("GitHub did not report the repository default branch")
    if default != task["base_ref"]:
        raise RulesetError("GitHub default branch differs from the recorded task base ref")
    summaries = fetch_all_rulesets(lambda page: github_call(
        profile, token, "GET", f"/rulesets?includes_parents=true&per_page=100&page={page}"))
    details = fetch_ruleset_details(summaries, lambda ruleset_id: github_call(
        profile, token, "GET", f"/rulesets/{ruleset_id}?includes_parents=true"))
    selected = require_default_branch_ruleset(details, default)
    return {"ruleset_id": selected["id"], "fingerprint": security_fingerprint(selected)}


def _pull_request_fields(pr: object) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    if not isinstance(pr, dict):
        raise PublicationError("GitHub pull-request response is malformed")
    head = pr.get("head")
    base = pr.get("base")
    repo = head.get("repo") if isinstance(head, dict) else None
    if not isinstance(head, dict) or not isinstance(base, dict) or not isinstance(repo, dict):
        raise PublicationError("GitHub pull-request identity is malformed")
    return pr, head, base


def _validate_pull_request(pr: object, profile: Profile, task: dict[str, object]) -> dict[str, object]:
    value, head, base = _pull_request_fields(pr)
    head_repo = head.get("repo")
    if (value.get("state") != "open" or value.get("draft") is not True or
            head.get("ref") != task["branch"] or base.get("ref") != task["base_ref"] or
            not isinstance(head_repo, dict) or head_repo.get("full_name") != profile.repository or
            head.get("sha") != task["current_head"]):
        raise PublicationError("GitHub pull request does not prove the exact draft publication identity")
    number = value.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise PublicationError("GitHub pull request number is invalid")
    return value


def _list_pull_requests(profile: Profile, task: dict[str, object], token: str, github_call=github) -> list[dict[str, object]]:
    owner = profile.repository.split("/", 1)[0]
    result: list[dict[str, object]] = []
    for page in range(1, 101):
        value = github_call(
            profile, token, "GET",
            f"/pulls?state=all&head={owner}:{task['branch']}&per_page=100&page={page}",
        )
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise PublicationError("GitHub pull-request listing is malformed")
        result.extend(value)
        if len(value) < 100:
            return result
    raise PublicationError("GitHub pull-request listing exceeded pagination bound")


def reconcile_pull_request(profile: Profile, task: dict[str, object], token: str, github_call=github) -> dict[str, object]:
    prs = _list_pull_requests(profile, task, token, github_call)
    same_branch = []
    for pr in prs:
        if not isinstance(pr.get("head"), dict):
            raise PublicationError("GitHub pull-request identity is malformed")
        if pr["head"].get("ref") == task["branch"]:
            same_branch.append(pr)
    exact = [pr for pr in same_branch if isinstance(pr.get("base"), dict) and
             pr["base"].get("ref") == task["base_ref"] and
             isinstance(pr["head"].get("repo"), dict) and
             pr["head"]["repo"].get("full_name") == profile.repository]
    if len(exact) > 1:
        raise PublicationError("multiple matching draft pull requests exist")
    if len(same_branch) > 1 or (same_branch and not exact):
        raise PublicationError("an existing pull request contradicts the publication identity")
    if exact:
        number = exact[0].get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise PublicationError("GitHub pull request number is invalid")
        return _validate_pull_request(
            github_call(profile, token, "GET", f"/pulls/{number}"), profile, task,
        )
    created = github_call(profile, token, "POST", "/pulls", {
        "title": f"[{task['identifier']}] {task['title']}",
        "body": "\n".join((
            f"Task: {task['identifier']}", f"Published HEAD: {task['current_head']}",
            f"Base: {task['base_ref']} @ {task['base_sha']}",
            "Lifecycle acceptance is stored in Pilot SQLite.", "Human merge only.",
        )),
        "head": task["branch"], "base": task["base_ref"], "draft": True,
    })
    value = _validate_pull_request(created, profile, task)
    # Re-read the server object before any SQLite finalization.
    reread = github_call(profile, token, "GET", f"/pulls/{value['number']}")
    return _validate_pull_request(reread, profile, task)


def _generate_bundle(workspace: pathlib.Path) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path]:
    directory = tempfile.TemporaryDirectory(prefix="symphony-pilot-publication-")
    bundle = pathlib.Path(directory.name) / "task.bundle"
    result = run_git(workspace, "bundle", "create", str(bundle), "HEAD")
    if result.returncode:
        directory.cleanup()
        raise PublicationError("host could not generate the publication bundle")
    return directory, bundle


def _publish_branch(profile: Profile, task: dict[str, object], bundle: pathlib.Path,
                    stable_key: pathlib.Path, remote: str,
                    *, allow_existing_remote: bool) -> None:
    if not GIT_EXECUTABLE:
        raise PublicationError("host Git executable is unavailable")
    env = sterile_environment(pathlib.Path.cwd())
    env["GIT_SSH_COMMAND"] = "ssh -i " + str(stable_key) + " -o IdentitiesOnly=yes -o BatchMode=yes"
    env["GIT_SSH_VARIANT"] = "ssh"
    with tempfile.TemporaryDirectory(prefix="symphony-pilot-publication-bare-") as directory:
        bare = pathlib.Path(directory) / "repository.git"
        initialized = subprocess.run([GIT_EXECUTABLE, "init", "--bare", str(bare)], env=env,
                                     capture_output=True, text=True, check=False)
        if initialized.returncode:
            raise PublicationError("host publication repository could not be initialized")
        base_fetch = _host_git(bare, "fetch", remote, "refs/heads/" + str(task["base_ref"]) + ":refs/remotes/publication/base", env=env)
        if base_fetch.returncode:
            detail = (base_fetch.stderr or base_fetch.stdout).strip().replace("\n", " ")
            raise PublicationError("canonical publication default branch could not be fetched: " + detail[:240])
        base = _host_git(bare, "rev-parse", "refs/remotes/publication/base", env=env)
        if base.returncode or not SHA_RE.fullmatch(base.stdout.strip()):
            raise PublicationError("canonical publication default branch is malformed")
        remote_base_ancestry = _host_git(
            bare, "merge-base", "--is-ancestor", str(task["base_sha"]),
            "refs/remotes/publication/base", env=env,
        )
        if remote_base_ancestry.returncode:
            raise PublicationError("recorded task base is not an ancestor of the remote base")
        branch_ref = "refs/heads/" + str(task["branch"])
        remote_head = _remote_head(remote, str(task["branch"]), env)
        if remote_head is not None and remote_head != task["current_head"]:
            raise PublicationError("unexpected remote task branch head exists")
        if remote_head is not None and not allow_existing_remote:
            raise PublicationError("task branch exists before the first publication attempt")
        check = _host_git(bare, "bundle", "verify", str(bundle), env=env)
        if check.returncode:
            raise PublicationError("host publication bundle verification failed")
        imported = _host_git(bare, "fetch", str(bundle), "HEAD:refs/quarantine/task", env=env)
        if imported.returncode:
            raise PublicationError("host publication bundle import failed")
        fsck = _host_git(bare, "fsck", "--strict", "--full", env=env)
        if fsck.returncode:
            raise PublicationError("host publication object verification failed")
        commit = _host_git(bare, "cat-file", "-t", str(task["current_head"]), env=env)
        if commit.returncode or commit.stdout.strip() != "commit":
            raise PublicationError("requested publication HEAD is not exactly a commit")
        ancestor = _host_git(bare, "merge-base", "--is-ancestor", str(task["base_sha"]), str(task["current_head"]), env=env)
        if ancestor.returncode:
            raise PublicationError("recorded base is not an ancestor of publication HEAD")
        if remote_head is None:
            update = _host_git(bare, "update-ref", branch_ref, str(task["current_head"]), "0" * 40, env=env)
            if update.returncode:
                raise PublicationError("host publication branch lease was lost")
            # Empty expected value is an expected-absence CAS, not permission
            # to overwrite an existing remote ref.
            pushed = _host_git(
                bare, "push", "--force-with-lease=" + branch_ref + ":",
                remote, branch_ref + ":" + branch_ref, env=env,
            )
            if pushed.returncode:
                raise PublicationError("host publication push failed")


def _publish_task_locked(profile: Profile, task_id: str, *, database_path: pathlib.Path,
                         deployment_check=None, github_call=github, snapshot_fn=None) -> dict[str, object]:
    """Publish one exact ARCHIVIST task; all selectors and destinations are host-derived."""
    intent_started = False
    eligibility_established = False
    task: dict[str, object] | None = None
    publication: dict[str, object] | None = None
    remote_branch: str | None = None
    pr_number: int | None = None
    try:
        with ControlPlaneDatabase.open(database_path) as database:
            task = database.read_task(task_id)
            if task["project_slug"] != profile.slug:
                raise StateConflict("task is not registered to the selected project")
            if task["state"] == "READY_FOR_HUMAN_MERGE":
                publication = database.read_publication(task["id"])
                if publication and publication["publication_status"] == "published":
                    return {"task": task, "publication": publication, "idempotent": True}
            if (task["state"] != "ARCHIVIST" or not task["current_head"] or
                    database.connection.execute("SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (task["id"],)).fetchone() or
                    database.connection.execute("SELECT 1 FROM role_runs WHERE task_id = ? AND role = 'ARCHITECT' AND status = 'started' LIMIT 1", (task["id"],)).fetchone()):
                raise StateConflict("task is not eligible for publication")
            # From this point the exact task identity and publication
            # eligibility are known. Any handled failure must leave a durable
            # blocker even if it occurs before publication intent is written.
            eligibility_established = True
            remote_branch = str(task["branch"])
            workspace = require_physical_namespace(pathlib.Path(profile.workspace_root) / str(task["identifier"]))
            if process_owns_workspace(workspace):
                raise PublicationError("publication workspace is currently owned by another process")
            if deployment_check is not None:
                deployment_check(profile)
            verify_workspace(profile, task, workspace)
            prior_publication = database.read_publication(task["id"])
            remote = canonical_publication_remote(profile)
            # An exact remote branch is admissible only after a durable prior
            # attempt established recovery evidence; initial publication must
            # create the task branch while it is absent.
            allow_existing_remote = bool(
                prior_publication and
                prior_publication["publication_status"] in {"started", "failed"}
            )
            publication = database.start_publication(
                task["id"], head_sha=str(task["current_head"]), remote_branch=remote_branch,
            )
            intent_started = True
            token = read_secret(profile)
            temp_directory, bundle = _generate_bundle(workspace)
            try:
                with verified_private_key(profile, token, github_call) as verified_key:
                    stable_key, key_evidence = verified_key
                    # Deploy-key verification precedes Snapshot A; the stable
                    # key remains live through the snapshot and branch push.
                    snapshot_a = (snapshot_fn(profile, task, token) if snapshot_fn else
                                  _ruleset_snapshot(profile, task, token, github_call))
                    _publish_branch(
                        profile, task, bundle, stable_key, remote,
                        allow_existing_remote=allow_existing_remote,
                    )
                    task_for_pr = dict(task)
                    task_for_pr["current_head"] = task["current_head"]
                    pr = reconcile_pull_request(profile, task_for_pr, token, github_call)
                    pr_number = int(pr["number"])
                    remote_head = _remote_head(remote, remote_branch, {
                        **sterile_environment(pathlib.Path.cwd()),
                        "GIT_SSH_COMMAND": "ssh -i " + str(stable_key) + " -o IdentitiesOnly=yes -o BatchMode=yes",
                        "GIT_SSH_VARIANT": "ssh",
                    })
                    # Re-read the exact PR after the remote ref check. No
                    # GitHub protection read occurs after Snapshot B.
                    final_pr = _validate_pull_request(
                        github_call(profile, token, "GET", f"/pulls/{pr_number}"),
                        profile, task_for_pr,
                    )
                    snapshot_b = (snapshot_fn(profile, task, token) if snapshot_fn else
                                  _ruleset_snapshot(profile, task, token, github_call))
                    if snapshot_a["fingerprint"] != snapshot_b["fingerprint"]:
                        raise PublicationError("default-branch protection changed during publication")
                    if (remote_head != task["current_head"] or
                            final_pr["head"].get("sha") != task["current_head"]):
                        raise PublicationError("remote task branch is not the exact current HEAD")
                    evidence = {
                        "ruleset_id": snapshot_b["ruleset_id"],
                        "ruleset_snapshot_a_fingerprint": snapshot_a["fingerprint"],
                        "ruleset_snapshot_b_fingerprint": snapshot_b["fingerprint"],
                        "ruleset_fingerprint": snapshot_b["fingerprint"],
                        "deploy_key_id": key_evidence["id"],
                        "deploy_key_fingerprint": key_evidence["fingerprint"],
                    }
                    finalized = database.finalize_publication(
                        task["id"], head_sha=str(task["current_head"]), remote_branch=remote_branch,
                        github_pr_number=pr_number, evidence=evidence,
                    )
                    return {"task": finalized, "publication": database.read_publication(task["id"]), "idempotent": False}
            finally:
                temp_directory.cleanup()
    except Exception as exc:
        # Exception (rather than BaseException) deliberately leaves
        # KeyboardInterrupt/SystemExit/GeneratorExit as process-disappearance
        # semantics, preserving a legitimate started intent for recovery.
        if eligibility_established and task is not None:
            try:
                with ControlPlaneDatabase.open(database_path) as database:
                    if intent_started:
                        database.fail_publication(
                            task["id"], detail=str(exc), head_sha=str(task["current_head"]),
                            remote_branch=remote_branch, github_pr_number=pr_number,
                        )
                    else:
                        database.record_blocker(
                            task_id=task["id"], kind="infrastructure",
                            body="publication preflight failed: " + str(exc),
                        )
            except Exception as persistence_error:
                raise PublicationError(
                    "publication failure could not be persisted"
                ) from persistence_error
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError(str(exc)) from exc


def publish_task(profile: Profile, task_id: str, *, database_path: pathlib.Path,
                 deployment_check=None, github_call=github, snapshot_fn=None) -> dict[str, object]:
    """Acquire the task-derived lock before any publication work begins."""
    with ControlPlaneDatabase.open_readonly(database_path) as database:
        task = database.read_task(task_id)
        if task["project_slug"] != profile.slug:
            raise PublicationError("task is not registered to the selected project")
        identifier = str(task["identifier"])
    with publication_lock(profile, identifier):
        return _publish_task_locked(
            profile, task_id, database_path=database_path,
            deployment_check=deployment_check, github_call=github_call, snapshot_fn=snapshot_fn,
        )
