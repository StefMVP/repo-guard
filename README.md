# repo-guard

repo-guard keeps credentials, private names and personal paths out of a Git
repository. It also gives you a reviewed path from a private repository to a
public one.

It uses [Betterleaks](https://github.com/betterleaks/betterleaks) to find
credentials. It adds your own privacy policy, Git hooks that stop on a finding
or a scan error, and a release audit of Git history and GitHub content.

```console
$ git commit -m "Add sync settings"
github-pat: src/settings.py:1
private-name: README.md:1
personal-path: README.md:2
```

Each line names the rule, file and line. The output never shows the matched value.
These are credential and policy checks. A clean result still needs a review of
project-specific content. Unknown private names can pass the configured policy.

## Requirements

- Linux or macOS
- Python 3.11 or newer
- Git 2.54 or newer
- An authenticated [GitHub CLI](https://cli.github.com/) for GitHub audits and publication

## Install

```sh
git clone https://github.com/StefMVP/repo-guard
cd repo-guard
./bin/repo-guard install
```

`install` downloads Betterleaks 1.9.0 and checks its pinned SHA-256 checksum.
It links `repo-guard` into `~/.local/bin`. If you have no policy, it copies the
[example policy](examples/policy.toml) to `~/.config/repo-guard/policy.toml`.
Edit that file. Add your private names, domains, email addresses and GitHub owner.

`install` also links the `public-repo` agent skill into `~/.claude/skills` and
`~/.codex/skills` when those folders exist. Use `--skills-dir DIR` to select
other folders.

## Use

Turn on the checks in a repository:

```sh
repo-guard enable --repo path/to/project --profile candidate
```

| Profile | Checks |
| --- | --- |
| `candidate` | Credentials, private names, domains, emails, repository links, personal paths, worktree file paths, sensitive file names and changed binary files. Use it for a public repository or for one that you will publish. |
| `private` | Credentials and sensitive file names. Use it for a private repository that can contain its own private names. |

The profile is stored in the repository Git config. A checkout with the
`candidate` profile runs the full check in all hooks and in `status`, also
while its GitHub repository is private. Use it for a private repository that
you plan to publish. `enable --profile private` refuses to lower a `candidate`
checkout. Add `--allow-private` to lower it.

The hooks check the staged files before a commit, the commit message, and each
outgoing commit before a push. A scan error also stops the operation. Existing
hooks and hook managers continue to run. Run `enable` again in each new clone.

The `worktree-path` rule detects paths with a concrete checkout name and a child
path under `.worktrees`, including Windows paths. It needs no private-name list.
Use `example-*`, `sample-*`, `demo-*`, `<checkout>`, or variable placeholders in
public examples. The short examples `feature`, `feature-a`, and `feature-b` are
also allowed. A checkout directory argument alone is allowed. Keep real local
paths and project-specific settings in configuration outside the public tree.
After a privacy finding, review the complete change before you retry.

To run the staged check yourself, use `repo-guard check --repo path/to/project`.
Add `--mode history` to check all history.

A finding can be an intentional test value or a reviewed image:

```sh
repo-guard approve-finding --repo . --fingerprint HASH --note 'Fake token in a test'
repo-guard approve-binary docs/screen.png --repo . --note 'Sample data only'
```

Take `HASH` from the audit file that `prepare-public` names. An approval applies
only to that exact value or file content. You cannot approve private names or
personal paths. Change the content instead.

## Check text before GitHub posts it

The Git hooks do not see pull request descriptions, comments, release notes or
gists. The `gh` wrapper in `shims/` checks that text before `gh` posts it. Put
it first in your `PATH`:

```sh
export PATH="/path/to/repo-guard/shims:$PATH"
```

It checks the titles, bodies, notes, descriptions, topics and files that these
commands post: `pr create|edit|comment|review|merge`, `issue create|edit|comment`,
`release create|edit`, `repo create|edit` and `gist create|edit`. Other `gh`
commands run unchanged. The wrapper does not create a public repository or
change a repository to public. Use `repo-guard publish` for that.

A public repository, or a checkout with the `candidate` profile, gets the full
check. A private repository gets the credential check. A public gist gets the
full check. A secret gist gets the credential check, because only the people
who get its link can read it. When a post links a gist, the wrapper also reads
the gist and gives it the same check as the post. A finding or a failed check
stops the post.

To check text yourself, use `repo-guard check-text notes.md`. Use `-` to read
standard input. Add `--profile private` to check only for credentials, and
`--linked-gists` to also check the gists that the text links.

## Publish a private repository

1. Audit the repository and its GitHub content:

   ```sh
   repo-guard prepare-public --repo path/to/project
   ```

   This scans all branches, tags, pull request heads and commit metadata. It
   reads GitHub issues, pull requests, releases, Actions logs and artifacts.
   It turns on the `candidate` hooks in a clone that has no profile; a
   `private` profile stays during cleanup. It changes nothing on GitHub. It
   writes a private HTML report at `reports/report.html`, which Git ignores.
   The report also lists hardcoded paths, hosts, accounts, commands and limits
   that another user can need to change.

2. Review the code and the manual checks in the report. Write your review as an
   HTML file and record it:

   ```sh
   repo-guard review --repo path/to/project --decision ready --report review.html
   ```

   `ready` needs zero automated findings. A later change to the code, refs,
   GitHub content or policy needs a new audit and review.

3. Make the repository public:

   ```sh
   repo-guard publish --repo path/to/project --confirm OWNER/REPO
   ```

   This runs the audit again. It changes visibility only if the result matches
   the ready review and `HEAD` matches the pushed default branch. Then it turns
   on GitHub secret scanning and push protection and confirms both. Without
   that confirmation, the repository becomes private again. The clone gets the
   `candidate` profile. An audit of a public repository with secret scanning
   off reports a finding.

To publish without the old history, first make a copy with one new commit:

```sh
repo-guard clean-history --repo path/to/project --destination path/to/project/.worktrees/public-source --confirm OWNER/REPO
repo-guard prepare-public --repo path/to/project/.worktrees/public-source --local-only --target OWNER/NEW_REPO
```

The source must be a clean, private GitHub repository. The copy contains only
the current tracked files and the source's local Git excludes. Its commit uses
your GitHub noreply email address. The original repository does not change.
A local-only audit does not read GitHub, so run the full audit after you
create and push the new repository.

The [public-repo skill](skills/public-repo/SKILL.md) gives an agent these steps
and the [release review criteria](skills/public-repo/references/release-review.md).

## Configuration

| Item | Default location | Change it with |
| --- | --- | --- |
| Policy and scanner rules | `~/.config/repo-guard/` | `REPO_GUARD_CONFIG` or `XDG_CONFIG_HOME` |
| Audits, reviews and approvals | `~/.local/state/repo-guard/` | `REPO_GUARD_STATE` or `XDG_STATE_HOME` |
| Scanner binary | `~/.local/share/repo-guard/betterleaks-1.9.0` | `REPO_GUARD_BINARY` |

`betterleaks.toml` extends the Betterleaks default rules. Inline
`betterleaks:allow` comments and ignore files in the repository cannot skip a
check. Network validation of credentials is off.

## Limits

- Hooks run only in clones where you ran `enable`. `git commit --no-verify`
  skips them. They do not check edits that you make on GitHub.
- The `gh` wrapper does not check `gh api` requests, text that you type in the
  `gh` editor, or release asset files. A visibility change on the GitHub
  website does not use `publish`.
- repo-guard supports only GitHub remotes.
- The audit reads text. Images, archives, the wiki, discussions and Pages need
  a manual review. The report lists them.
- The audit reads Actions logs and artifacts up to 64 MiB each. A larger or
  binary archive stops the audit until you review or delete it.
- GitHub secret scanning and push protection are a good second check. They do
  not know your private names.

## Development

```sh
./bin/repo-guard install   # The tests use the real pinned scanner.
python3 -m unittest discover -p 'test_*.py'
```

## License

[MIT](LICENSE). Betterleaks is a separate MIT project. repo-guard downloads its
release binary and does not include its source.
