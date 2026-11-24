---
document:
  status: DRAFT
  role: PRIMARY_SOURCE
---

# Bitbucket Cloud setup

Add the `bitbucket` object to the worker's `atlassian.json` and retain the other sections. The worker reads the file from `KIAO_WORKER_STATE_PATH`, which defaults to `$KIAO_WORKER_ROOT_PATH/.kiao-config`. The standalone worker defaults to `/tmp` as its store root. From a shell outside a worker turn, set `KIAO_WORKER_STATE_PATH` to the intended worker's absolute state directory.

```json
{
  "bitbucket": {
    "email": "account@example.com",
    "token": "<Bitbucket API token>",
    "workspace": "workspace-slug",
    "repository": "repo-slug"
  }
}
```

With `email`, the script uses HTTP Basic authentication with the Atlassian account email and API token. Omit `email` for Bearer authentication, including repository, project or workspace access tokens. Restrict access to the worker account and run the script directly so the token stays in the configuration. [API tokens](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/) · [Access tokens](https://support.atlassian.com/bitbucket-cloud/docs/access-tokens/)

For a scoped API token, grant the scopes for the requested commands. Each endpoint lists its scopes, including the equivalents for access tokens. [Bitbucket Cloud REST API](https://developer.atlassian.com/cloud/bitbucket/rest/)

| Operation | API token scopes |
| --- | --- |
| Workspace, project and member reads | See the workspace endpoint's scopes |
| Repository, branch, tag and commit reads | `read:repository:bitbucket` |
| Pull request reads | `read:pullrequest:bitbucket` |
| Pull request creation and updates | `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket` |
| Pipeline runs, steps and logs | `read:pipeline:bitbucket` for reads, `write:pipeline:bitbucket` to start or stop a run |

Run `python3 "$B" repo get` to check access to the configured repository. For HTTP 401, check the token and its authentication mode. For HTTP 403, check the token scopes and repository permissions.
