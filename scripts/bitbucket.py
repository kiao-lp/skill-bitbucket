#!/usr/bin/env python3
"""Work with Bitbucket Cloud using the Kiao worker's credentials."""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from bitbucket_client import (
    API_ROOT,
    NETWORK_ERRORS,
    PREVIEW_BYTES,
    Bitbucket,
    BitbucketError,
    Credentials,
    InputError,
    api_url,
    json_text,
    save_download,
    tail,
)

WORKSPACE = r"[A-Za-z0-9_-]+"
REPOSITORY = WORKSPACE + r"/[A-Za-z0-9_-][A-Za-z0-9_.-]*"
REPOS = API_ROOT + "/repositories"
WORKSPACES = API_ROOT + "/workspaces"

REPO_FIELDS = "full_name,mainbranch.name,project.key,is_private,links.html.href"
PR_FIELDS = (
    "id,title,state,draft,source.branch.name,destination.branch.name,"
    "author.display_name,links.html.href,updated_on"
)
PR_DETAILS = PR_FIELDS + ",description,reviewers.display_name,reviewers.uuid,close_source_branch"
REVIEW_FIELDS = "user.display_name,role,approved,state"
COMMENT_FIELDS = "id,content.raw,user.display_name,created_on,parent.id,inline.path,inline.from,inline.to"
STATUS_FIELDS = "key,name,state,url,updated_on"
DIFFSTAT_FIELDS = "status,old.path,new.path,lines_added,lines_removed"
PIPELINE_FIELDS = "uuid,build_number,state.name,state.result.name,target.ref_name,target.commit.hash,created_on"
STEP_FIELDS = "uuid,name,state.name,state.result.name,started_on,completed_on"
COMMIT_FIELDS = "hash,summary.raw,date,author.user.display_name,links.html.href"


def workspace_slug(value):
    if not re.fullmatch(WORKSPACE, value):
        raise argparse.ArgumentTypeError("Use a workspace slug")
    return value


def repo_scope(value):
    if not re.fullmatch(REPOSITORY, value):
        raise argparse.ArgumentTypeError("Use WORKSPACE/REPO")
    return value


def positive_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("Use a positive integer ID")
    return value


def uuid_id(value):
    try:
        return "{" + str(UUID(value)) + "}"
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("Use a UUID, with or without braces") from None


def nonblank(value):
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("Provide non-empty text")
    return value


def ref(value):
    nonblank(value)
    if value in (".", "..") or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise argparse.ArgumentTypeError("Use a Git branch, tag or commit reference")
    return value


def segment(value):
    return quote(value, safe="")


def field_list(value):
    name = r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*"
    if not re.fullmatch(name + f"(?:,{name})*", value):
        raise argparse.ArgumentTypeError("Use comma-separated field paths such as id,source.branch.name")
    return value


def bounded_integer(value, lower, upper):
    number = int(value)
    if not lower <= number <= upper:
        raise argparse.ArgumentTypeError(f"Use a value between {lower} and {upper}")
    return number


def repository(args, client):
    scope = args.repo or f"{client.credentials.workspace}/{client.credentials.repository}"
    if not re.fullmatch(REPOSITORY, scope):
        raise InputError("Set bitbucket.workspace and bitbucket.repository, or pass --repo WORKSPACE/REPO")
    return f"{REPOS}/{scope}"


def workspace(args, client):
    slug = args.workspace or (args.repo.split("/")[0] if args.repo else client.credentials.workspace)
    if not re.fullmatch(WORKSPACE, slug):
        raise InputError("Set bitbucket.workspace or pass a workspace slug")
    return slug


def page(client, args, path, fields, **query):
    return client.page(path, fields, next_page=args.next, page_size=args.page_size, query=query)


def do_workspace(client, args):
    if args.action == "list":
        return page(client, args, API_ROOT + "/user/workspaces", "workspace.slug,workspace.uuid")
    path = f"{WORKSPACES}/{workspace(args, client)}"
    if args.action == "get":
        return client.object(path, "slug,name,uuid,links.html.href")
    if args.action == "members":
        return page(client, args, path + "/members", "user.display_name,user.uuid,user.account_id")
    return page(client, args, path + "/projects", "key,name,uuid,links.html.href")


def do_repo(client, args):
    if args.action == "list":
        return page(client, args, f"{REPOS}/{workspace(args, client)}", REPO_FIELDS, q=args.q)
    return client.object(repository(args, client), REPO_FIELDS)


def do_ref(client, args):
    collection = {"branch": "branches", "tag": "tags"}[args.group]
    path = f"{repository(args, client)}/refs/{collection}"
    fields = "name,target.hash,target.date"
    if args.action == "list":
        return page(client, args, path, fields, q=args.q)
    return client.object(f"{path}/{segment(args.name)}", fields)


def do_commit(client, args):
    repo = repository(args, client)
    if args.action == "list":
        path = repo + "/commits"
        if args.revision:
            path += "/" + segment(args.revision)
        return page(client, args, path, COMMIT_FIELDS)
    path = f"{repo}/commit/{segment(args.revision)}"
    if args.action == "statuses":
        return page(client, args, path + "/statuses", STATUS_FIELDS)
    return client.object(path, COMMIT_FIELDS + ",message")


def text_input(args, name):
    file = getattr(args, name + "_file")
    if file is not None:
        try:
            return file.read_text(encoding="utf-8")
        except UnicodeError:
            raise InputError(f"{file} must contain UTF-8 text") from None
    return getattr(args, name)


def pull_request_body(args):
    fields = {"title": args.title, "description": text_input(args, "description"),
              "draft": args.draft, "close_source_branch": args.close_source_branch}
    body = {key: value for key, value in fields.items() if value is not None}
    if args.action == "create":
        if args.source == args.destination:
            raise InputError("Choose different source and destination branches")
        body["source"] = {"branch": {"name": args.source}}
    if args.destination is not None:
        body["destination"] = {"branch": {"name": args.destination}}
    if args.reviewer is not None:
        body["reviewers"] = [{"uuid": user} for user in args.reviewer]
    if not body:
        raise InputError("Provide at least one pull request field to update")
    return body


def checked_output(args):
    path = args.output
    if path is not None:
        if path.exists() or path.is_symlink():
            raise InputError(f"Output file {path} already exists")
        if not path.parent.is_dir():
            raise InputError(f"Output directory {path.parent} does not exist")
    return path


def pull_request_diff(client, args, path):
    output = checked_output(args)
    with client.open(path + "/diff", accept="text/plain", query={"path": args.path, "binary": "false"}) as response:
        if output:
            return save_download(response, output)
        size = args.bytes or PREVIEW_BYTES
        text = response.read(size + 1)
    sys.stdout.buffer.write(text[:size])
    if len(text) > size:
        print("Diff trimmed. Use --path for a specific file or --output to save the diff.", file=sys.stderr)
    return None


def do_pr(client, args):
    path = repository(args, client) + "/pullrequests"
    action = args.action
    if action == "list":
        return page(client, args, path, PR_FIELDS, state=args.state, q=args.q, sort="-updated_on")
    if action == "create":
        return client.object(path, PR_FIELDS, method="POST", body=pull_request_body(args))
    path += "/" + args.id
    if action == "get":
        return client.object(path, PR_DETAILS if args.details else PR_FIELDS)
    if action == "update":
        return client.object(path, PR_FIELDS, method="PUT", body=pull_request_body(args))
    if action == "diff":
        return pull_request_diff(client, args, path)
    if action in ("diffstat", "statuses", "comments"):
        fields = {"diffstat": DIFFSTAT_FIELDS, "statuses": STATUS_FIELDS, "comments": COMMENT_FIELDS}
        return page(client, args, f"{path}/{action}", fields[action])
    if action == "comment":
        text = text_input(args, "text")
        if not text.strip():
            raise InputError("Provide non-empty comment text")
        body = {"content": {"raw": text}}
        if args.reply_to:
            body["parent"] = {"id": int(args.reply_to)}
        return client.object(path + "/comments", COMMENT_FIELDS, method="POST", body=body)
    if action == "merge":
        body = {}
        if args.strategy:
            body["merge_strategy"] = args.strategy
        if args.close_source_branch is not None:
            body["close_source_branch"] = args.close_source_branch
        message = text_input(args, "message")
        if message is not None:
            body["message"] = message
        if body:
            body["type"] = "pullrequest_merge_parameters"
        return client.object(path + "/merge", PR_FIELDS, method="POST", body=body or None)
    if action == "decline":
        return client.object(path + "/decline", PR_FIELDS, method="POST")
    if action in ("unapprove", "clear-request-changes"):
        endpoint = {"unapprove": "approve", "clear-request-changes": "request-changes"}[action]
        return client.object(f"{path}/{endpoint}", REVIEW_FIELDS, method="DELETE")
    return client.object(f"{path}/{action}", REVIEW_FIELDS, method="POST")


def pipeline_log(client, args, path):
    output = checked_output(args)
    step = args.step
    if step is None:
        steps = client.page(path + "/steps", "uuid")
        if steps.get("next") or len(steps["items"]) > 1:
            raise InputError("Pipeline has multiple steps. Run pipeline steps and pass the step UUID.")
        if not steps["items"]:
            raise InputError("Pipeline has no steps yet")
        try:
            step = uuid_id(steps["items"][0].get("uuid"))
        except argparse.ArgumentTypeError:
            raise BitbucketError("Bitbucket returned no valid step UUID") from None
    path += "/steps/" + segment(step)
    path += "/logs/" + segment(args.log_id) if args.log_id else "/log"
    size = args.bytes or PREVIEW_BYTES
    try:
        response = client.open(path, accept="*/*", byte_range=None if output else f"bytes=-{size}", log_download=True)
    except BitbucketError as exc:
        if output or exc.status != 416:
            raise
        if exc.empty_log:
            return None
        response = client.open(path, accept="*/*", log_download=True)
    with response:
        if output:
            return save_download(response, output)
        sys.stdout.buffer.write(tail(response, size))
    return None


def do_pipeline(client, args):
    path = repository(args, client) + "/pipelines"
    if args.action == "list":
        return page(client, args, path, PIPELINE_FIELDS, sort="-created_on",
                    **{"target.ref_name": args.branch, "target.commit.hash": args.commit})
    if args.action == "run":
        target = {"type": "pipeline_ref_target", "ref_type": "branch", "ref_name": args.branch}
        if args.custom:
            target["selector"] = {"type": "custom", "pattern": args.custom}
        return client.object(path, PIPELINE_FIELDS, method="POST", body={"target": target})
    path += "/" + segment(args.pipeline)
    if args.action == "get":
        return client.object(path, PIPELINE_FIELDS + ",completed_on")
    if args.action == "steps":
        return page(client, args, path + "/steps", STEP_FIELDS)
    if args.action == "stop":
        return client.object(path + "/stopPipeline", PIPELINE_FIELDS, method="POST")
    return pipeline_log(client, args, path)


def do_api(client, args):
    return client.object(args.target, args.fields)


def make_group(groups, name, handle, actions):
    parser = groups.add_parser(name)
    parser.set_defaults(handle=handle)
    subparsers = parser.add_subparsers(dest="action", required=True)
    return {action: subparsers.add_parser(action, help=help_text) for action, help_text in actions.items()}


def page_options(parser, *, filterable=False):
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--page-size", type=lambda value: bounded_integer(value, 10, 100),
                         default=10, metavar="N", help="results per page, default 10")
    options.add_argument("--next", type=api_target, metavar="URL", help="next link from the previous output")
    if filterable:
        parser.add_argument("--q", help="Bitbucket query filter for the first page")


def output_options(parser):
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--bytes", type=lambda value: bounded_integer(value, 1, 65536),
                         metavar="N", help="maximum stdout bytes, default 8192")
    options.add_argument("--output", type=Path, metavar="FILE", help="save the full response to a new file")


def text_options(parser, name, *, required=False):
    options = parser.add_mutually_exclusive_group(required=required)
    options.add_argument("--" + name, metavar="TEXT")
    options.add_argument(f"--{name}-file", type=Path, metavar="FILE", help="read UTF-8 text from a file")


def api_target(value):
    try:
        return api_url(value)
    except InputError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=repo_scope, metavar="WORKSPACE/REPO", help="override the Kiao repository")
    groups = parser.add_subparsers(dest="group", required=True)

    work = make_group(groups, "workspace", do_workspace, {
        "list": "list the authenticated user's workspaces",
        "get": "show a workspace",
        "members": "list members and reviewer UUIDs",
        "projects": "list projects",
    })
    for action in ("get", "members", "projects"):
        work[action].add_argument("workspace", nargs="?", type=workspace_slug)
    for action in ("list", "members", "projects"):
        page_options(work[action])

    repo = make_group(groups, "repo", do_repo, {"list": "list workspace repositories", "get": "show a repository"})
    repo["list"].add_argument("workspace", nargs="?", type=workspace_slug)
    page_options(repo["list"], filterable=True)

    for name in ("branch", "tag"):
        refs = make_group(groups, name, do_ref, {"list": f"list {name}s", "get": f"show a {name}"})
        page_options(refs["list"], filterable=True)
        refs["get"].add_argument("name", type=ref)

    commit = make_group(groups, "commit", do_commit, {
        "list": "list commits",
        "get": "show a commit",
        "statuses": "list commit build statuses",
    })
    commit["list"].add_argument("revision", nargs="?", type=ref)
    for action in ("get", "statuses"):
        commit[action].add_argument("revision", type=ref)
    page_options(commit["list"])
    page_options(commit["statuses"])

    pr = make_group(groups, "pr", do_pr, {
        "list": "list pull requests",
        "get": "show a pull request",
        "create": "open a pull request from a pushed branch",
        "update": "edit a pull request",
        "diffstat": "list changed files",
        "diff": "preview or save a diff",
        "statuses": "list build statuses",
        "comments": "list comments",
        "comment": "post a comment or reply",
        "approve": "approve a pull request",
        "unapprove": "remove your approval",
        "request-changes": "request changes",
        "clear-request-changes": "remove your change request",
        "merge": "merge a pull request",
        "decline": "decline a pull request",
    })
    for action, options in pr.items():
        if action not in ("list", "create"):
            options.add_argument("id", type=positive_id)
    page_options(pr["list"], filterable=True)
    pr["list"].add_argument("--state", action="append", choices=["OPEN", "MERGED", "DECLINED", "SUPERSEDED"],
                            help="state filter, repeatable, defaults to OPEN")
    pr["get"].add_argument("--details", action="store_true", help="include the description and reviewers")
    pr["create"].add_argument("--source", type=ref, required=True)
    for action in ("create", "update"):
        options = pr[action]
        options.add_argument("--title", type=nonblank, required=action == "create")
        options.add_argument("--destination", type=ref,
                             help="destination branch, defaults to the main branch on create")
        options.add_argument("--draft", action=argparse.BooleanOptionalAction)
        options.add_argument("--close-source-branch", action=argparse.BooleanOptionalAction)
        reviewers = options.add_mutually_exclusive_group()
        reviewers.add_argument("--reviewer", type=uuid_id, action="append", help="set reviewers by UUID, repeatable")
        if action == "update":
            reviewers.add_argument("--clear-reviewers", action="store_const", const=[], dest="reviewer")
        text_options(options, "description")
    for action in ("diffstat", "statuses", "comments"):
        page_options(pr[action])
    pr["diff"].add_argument("--path", action="append", help="file path in the repository, repeatable")
    output_options(pr["diff"])
    text_options(pr["comment"], "text", required=True)
    pr["comment"].add_argument("--reply-to", type=positive_id, metavar="COMMENT_ID")
    pr["merge"].add_argument("--strategy", choices=["merge_commit", "squash", "fast_forward",
                                                   "squash_fast_forward", "rebase_fast_forward", "rebase_merge"])
    pr["merge"].add_argument("--close-source-branch", action=argparse.BooleanOptionalAction)
    text_options(pr["merge"], "message")
    pr["merge"].epilog = (
        "For HTTP 202, check the returned location with "
        "api get LOCATION --fields task_status,merge_result.id,merge_result.state."
    )

    pipelines = make_group(groups, "pipeline", do_pipeline, {
        "list": "list recent pipeline runs",
        "get": "show a pipeline run and result",
        "steps": "list the steps of a run",
        "log": "show the end of a step log or save the full log",
        "run": "start a pipeline for a branch",
        "stop": "stop a pipeline run",
    })
    for action in ("get", "steps", "log", "stop"):
        pipelines[action].add_argument("pipeline", type=uuid_id, metavar="PIPELINE_UUID")
    page_options(pipelines["list"])
    pipelines["list"].add_argument("--branch", type=ref)
    pipelines["list"].add_argument("--commit", type=ref, help="filter by commit hash")
    page_options(pipelines["steps"])
    pipelines["log"].add_argument("step", nargs="?", type=uuid_id, metavar="STEP_UUID")
    pipelines["log"].add_argument("--log-id", type=uuid_id, metavar="UUID", help="container log UUID, if known")
    output_options(pipelines["log"])
    pipelines["run"].add_argument("branch", type=ref)
    pipelines["run"].add_argument("--custom", type=nonblank, help="custom pipeline name from bitbucket-pipelines.yml")

    api = make_group(groups, "api", do_api, {"get": "read selected fields from a Bitbucket REST endpoint"})
    api["get"].add_argument("target", type=api_target, metavar="PATH_OR_URL")
    api["get"].add_argument("--fields", type=field_list, required=True)
    return parser


def main(argv=None):
    args = parser_for_cli().parse_args(argv)
    try:
        client = Bitbucket(Credentials.load())
        result = args.handle(client, args)
        if result is not None:
            print(json_text(result))
        return 0
    except BrokenPipeError:
        return 1
    except NETWORK_ERRORS:
        print("Could not complete the Bitbucket request (network error or timeout).", file=sys.stderr)
        return 1
    except (InputError, BitbucketError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
