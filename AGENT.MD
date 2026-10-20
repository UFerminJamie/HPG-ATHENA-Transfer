# Agent handoff rules

Read `README.md` first. This repository matches HiPerGator JSON annotation files to Athena DSA slide items and creates real DSA annotation records. Never treat a JSON file uploaded to a Girder folder as a slide annotation.

## Before a handoff

1. Check `git status --short --branch`, `git remote -v`, and the current branch. Read the README and relevant code. State whether the last run was dry-run or apply, which destination was used, where the manifest is, and which statuses remain. Do not copy credentials or source annotation contents into the handoff.
2. Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` after code changes. Report the result and any untested live-Athena behavior.
3. Run `git diff --check`, `git status --short`, and `git diff --cached --name-only` before committing. Review `git diff --cached` and `git ls-files` so only project source, tests, and documentation are staged. Keep `.env`, manifests, JSON annotations, WSI files, credentials, tokens, and personal data out of Git.
4. Commit only the reviewed files. Do not force-push or delete remote history. Tell the next agent the commit hash, branch, remote, test result, and remaining work.

## GitHub commands for a handoff

```bash
gh auth status
gh api user --jq .login
gh repo view OWNER/HPG-ATHENA-Transfer --json nameWithOwner,url,viewerPermission
git remote -v
git status --short --branch
git diff --check
git diff --cached --name-only
git diff --cached
git ls-files
git commit -m "Describe reviewed change"
git push -u origin HEAD
```

Replace `OWNER` only after checking the authenticated GitHub identity and repository permissions. If the repository does not exist and the authenticated account permits creation, use `gh repo create HPG-ATHENA-Transfer --private --source . --remote origin --push` after reviewing staged files and committing. If `gh` or GitHub authentication is unavailable, leave the local project ready and tell the user exactly which identity, permission, or tool is missing. Never invent an owner or place a token in a command, URL, handoff, or Git remote.
