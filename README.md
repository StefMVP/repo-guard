# repo-guard

repo-guard keeps credentials, private names and personal paths out of a Git
repository, and audits a private repository before you make it public.

It uses [Betterleaks](https://github.com/betterleaks/betterleaks) to find
credentials. It adds your privacy policy, Git hooks, a `gh` wrapper and a
release audit of Git history and GitHub content.

```console
$ git commit -m "Add sync settings"
github-pat: src/settings.py:1
personal-path: README.md:2
personal-path: use /home/example/, $HOME or <home> in public text.
Credential: remove it. For a fake test value, run repo-guard check --json, then repo-guard approve-finding --fingerprint HASH --note REASON.
```

Each finding names the rule, file and line, never the matched value. A clean
result does not find private names that are not in your policy.

## Requirements

- Linux or macOS, Python 3.11 or newer, Git 2.54 or newer
- An authenticated [GitHub CLI](https://cli.github.com/) for GitHub audits

## Install

```sh
git clone https://github.com/StefMVP/repo-guard
cd repo-guard
./bin/repo-guard install
```

`install` downloads Betterleaks 1.9.0, checks its pinned SHA-256 checksum and
links `repo-guard` into `~/.local/bin`. It links the `public-repo` agent skill
into `~/.claude/skills` and `~/.codex/skills` when they exist
(`--skills-dir DIR` selects other folders).

## Configure

`install` copies the [example policy](examples/policy.toml) to
`~/.config/repo-guard/policy.toml` if you have none. Add your private names,
domains, email addresses and GitHub owner. The example explains each field.
`betterleaks.toml` in the same folder extends the Betterleaks default rules.

| Item | Default location | Change it with |
| --- | --- | --- |
| Policy and scanner rules | `~/.config/repo-guard/` | `REPO_GUARD_CONFIG` or `XDG_CONFIG_HOME` |
| Audits, reviews and approvals | `~/.local/state/repo-guard/` | `REPO_GUARD_STATE` or `XDG_STATE_HOME` |
| Scanner binary | `~/.local/share/repo-guard/betterleaks-1.9.0` | `REPO_GUARD_BINARY` |

## Use

Turn on the hooks in each clone:

```sh
repo-guard enable --repo path/to/project --profile candidate
```

| Profile | Checks |
| --- | --- |
| `candidate` | Credentials, private names, domains, emails, repository links, personal and worktree paths, sensitive file names and new binary files. Use it for a public repository or one that you will publish. |
| `private` | Credentials and sensitive file names. Use it for a private repository. |

The hooks check staged files, the commit message and each pushed commit. A
finding or a scan error stops the operation. Existing hooks continue to run.
`git commit --no-verify` skips them.

Each finding prints its fix. To approve a fake test value or a reviewed image:

```sh
repo-guard check --repo . --json
repo-guard approve-finding --repo . --fingerprint HASH --note 'Fake token in a test'
repo-guard approve-binary docs/screen.png --repo . --note 'Sample data only'
```

An approval covers only that exact value and source.

### Check text before GitHub posts it

The hooks do not see pull request text, comments, release notes or gists. Put
the `gh` wrapper first in your `PATH`:

```sh
export PATH="/path/to/repo-guard/shims:$PATH"
```

It checks the text and files of `pr`, `issue`, `release`, `repo` and `gist`
create and edit commands before `gh` posts them. Text for a public target gets
the full check; text for a private target gets the credential check. It does
not check `gh api`. To check a file yourself, run `repo-guard check-text FILE`.

### Publish a private repository

```sh
repo-guard prepare-public --repo path/to/project
repo-guard review --repo path/to/project --decision ready --report review.html
repo-guard publish --repo path/to/project --confirm OWNER/REPO
```

1. `prepare-public` scans all refs, pull request heads, commit metadata,
   GitHub issues, pull requests, releases, Actions logs and artifacts. It
   changes nothing on GitHub. It writes a private report at
   `reports/report.html` with the manual review items.
2. `review` records your HTML code review. `ready` needs zero findings. A later
   change to code, refs, GitHub content or policy needs a new audit.
3. `publish` audits again, makes the repository public only if the result
   matches the ready review, and turns on GitHub secret scanning and push
   protection. If it cannot confirm them, the repository becomes private again.

To publish without the old history, `repo-guard clean-history` makes a copy
with one new commit. See `repo-guard clean-history --help`.

The [public-repo skill](skills/public-repo/SKILL.md) gives an agent these steps
and the review criteria.

## Limits

- repo-guard supports only GitHub remotes.
- The audit reads text. Images, the wiki, discussions and Pages need a manual
  review. The report lists them.

## Development

```sh
./bin/repo-guard install   # The tests use the real pinned scanner.
python3 -m unittest discover -p 'test_*.py'
```

## License

[MIT](LICENSE). Betterleaks is a separate MIT project. repo-guard downloads its
release binary and does not include its source.
