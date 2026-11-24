---
name: bitbucket
description: Create and review Bitbucket Cloud pull requests, inspect pipeline runs and step logs, and look up repositories, branches, tags and commits through the Kiao worker.
---

# Bitbucket

Set `B` to the absolute path of [scripts/bitbucket.py](scripts/bitbucket.py) in this skill directory. Run it with Python 3.11 or later. It reads the worker's Bitbucket credentials and repository defaults. Pass `--repo WORKSPACE/REPO` before the command to select another repository.

```bash
python3 "$B" pr create --source feature/export --destination main --title 'Fix CSV export' --description-file pr.md
python3 "$B" pipeline list --branch feature/export
python3 "$B" pipeline steps "$run"
python3 "$B" pipeline log "$run" "$step"
```

Create a PR from a pushed branch. Omit `--destination` to target the repository's main branch. Use the pipeline and step UUIDs from the preceding results. For a run with one step, `pipeline log` can select it automatically. Logs show the last 8 KiB by default. Use `--output FILE` to save the full log.

Use `RESOURCE ACTION --help` for parameters. Lists return one page of compact JSON. Reuse the command with `--next` and the returned link to continue. For reviews, start with `pr diffstat ID` and request individual files with `pr diff ID --path FILE`. After an uncertain write, check Bitbucket for the result before retrying.

Read [worker setup](references/setup.md) for the configuration, token scopes or authentication errors.
