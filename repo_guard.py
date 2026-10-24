"""Local checks and an explicit private-to-public release gate. Python 3.11+."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import report_html

ROOT = Path(__file__).resolve().parent
CLI = ROOT / 'bin/repo-guard'
EXAMPLES = ROOT / 'examples'
SKILL = ROOT / 'skills/public-repo'
SHIM = ROOT / 'shims/gh'
VERSION = '1.9.0'
HASHES = {
    ('Linux', 'x86_64'): ('linux_x64', 'f8b185a39ffcece2a1ca82bf3a4e7435cd81963ffd16b7a9128daf75f35f6de7'),
    ('Linux', 'aarch64'): ('linux_arm64', '1d39116e0a58dc94574715e2aa12a2dbd5062f193eee3fec011fef6ba06bd13b'),
    ('Darwin', 'arm64'): ('darwin_arm64', '83dd7eaab13d44a1e8e347a17064cdef153a1478db473276d41b38d55613d3f2'),
    ('Darwin', 'x86_64'): ('darwin_x64', '68dfd83458d9e7f90663daa7a81c7f944083b170dae82e4f5f8d0581ed519ef1'),
}


class GuardError(Exception):
    pass


# A public repository keeps GitHub's own credential checks on.
SCANNING = {'secret_scanning': {'status': 'enabled'}, 'secret_scanning_push_protection': {'status': 'enabled'}}


def scanning_off(repository):
    state = repository.get('security_and_analysis') or {}
    return [key for key in SCANNING if (state.get(key) or {}).get('status') != 'enabled']


def run(args, repo=None, data=None, allowed=(0,), env=None):
    # A child gets no terminal input: a prompt must fail, not wait.
    result = subprocess.run([str(arg) for arg in args], cwd=repo, input=data,
                            stdin=subprocess.DEVNULL if data is None else None,
                            capture_output=True, env=env)
    if result.returncode not in allowed:
        # Tool output can contain credentials. Keep it out of hook and CI logs.
        raise GuardError(f'{Path(str(args[0])).name} failed (exit {result.returncode}). No scan result was accepted.')
    return result


def git(repo, *args, allowed=(0,)):
    return run(['git', *args], repo, allowed=allowed).stdout


def repo_root(repo):
    return Path(git(repo, 'rev-parse', '--show-toplevel').decode().strip()).resolve()


def config_dir():
    if os.environ.get('REPO_GUARD_CONFIG'):
        return Path(os.environ['REPO_GUARD_CONFIG'])
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'repo-guard'


def policy():
    path = config_dir() / 'policy.toml'
    if not path.is_file():
        raise GuardError(f'No policy at {path}. Run repo-guard install.')
    return tomllib.loads(path.read_text())


def policy_hash():
    digest = hashlib.sha256()
    for item in sorted(config_dir().glob('*.toml')):
        digest.update(item.name.encode() + b'\0' + item.read_bytes())
    digest.update((ROOT / 'repo_guard.py').read_bytes())
    digest.update((ROOT / 'report_html.py').read_bytes())
    for item in sorted(SKILL.rglob('*.md')):
        digest.update(item.relative_to(ROOT).as_posix().encode() + b'\0' + item.read_bytes())
    digest.update(VERSION.encode())
    return digest.hexdigest()


def state_dir(repo):
    identity = str(Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()))
    base = Path(os.environ.get('REPO_GUARD_STATE') or
                Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'repo-guard')
    directory = base / hashlib.sha256(identity.encode()).hexdigest()[:20]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as target:
        json.dump(value, target, indent=2)
        target.write('\n')
    temporary.replace(path)


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def binary():
    item = Path(os.environ.get('REPO_GUARD_BINARY', Path.home() / f'.local/share/repo-guard/betterleaks-{VERSION}'))
    if not item.is_file() or not os.access(item, os.X_OK):
        raise GuardError('Betterleaks is missing. Run repo-guard install.')
    return item


def install(skill_dirs=None):
    import platform
    import tarfile
    import urllib.request
    target = Path.home() / f'.local/share/repo-guard/betterleaks-{VERSION}'
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        selected = HASHES.get((platform.system(), platform.machine()))
        if not selected:
            raise GuardError('No pinned binary for this platform.')
        archive_name, expected = selected
        url = f'https://github.com/betterleaks/betterleaks/releases/download/v{VERSION}/betterleaks_{VERSION}_{archive_name}.tar.gz'
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise GuardError('Betterleaks download checksum does not match the pinned checksum.')
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            member = next(m for m in archive.getmembers() if m.isfile() and Path(m.name).name == 'betterleaks')
            payload = archive.extractfile(member).read()
        target.write_bytes(payload)
        target.chmod(0o700)
    actual = run([target, 'version']).stdout.decode().strip()
    if actual != VERSION:
        raise GuardError('Installed Betterleaks version does not match the pinned version.')
    # Link the agent skill into each agent skill folder that exists, or only
    # into the folders the user selected.
    if skill_dirs is None:
        skill_dirs = [item for item in (Path.home() / '.claude/skills', Path.home() / '.codex/skills') if item.is_dir()]
    links = [(Path.home() / '.local/bin/repo-guard', CLI)]
    links += [(Path(item).expanduser() / 'public-repo', SKILL) for item in skill_dirs]
    for link, source in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and link.resolve() == source.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise GuardError(f'Installation path is in use: {link}')
        link.symlink_to(source)
    config = config_dir()
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    for example in sorted(EXAMPLES.glob('*.toml')):
        if not (config / example.name).exists():
            shutil.copyfile(example, config / example.name)
            (config / example.name).chmod(0o600)
            print(f'Created {config / example.name} from the example.' +
                  (' Add your private names before use.' if example.name == 'policy.toml' else ''))
    print(f'Installed repo-guard and Betterleaks {VERSION}.')
    for link, _ in links[1:]:
        print(f'Linked the public-repo skill at {link}.')


def write_report(repo, audit):
    state = state_dir(repo)
    destination = repo / 'reports/report.html'
    if git(repo, 'ls-files', '--', 'reports/report.html').strip():
        # A generated review must not replace an existing tracked document.
        destination = state / 'report.html'
    else:
        exclude = Path(git(repo, 'rev-parse', '--git-path', 'info/exclude').decode().strip())
        if not exclude.is_absolute():
            exclude = repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        content = exclude.read_text() if exclude.exists() else ''
        if '/reports/report.html' not in content.splitlines():
            with exclude.open('a') as target:
                target.write('\n/reports/report.html\n')
    record = load(state / 'review.json', {})
    source = state / 'review.html'
    valid = (record.get('snapshot') == audit['snapshot'] and source.is_file() and
             hashlib.sha256(source.read_bytes()).hexdigest() == record.get('report_sha256'))
    document = report_html.render(audit, source.read_text() if valid else None,
                                  record.get('decision') if valid else None)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(document)
    destination.chmod(0o600)
    return str(destination)


def finding(rule, path, line=0, commit=''):
    return {'rule': rule, 'path': path, 'line': line, 'commit': commit}


def public_profile(repo):
    return git(repo, 'config', '--get', 'repoGuard.profile', allowed=(0, 1)).strip() == b'candidate'


def privacy(text, path, config=None):
    config = config or policy()
    output = []
    # On ASCII input, cheap literal searches avoid regex scans for absent terms.
    # Keep the regex path for Unicode so case-insensitive matching stays intact.
    lower = text.lower() if text.isascii() else None
    patterns = [('private-name', re.escape(term)) for term in config.get('private_terms', [])
                if lower is None or term.lower() in lower]
    # A word needs no letter or digit before it and no lowercase letter or
    # digit after it, so "Falcon" matches "FalconBot" but not "falconry".
    patterns += [('private-name', rf'(?<![A-Za-z0-9]){re.escape(term)}(?-i:(?![a-z0-9]))')
                 for term in config.get('private_words', []) if lower is None or term.lower() in lower]
    patterns += [('private-domain', re.escape(term)) for term in config.get('private_domains', [])
                 if lower is None or term.lower() in lower]
    patterns += [('private-email', re.escape(term)) for term in config.get('private_emails', [])
                 if lower is None or term.lower() in lower]
    owner = re.escape(config.get('private_repo_owner', ''))
    if owner and (lower is None or 'github.com' in lower):
        # Subdomains such as gist.github.com link no repository.
        patterns += [('private-repo-url', rf'(?:(?<![\w.-])|(?<=www\.))github\.com[:/]{owner}/[A-Za-z0-9_.-]+')]
    patterns += [('personal-path', item) for item in config['personal_path_patterns']]
    # A checkout name plus a child path can expose private task and source names
    # even when the policy has no matching brand name. Directory arguments alone
    # are common in tool instructions. Keep explicit documentation placeholders.
    patterns += [('worktree-path',
                  r'(?<![\w.-])\.worktrees[\\/]+(?!(?:(?:example|sample|demo)(?:[-_][a-z0-9_.-]+)?'
                  r'|feature(?:-[ab])?)[\\/])[a-z0-9][a-z0-9_.-]*[\\/]+[a-z0-9_.-]+')]
    seen = set()
    for name, pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            if (name == 'private-repo-url' and config.get('public_repository') and
                match.group().replace('github.com:', 'github.com/').lower().removesuffix('.git')
                    == ('github.com/' + config['public_repository']).lower()):
                continue
            line_number = text.count('\n', 0, match.start()) + 1
            if (name, line_number) not in seen:
                output.append(finding(name, path, line_number))
                seen.add((name, line_number))
    return output


def sensitive_name(name):
    leaf = Path(name).name.lower()
    if leaf == '.env' or (leaf.startswith('.env.') and not re.search(r'\.(?:example|sample|template)$', leaf)):
        return True
    return bool(re.search(r'^(?:id_(?:rsa|ed25519|ecdsa)|.*(?:private[-_]?key|credentials)\.(?:pem|key|json)|.*\.(?:p12|pfx))$', leaf))


def scoped_policy(repo):
    config = policy()
    try:
        config['public_repository'] = github_name(repo)
    except GuardError:
        planned = git(repo, 'config', '--get', 'repoGuard.publicRepository', allowed=(0, 1)).decode().strip()
        if planned:
            config['public_repository'] = planned
    return config


def scanner(repo, mode, revisions=None, data=None, label='metadata'):
    with tempfile.TemporaryDirectory(prefix='repo-guard-scan-') as directory:
        report = Path(directory) / 'findings.json'
        # The report lives only inside this 0700 temporary directory. Hash the
        # exact value before discarding it; scanner-side redaction would make
        # different credentials share the same exception fingerprint. Never
        # emit this report or the captured scanner output.
        command = [binary(), 'stdin' if data is not None else 'git', '--no-banner',
                   '--ignore-gitleaks-allow', '--validation=false', '--config', config_dir() / 'betterleaks.toml',
                   '--gitleaks-ignore-path', config_dir(),
                   '--report-format', 'json', '--report-path', report]
        if data is None:
            if mode == 'staged':
                command += ['--pre-commit', '--staged']
            else:
                command += ['--log-opts=' + shlex.join(revisions or ['--all', '--full-history', '-m'])]
        result = run(command, repo, data=data, allowed=(0, 1))
        if not report.is_file():
            raise GuardError('Betterleaks did not write its result.')
        raw = json.loads(report.read_text())
        if result.returncode == 1 and not raw:
            raise GuardError('Betterleaks returned a failure without findings.')
        exceptions = load(state_dir(repo) / 'scanner-exceptions.json', {}) if repo else {}
        output = []
        for item in raw:
            name = label if data is not None else item.get('File', '')
            # Bind an exception to its rule, source and exact value. A changed
            # credential on the same line must never inherit a test-data exception.
            fingerprint = hashlib.sha256('\0'.join([item['RuleID'], name, item['Secret']]).encode()).hexdigest()
            if fingerprint in exceptions:
                continue
            record = finding(item['RuleID'], name, item.get('StartLine', 0), item.get('Commit', ''))
            record['fingerprint'] = fingerprint
            output.append(record)
        return output


def privacy_history(text, config):
    output = []
    commit = ''
    path = 'commit message'
    old_line = new_line = 0
    in_diff = False
    for line in text.splitlines():
        if re.fullmatch(r'guard-commit:[0-9a-f]{40,64}', line):
            commit = line.split(':', 1)[1]
            path = 'commit message'
            in_diff = False
            continue
        if line.startswith('diff --git '):
            in_diff = True
            old_line = new_line = 0
            continue
        if line.startswith('--- a/'):
            path = line[6:]
            continue
        if line.startswith('+++ b/'):
            path = line[6:]
            for item in privacy(path, 'Git path', config):
                item['commit'] = commit
                output.append(item)
            continue
        hunk = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if hunk:
            old_line, new_line = map(int, hunk.groups())
            continue
        number = 0
        content = line
        if in_diff:
            if line.startswith('+'):
                number = new_line
                new_line += 1
            elif line.startswith('-'):
                number = old_line
                old_line += 1
            elif line.startswith(' '):
                number = new_line
                new_line += 1
                old_line += 1
            else:
                continue
            content = line[1:]
        for item in privacy(content, path, config):
            item.update(line=number, commit=commit)
            output.append(item)
    return output


def binary_approved(repo, blob):
    return blob in load(state_dir(repo) / 'binary-reviews.json', {})


def staged_blobs(repo, names):
    if not names:
        return []
    index = git(repo, '--literal-pathspecs', 'ls-files', '--stage', '-z', '--', *[os.fsdecode(name) for name in names])
    objects = {}
    for entry in index.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        _, object_id, stage = metadata.split()
        if stage != b'0':
            raise GuardError('Resolve index conflicts before the scan.')
        objects[name] = object_id
    request = b'\n'.join(objects[name] for name in names) + b'\n'
    data = run(['git', 'cat-file', '--batch'], repo, data=request).stdout
    stream = io.BytesIO(data)
    output = []
    for name in names:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b'blob':
            raise GuardError('A changed non-file Git entry needs a separate review.')
        size = int(header[2])
        content = stream.read(size)
        if len(content) != size or stream.read(1) != b'\n':
            raise GuardError('Git did not return the complete staged file.')
        output.append((os.fsdecode(name), content))
    return output


def tag_metadata(repo, object_id):
    output = []
    while git(repo, 'cat-file', '-t', object_id).strip() == b'tag':
        value = git(repo, 'cat-file', 'tag', object_id)
        output.append(value)
        object_id = value.splitlines()[0].removeprefix(b'object ').decode()
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', object_id):
            raise GuardError('A tag has an invalid target object.')
    return b'\n'.join(output)


def check(repo, mode, revisions=None, config=None):
    public = public_profile(repo)
    config = config or scoped_policy(repo)
    output = scanner(repo, mode, revisions)
    if mode == 'staged':
        names = [name for name in git(repo, 'diff', '--cached', '--name-only', '--diff-filter=ACMRT', '-z').split(b'\0') if name]
        contents = staged_blobs(repo, names) if public else [(os.fsdecode(name), b'') for name in names]
        for name, content in contents:
            if sensitive_name(name):
                output.append(finding('sensitive-filename', name))
            if public:
                output += privacy(name, 'staged path', config)
                blob = hashlib.sha256(content).hexdigest()
                if b'\0' in content:
                    if not binary_approved(repo, blob):
                        output.append(finding('binary-review', name))
                else:
                    output += privacy(content.decode(errors='replace'), name, config)
    else:
        options = revisions or ['--all', '--full-history', '-m']
        # Refs and tags come first. A marker line starts each commit's metadata.
        metadata = git(repo, 'for-each-ref', '--format=%(refname)')
        if revisions is None:
            tags = git(repo, 'for-each-ref', '--format=%(objecttype) %(objectname)', 'refs/tags')
            for tag in tags.splitlines():
                kind, object_id = tag.split()
                if kind == b'tag':
                    metadata += tag_metadata(repo, object_id.decode()) + b'\n'
        metadata += git(repo, 'log', *options, '--format=guard-commit:%H%n%B%n%an <%ae>', '--name-only', '--no-ext-diff')
        text = metadata.decode(errors='replace')
        found = scanner(repo, mode, data=metadata, label='commit metadata')
        if public:
            patch = git(repo, '-c', 'core.quotePath=false', 'log', *options, '-p',
                        '--format=guard-commit:%H%n%B', '--no-ext-diff', '--no-textconv')
            output += privacy_history(patch.decode(errors='replace'), config)
            found += privacy(text, 'commit metadata', config)
            found += [finding('sensitive-filename-history', name, number)
                      for number, name in enumerate(text.splitlines(), 1) if sensitive_name(name)]
        output += metadata_sources(found, text)
    # Keep the report small without hiding distinct locations.
    return list({json.dumps(item, sort_keys=True): item for item in output}.values())


def metadata_sources(findings, text):
    """Give each metadata finding its commit and its line in that commit's metadata."""
    sources = []
    commit, start = '', 0
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.fullmatch(r'guard-commit:([0-9a-f]{40,64})', line)
        if marker:
            commit, start = marker[1], number
        sources.append((commit, number - start))
    for item in findings:
        if 0 < item['line'] <= len(sources):
            commit, line = sources[item['line'] - 1]
            item.update(commit=commit, line=line)
            if item['path'] == 'commit metadata' and not commit:
                item['path'] = 'ref and tag metadata'
    return findings


EVENTS = {'pre-commit': 'staged', 'commit-msg': 'message', 'pre-push': 'push'}


def enable(repo, profile, allow_private=False):
    version = git(repo, '--version').decode()
    numbers = re.search(r'(\d+)\.(\d+)', version)
    if not numbers or tuple(map(int, numbers.groups())) < (2, 54):
        raise GuardError('Hook installation needs Git 2.54 or newer. Existing hooks were preserved.')
    if profile == 'private' and public_profile(repo) and not allow_private:
        raise GuardError('This checkout has the candidate profile. Add --allow-private to lower it to private.')
    git(repo, 'config', '--local', 'repoGuard.profile', profile)
    for event in EVENTS:
        key = 'hook.repo-guard-' + event
        git(repo, 'config', '--local', key + '.event', event)
        git(repo, 'config', '--local', key + '.command', shlex.join([str(CLI), 'hook', event]))
    return {'profile': profile, 'hooks': list(EVENTS)}


def hook(repo, event, args, stdin):
    if event == 'pre-commit':
        return check(repo, 'staged')
    if event == 'commit-msg':
        if not args:
            raise GuardError('Commit message path is missing.')
        data = Path(args[0]).read_bytes()
        result = scanner(repo, 'staged', data=data, label='commit message')
        return result + (privacy(data.decode(errors='replace'), 'commit message', scoped_policy(repo)) if public_profile(repo) else [])
    if len(args) < 2:
        raise GuardError('Push remote arguments are missing.')
    for line in stdin.decode().splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise GuardError('Push update format is invalid.')
        _, local, _, remote = fields
        if not all(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha) for sha in (local, remote)):
            raise GuardError('Push object ID is invalid.')
    # Scan each update on its own: a new ref's --not must not exclude another ref.
    output = []
    for line in stdin.decode().splitlines():
        _, local, _, remote = line.split()
        if set(local) == {'0'}:
            continue
        opts = [f'{remote}..{local}'] if set(remote) != {'0'} else [local, '--not', '--remotes=' + args[0]]
        output += check(repo, 'history', revisions=opts)
        annotation = tag_metadata(repo, local)
        if annotation:
            output += scanner(repo, 'history', data=annotation, label='tag metadata')
            if public_profile(repo):
                output += privacy(annotation.decode(errors='replace'), 'tag metadata', scoped_policy(repo))
    return output


def require_clean(repo):
    if git(repo, 'status', '--porcelain', '--untracked-files=normal').strip():
        raise GuardError('Commit or remove pending work before the public release audit.')


def clean_history(repo, destination, confirm):
    """Copy the current tracked tree into one new commit, only on request."""
    require_clean(repo)
    name = github_name(repo)
    if confirm.lower() != name.lower():
        raise GuardError('Confirm the source OWNER/REPO for the requested history removal.')
    if not gh_json(repo, 'repos/' + name)['private']:
        raise GuardError('This first-release command requires a private source repository.')
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise GuardError('Use a new destination directory for the clean history.')
    destination = destination.resolve()
    # A nested checkout must be in the source's ignored workspace directory.
    if repo in destination.parents:
        workspaces = repo / '.worktrees'
        if (not destination.is_relative_to(workspaces) or
                run(['git', 'check-ignore', '-q', '--', '.worktrees/'], repo, allowed=(0, 1)).returncode):
            raise GuardError('Use an ignored .worktrees directory or a separate destination.')
    entries = []
    for entry in git(repo, 'ls-tree', '-rz', 'HEAD').split(b'\0'):
        if not entry:
            continue
        metadata, filename = entry.split(b'\t', 1)
        mode, kind, _ = metadata.split()
        if kind != b'blob' or mode not in (b'100644', b'100755', b'120000'):
            raise GuardError('Review submodules separately before making a clean release copy.')
        parts = Path(os.fsdecode(filename))
        if parts.is_absolute() or '..' in parts.parts or any(part.lower() == '.git' for part in parts.parts):
            raise GuardError('A tracked path cannot be copied into the release checkout.')
        entries.append((filename, mode))
    blobs = dict(staged_blobs(repo, [filename for filename, _ in entries]))
    identity = gh_json(repo, 'user')
    login = identity['login']
    email = f'{identity["id"]}+{login}@users.noreply.github.com'
    destination.mkdir(parents=True, mode=0o700)
    git(destination, 'init', '-q', '--initial-branch=main')
    # Local excludes, such as tool folders of this machine, apply to the copy too.
    exclude = Path(git(repo, 'rev-parse', '--git-path', 'info/exclude').decode().strip())
    exclude = exclude if exclude.is_absolute() else repo / exclude
    if exclude.is_file():
        (destination / '.git/info/exclude').write_text(exclude.read_text())
    for filename, mode in entries:
        target = destination / os.fsdecode(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = blobs[os.fsdecode(filename)]
        if mode == b'120000':
            target.symlink_to(os.fsdecode(content))
        else:
            target.write_bytes(content)
            target.chmod(0o755 if mode == b'100755' else 0o644)
    # Add only the original tracked paths, including files now covered by an
    # ignore rule. Ignored and untracked source files are never copied.
    for offset in range(0, len(entries), 100):
        git(destination, '--literal-pathspecs', 'add', '-f', '--',
            *[os.fsdecode(filename) for filename, _ in entries[offset:offset + 100]])
    tree = git(destination, 'write-tree').strip()
    if tree != git(repo, 'rev-parse', 'HEAD^{tree}').strip():
        raise GuardError('The release tree differs from the source. Inspect the new copy before use.')
    git(destination, 'config', 'user.name', login)
    git(destination, 'config', 'user.email', email)
    # commit-tree has no parent argument and does not copy old Git objects,
    # refs, messages, author identities, remotes or repository configuration.
    identity_env = dict(os.environ, GIT_AUTHOR_NAME=login, GIT_AUTHOR_EMAIL=email,
                        GIT_COMMITTER_NAME=login, GIT_COMMITTER_EMAIL=email)
    commit = run(['git', '-c', 'commit.gpgsign=false', 'commit-tree', tree.decode(), '-m', 'Initial commit'],
                 destination, env=identity_env).stdout.strip()
    git(destination, 'update-ref', 'refs/heads/main', commit.decode())
    enable(destination, 'candidate')
    record = {'source_repository': name, 'source_head': git(repo, 'rev-parse', 'HEAD').decode().strip(),
              'destination': str(destination), 'head': commit.decode(), 'tracked_files': len(entries),
              'needs_release_review': True}
    save(state_dir(destination) / 'clean-source.json', record)
    return record


def github_name(repo):
    remote = git(repo, 'remote', 'get-url', 'origin').decode().strip()
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', remote)
    if not match:
        raise GuardError('origin must be a GitHub repository URL without credentials.')
    return match.group(1)


def gh_json(repo, endpoint, pages=False):
    args = ['gh', 'api', endpoint]
    if pages:
        args += ['--paginate', '--slurp']
    return json.loads(run(args, repo).stdout)


def github_surface(repo):
    name = github_name(repo)
    result = {'repository': gh_json(repo, 'repos/' + name), 'resources': {}}
    # This request-scoped credential is neither public content nor stable metadata.
    result['repository'].pop('temp_clone_token', None)
    for resource in ('issues?state=all', 'issues/comments', 'pulls/comments', 'comments', 'releases',
                     'actions/runs', 'actions/artifacts'):
        separator = '&' if '?' in resource else '?'
        pages = gh_json(repo, f'repos/{name}/{resource}{separator}per_page=100', pages=True)
        result['resources'][resource] = pages
    # Review summaries are not review comments; read them for each pull request.
    pulls = [item['number'] for page in result['resources']['issues?state=all'] for item in page
             if 'pull_request' in item]
    result['resources']['pulls/reviews'] = {
        str(number): gh_json(repo, f'repos/{name}/pulls/{number}/reviews?per_page=100', pages=True)
        for number in pulls}
    return result


def snapshot(repo, surface):
    parts = [git(repo, 'rev-parse', 'HEAD'), git(repo, 'for-each-ref', '--format=%(refname) %(objectname)'),
             git(repo, 'config', '--get', 'repoGuard.publicRepository', allowed=(0, 1)),
             policy_hash().encode(), json.dumps(surface, sort_keys=True).encode(),
             json.dumps(load(state_dir(repo) / 'scanner-exceptions.json', {}), sort_keys=True).encode(),
             json.dumps(load(state_dir(repo) / 'binary-reviews.json', {}), sort_keys=True).encode()]
    return hashlib.sha256(b'\0'.join(parts)).hexdigest()


def surface_text(value):
    if isinstance(value, dict):
        return '\n'.join(surface_text(item) for item in value.values())
    if isinstance(value, list):
        return '\n'.join(surface_text(item) for item in value)
    return value if isinstance(value, str) else ''


def archive_text(data):
    import zipfile
    # Read entries in memory. Never extract archive paths into the filesystem.
    result = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                total += entry.file_size
                if total > 64 * 1024 * 1024:
                    raise GuardError('A GitHub archive is too large for this audit. Review or remove it first.')
                if not entry.is_dir():
                    content = archive.read(entry)
                    if b'\0' in content:
                        raise GuardError('A GitHub archive contains binary data. Review or remove it first.')
                    result.append(entry.filename.encode() + b'\n' + content)
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise GuardError('A GitHub archive could not be read completely.') from error
    return b'\n'.join(result)


MANUAL_SURFACES = ['Repository architecture and configuration',
                   'Configuration and portability: hardcoded identities, paths, hosts, tools and tunable numbers',
                   'README clarity, setup, usage, links and writing',
                   'GitHub description, relevant topics and optional website',
                   'Useful screenshots or example output with public sample data',
                   'License and contribution rights',
                   'Documented local checks, installation and package contents',
                   'Images and historical media', 'Wiki, discussions, attachments, and Pages']

# Values that a new user can need to change. The list is a review aid, not a
# blocking finding: the reviewer keeps, infers, configures or documents each one.
SETTING_END = r'''[^\s'"`<>()\[\]{},;]'''
SETTINGS = [
    ('path', rf'(?<![\w.:/~$-])/(?:home|Users|opt|etc|var|srv|mnt|media|tmp|root|usr/local)(?:/{SETTING_END}*)*'),
    ('path', r'''(?<![\w])[A-Za-z]:\\[^\s'"<>]+'''),
    ('home-path', rf'(?:~|\$HOME|\$\{{HOME\}})/{SETTING_END}+'),
    ('home-path', r'''Path\.home\(\)\s*/\s*f?(['"])[^'"]+\1|expanduser\(\s*(['"])[^'"]+\2\)|os\.homedir\(\)'''),
    ('host', r'\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b'),
    ('host', r'\b(?:[a-z0-9-]+\.)+(?:local|lan|internal|home\.arpa|ts\.net)\b'),
    ('host', r'\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b'),
    ('url', r'''https?://[^\s'"`<>)\]},]+'''),
    ('email', r'(?<![\w.+:/-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b'),
    ('command', r'''\b(?:run|call|check_call|check_output|Popen|spawn|spawnSync|execFile|execFileSync|Command|execa)\(\s*\[?\s*['"]([\w.+-]+)['"]'''),
    ('constant', r'''^(?:export\s+)?(?:const\s+|readonly\s+|static\s+|final\s+)*([A-Z][A-Z0-9_]{2,})\s*(?::[^=\n]+)?=\s*['"\[{(\d-]'''),
    ('number', r'''(?i)\b(\w*(?:timeout|limit|max|min|size|port|retries|retry|interval|delay|ttl|threshold|batch|depth|days|hours|minutes|seconds|workers|jobs|concurrency)\w*)['"]?\s*[:=]\s*(\d[\d_.]*)\b(?!\s*\*)'''),
    ('number', r'\b\d+(?:\s*\*\s*1024)+\b'),
]
DOCUMENTATION_SETTINGS = {'path', 'home-path', 'host'}
DOCUMENTATION = re.compile(r'\.(?:md|markdown|rst|txt|adoc)$|(?:^|/)(?:LICEN[CS]E|COPYING|NOTICE)[^/]*$', re.I)
NOT_SETTINGS = re.compile(r'(?:^|/)(?:tests?|testdata|fixtures|__tests__|spec|vendor|node_modules)/|'
                          r'(?:^|/)(?:test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(?:test|spec)\.[^/]+|conftest\.py)$|'
                          r'(?:^|/)(?:go\.sum|[^/]*\.lock|package-lock\.json|pnpm-lock\.yaml)$', re.I)


def tree_files(repo, revision='HEAD', limit=1024 * 1024):
    entries = []
    for entry in git(repo, 'ls-tree', '-rzl', revision).split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, _, object_id, size = metadata.split()
        if mode in (b'100644', b'100755') and int(size) <= limit:
            entries.append((os.fsdecode(name), object_id))
    if not entries:
        return []
    stream = io.BytesIO(run(['git', 'cat-file', '--batch'], repo,
                            data=b'\n'.join(item for _, item in entries) + b'\n').stdout)
    output = []
    for name, _ in entries:
        size = int(stream.readline().split()[2])
        output.append((name, stream.read(size)))
        stream.read(1)
    return output


def setting_value(kind, match):
    if kind in ('command', 'constant'):
        return match.group(1)
    if kind == 'number' and match.lastindex == 2:
        return f'{match.group(1)}={match.group(2)}'
    value = match.group()
    if kind == 'url':
        # Keep the service location only. User information and query values
        # can hold credentials, and this list prints its values.
        value = re.sub(r'^(https?://)[^/@\s]*@', r'\1', value).split('?', 1)[0].split('#', 1)[0]
    return value[:160]


def configuration(repo):
    found = {}
    for name, content in tree_files(repo):
        if NOT_SETTINGS.search(name) or b'\0' in content:
            continue
        documentation = bool(DOCUMENTATION.search(name))
        for number, line in enumerate(content.decode(errors='replace').splitlines(), 1):
            for kind, pattern in SETTINGS:
                if documentation and kind not in DOCUMENTATION_SETTINGS:
                    continue
                for match in re.finditer(pattern, line):
                    value = setting_value(kind, match)
                    item = found.setdefault((kind, value), {'kind': kind, 'value': value, 'path': name,
                                                            'line': number, 'count': 0})
                    item['count'] += 1
    order = list(dict.fromkeys(kind for kind, _ in SETTINGS))
    return sorted(found.values(), key=lambda item: (order.index(item['kind']), item['path'], item['line']))


def prepare(repo, local_only=False, target=None):
    require_clean(repo)
    if target and not local_only:
        raise GuardError('--target is for a local-only audit before repository creation.')
    if target and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', target):
        raise GuardError('Use an exact OWNER/REPO as the planned target.')
    if git(repo, 'rev-parse', '--is-shallow-repository').strip() == b'true':
        raise GuardError('Full history is required. Fetch the complete repository before this audit.')
    # A private profile stays during cleanup, so commits that still hold
    # private names can land. clean-history and publish turn on candidate.
    current = git(repo, 'config', '--get', 'repoGuard.profile', allowed=(0, 1)).decode().strip()
    enable(repo, current or 'candidate')
    if local_only:
        source = load(state_dir(repo) / 'clean-source.json', {})
        name = target or source.get('source_repository') or github_name(repo)
        git(repo, 'config', '--local', 'repoGuard.publicRepository', name)
        surface = {'repository': {'full_name': name, 'private': None},
                   'resources': {}, 'scope': 'local'}
    else:
        git(repo, 'fetch', '--prune', '--tags', 'origin',
            '+refs/heads/*:refs/remotes/origin/*', '+refs/pull/*/head:refs/repo-guard/pr/*')
        surface = github_surface(repo)
    config = scoped_policy(repo)
    if local_only:
        config['public_repository'] = surface['repository']['full_name']
    output = check(repo, 'history', config=config)
    metadata = surface_text(surface).encode()
    output += scanner(repo, 'history', data=metadata, label='GitHub metadata')
    output += privacy(metadata.decode(), 'GitHub metadata', config)
    name = surface['repository']['full_name']
    resources = surface['resources']
    for page in resources.get('actions/runs', []):
        for item in page['workflow_runs']:
            data = run(['gh', 'api', f'repos/{name}/actions/runs/{item["id"]}/logs'], repo).stdout
            text = archive_text(data)
            output += scanner(repo, 'history', data=text, label=f'Actions run {item["id"]}')
            output += privacy(text.decode(errors='replace'), f'Actions run {item["id"]}', config)
    for page in resources.get('actions/artifacts', []):
        for item in page['artifacts']:
            if item['expired']:
                continue
            data = run(['gh', 'api', f'repos/{name}/actions/artifacts/{item["id"]}/zip'], repo).stdout
            text = archive_text(data)
            output += scanner(repo, 'history', data=text, label=f'Actions artifact {item["id"]}')
            output += privacy(text.decode(errors='replace'), f'Actions artifact {item["id"]}', config)
    # Media and packaged releases need content inspection, not a text-only pass.
    binary_paths = []
    for line in git(repo, 'rev-list', '--objects', '--all').decode(errors='replace').splitlines():
        pieces = line.split(' ', 1)
        if len(pieces) != 2 or not re.search(r'\.(?:png|jpe?g|gif|webp|pdf|zip|gz|mp4|wav)$', pieces[1], re.I):
            continue
        blob, path = pieces
        binary_paths.append({'object': blob, 'path': path})
    for page in resources.get('releases', []):
        for release in page:
            for asset in release.get('assets', []):
                output.append(finding('release-asset-review', f'GitHub asset {asset["id"]}'))
    if surface['repository'].get('private') is False and scanning_off(surface['repository']):
        output.append(finding('secret-scanning-off', 'GitHub settings'))
    files = set(git(repo, 'ls-tree', '-r', '--name-only', 'HEAD').decode().splitlines())
    if not any(Path(item).name.lower().startswith('readme') for item in files if '/' not in item):
        output.append(finding('missing-readme', 'README.md'))
    if not any(re.fullmatch(r'(?:LICEN[CS]E|COPYING)(?:\..*)?', item, re.I) for item in files):
        output.append(finding('missing-license', 'LICENSE'))
    result = {'snapshot': snapshot(repo, surface), 'head': git(repo, 'rev-parse', 'HEAD').decode().strip(),
              'github': {'full_name': name, 'private': surface['repository']['private'],
                         'description': surface['repository'].get('description'),
                         'homepage': surface['repository'].get('homepage'),
                         'topics': surface['repository'].get('topics')},
              'scope': 'local' if local_only else 'full',
              'findings': output, 'media_to_review': binary_paths,
              'configuration': configuration(repo),
              'manual_surfaces': MANUAL_SURFACES,
              'review_guide': str(SKILL / 'references/release-review.md'),
              'policy_hash': policy_hash()}
    save(state_dir(repo) / 'github-surface.json', surface)
    result['report_path'] = write_report(repo, result)
    save(state_dir(repo) / 'audit.json', result)
    return result


def review(repo, decision, report_path):
    state = state_dir(repo)
    audit = load(state / 'audit.json')
    if not audit:
        raise GuardError('Run prepare-public before recording a review.')
    require_clean(repo)
    surface = load(state / 'github-surface.json')
    if snapshot(repo, surface) != audit['snapshot']:
        raise GuardError('The audited revision or policy changed. Run prepare-public again.')
    report = Path(report_path).read_text()
    report_html.review_body(report)
    if decision == 'ready' and audit['findings']:
        raise GuardError('Resolve all automated findings before marking the review ready.')
    destination = state / 'review.html'
    destination.write_text(report)
    destination.chmod(0o600)
    record = {'snapshot': audit['snapshot'], 'decision': decision,
              'scope': audit.get('scope', 'full'),
              'report_sha256': hashlib.sha256(report.encode()).hexdigest(),
              'next_step': 'Wait for the user\'s first-release decision.'}
    save(state / 'review.json', record)
    (state / 'review.md').unlink(missing_ok=True)
    record['report_path'] = write_report(repo, audit)
    return record


def approve_finding(repo, fingerprint, note):
    state = state_dir(repo)
    audit = load(state / 'audit.json', {})
    findings = check(repo, 'staged') + audit.get('findings', [])
    item = next((item for item in findings if item.get('fingerprint') == fingerprint), None)
    if not item or not note.strip():
        raise GuardError('Select a scanner fingerprint from check --json or the current audit and give the review reason.')
    records = load(state / 'scanner-exceptions.json', {})
    records[fingerprint] = {'rule': item['rule'], 'path': item['path'], 'note': note}
    save(state / 'scanner-exceptions.json', records)
    print('Recorded an exception for that exact value, rule and source. Run the audit again.')


def publish(repo, confirm):
    if not confirm:
        raise GuardError('Use --confirm OWNER/REPO only when the user requests this public release.')
    state = state_dir(repo)
    recorded = load(state / 'review.json', {})
    if recorded.get('decision') != 'ready':
        raise GuardError('A ready code review is required before public release.')
    if recorded.get('scope') == 'local':
        raise GuardError('A full GitHub audit and ready review are required before public release.')
    require_clean(repo)
    current = prepare(repo)
    if current['github']['full_name'].lower() != confirm.lower():
        raise GuardError('The confirmation does not match origin.')
    if current['findings'] or current['snapshot'] != recorded.get('snapshot'):
        raise GuardError('The audit changed. Resolve findings and record a new code review.')
    report = state / 'review.html'
    if not report.is_file() or hashlib.sha256(report.read_bytes()).hexdigest() != recorded.get('report_sha256'):
        raise GuardError('The recorded code review changed or is missing.')
    branch = gh_json(repo, 'repos/' + confirm)['default_branch']
    local = git(repo, 'rev-parse', 'HEAD').strip()
    remote = git(repo, 'rev-parse', 'refs/remotes/origin/' + branch).strip()
    if local != remote:
        raise GuardError('The reviewed HEAD must match the pushed default branch.')
    run(['gh', 'api', '--method', 'PATCH', f'repos/{confirm}', '-f', 'visibility=public'], repo)
    actual = gh_json(repo, 'repos/' + confirm)
    if actual['private']:
        raise GuardError('GitHub did not confirm public visibility.')
    # Secret scanning is available after the change to public. A public
    # repository without it must not remain public.
    try:
        run(['gh', 'api', '--method', 'PATCH', f'repos/{confirm}', '--input', '-'], repo,
            data=json.dumps({'security_and_analysis': SCANNING}).encode())
        missing = scanning_off(gh_json(repo, 'repos/' + confirm))
    except GuardError:
        missing = list(SCANNING)
    if missing:
        run(['gh', 'api', '--method', 'PATCH', f'repos/{confirm}', '-f', 'visibility=private'], repo)
        raise GuardError(f'GitHub did not confirm {", ".join(missing)}. The repository is private again.')
    enable(repo, 'candidate')
    print(f'Public: https://github.com/{confirm} (secret scanning and push protection on)')


def check_text(data, label, profile, config):
    findings = scanner(None, 'staged', data=data, label=label)
    if profile == 'candidate':
        findings += privacy(data.decode(errors='replace'), label, config)
    return findings


# Flags whose values gh posts as text, or reads from a file and posts.
GH_POSTS = {
    'pr create': {'-t': 'text', '--title': 'text', '-b': 'text', '--body': 'text', '-F': 'file', '--body-file': 'file'},
    'pr comment': {'-b': 'text', '--body': 'text', '-F': 'file', '--body-file': 'file'},
    'pr review': {'-b': 'text', '--body': 'text', '-F': 'file', '--body-file': 'file'},
    'release create': {'-t': 'text', '--title': 'text', '-n': 'text', '--notes': 'text',
                       '-F': 'file', '--notes-file': 'file'},
    'pr merge': {'-t': 'text', '--subject': 'text', '-b': 'text', '--body': 'text', '-F': 'file', '--body-file': 'file'},
    'repo create': {'-d': 'text', '--description': 'text', '-h': 'text', '--homepage': 'text'},
    'repo edit': {'-d': 'text', '--description': 'text', '-h': 'text', '--homepage': 'text', '--add-topic': 'text'},
    'gist create': {'-d': 'text', '--desc': 'text', '-f': 'value', '--filename': 'value'},
    'gist edit': {'-d': 'text', '--desc': 'text', '-a': 'file', '--add': 'file', '-f': 'value', '--filename': 'value'},
}
GH_POSTS['pr edit'] = GH_POSTS['issue create'] = GH_POSTS['issue edit'] = GH_POSTS['pr create']
GH_POSTS['issue comment'] = GH_POSTS['pr comment']
GH_POSTS['release edit'] = GH_POSTS['release create']


def gh_posts(argv):
    """Return the target, texts and files of a gh command that posts text."""
    if len(argv) < 2:
        return None
    command = f'{argv[0]} {"create" if argv[1] == "new" else argv[1]}'
    flags = GH_POSTS.get(command)
    if flags is None:
        return None
    flags = {**flags, '-R': 'repo', '--repo': 'repo'}
    target, texts, files = os.environ.get('GH_REPO'), [], []
    rest = argv[2:]
    index = 0
    while index < len(rest):
        item, value = rest[index], None
        name = item
        if item.startswith('--') and '=' in item:
            name, value = item.split('=', 1)
        elif not item.startswith('--') and item[:2] in flags and len(item) > 2:
            name, value = item[:2], item[2:].removeprefix('=')
        if name in flags and value is None:
            index += 1
            value = rest[index] if index < len(rest) else ''
        kind = flags.get(name)
        if kind == 'repo':
            target = value
        elif kind == 'text':
            texts.append((name, value.encode()))
        elif kind == 'file':
            files.append(value)
        elif command == 'gist create' and not item.startswith('-'):
            files.append(item)
        elif command in ('repo create', 'repo edit', 'gist edit') and not item.startswith('-') and target is None:
            target = item
        index += 1
    # A new repository or gist has no visibility to read yet; its flags set it.
    visibility = None
    if command == 'repo create':
        visibility = 'private' if {'--private', '--internal'} & set(rest) else 'public'
    elif command == 'gist create':
        visibility = 'public' if {'-p', '--public'} & set(rest) else 'private'
    return {'gist': argv[0] == 'gist', 'target': target, 'texts': texts, 'files': files, 'visibility': visibility}


def linked_gists(contents, gh):
    """Read each gist that the text links, so it gets the check of the text."""
    output = []
    pattern = rb'gist\.github(?:usercontent)?\.com/[\w-]+/([0-9a-f]{20,})'
    for gist in dict.fromkeys(match.decode() for _, data in contents for match in re.findall(pattern, data)):
        output.append((f'gist {gist}', run([gh, 'gist', 'view', gist, '--raw']).stdout))
    return output


def real_gh():
    if os.environ.get('REPO_GUARD_GH'):
        return os.environ['REPO_GUARD_GH']
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        item = Path(folder) / 'gh'
        if item.is_file() and os.access(item, os.X_OK) and item.resolve() != SHIM.resolve():
            return str(item)
    raise GuardError('No gh command was found after the repo-guard wrapper.')


def post_profile(target, gh):
    """Use the full check unless the target is a known private repository."""
    try:
        root = repo_root(Path.cwd())
        local = github_name(root)
        target = target or local
        if local.lower() == target.lower() and public_profile(root):
            return 'candidate', target
    except GuardError:
        pass
    if not target:
        return 'candidate', None
    result = subprocess.run([gh, 'api', f'repos/{target}', '--jq', '.private'], capture_output=True)
    return ('private' if result.returncode == 0 and result.stdout.strip() == b'true' else 'candidate'), target


def makes_public(argv):
    """Return True when gh would create a public repository or change one to public."""
    if '--help' in argv:
        return False
    if argv[:2] in (['repo', 'create'], ['repo', 'new']):
        return not {'--private', '--internal'} & set(argv)
    return argv[:2] == ['repo', 'edit'] and ('--visibility=public' in argv or any(
        flag == '--visibility' and value == 'public' for flag, value in zip(argv, argv[1:])))


def gh_wrapper(argv):
    """Check the text that gh posts to GitHub, then run the real gh."""
    # Only publish audits a repository and turns on GitHub secret scanning.
    if makes_public(argv):
        print('repo-guard: gh did not make the repository public. Create it with --private, '
              'then run repo-guard publish.', file=sys.stderr)
        return 1
    try:
        gh = real_gh()
        post = gh_posts(argv)
        # The child gh must not find this wrapper again.
        env = dict(os.environ, PATH=os.pathsep.join(
            folder for folder in os.environ.get('PATH', '').split(os.pathsep)
            if not (Path(folder) / 'gh').resolve() == SHIM.resolve()))
        if post is None:
            os.execve(gh, [gh, *argv], env)
        stdin = None
        contents = list(post['texts'])
        for name in post['files']:
            if name == '-':
                stdin = sys.stdin.buffer.read()
                contents.append(('stdin', stdin))
            elif Path(name).is_file():
                contents.append((name, Path(name).read_bytes()))
        # A secret gist is readable by anyone with its link.
        if post['gist']:
            # A secret gist reaches only the people who get its link. The text
            # that links it gets the check of the place where the link appears.
            visibility = post['visibility']
            if visibility is None and post['target']:
                visibility = 'public' if run([gh, 'api', f'gists/{post["target"]}', '--jq', '.public']).stdout.strip() == b'true' else 'private'
            profile, target = ('candidate' if visibility == 'public' else 'private'), None
        elif post['visibility']:
            profile, target = ('private' if post['visibility'] == 'private' else 'candidate'), post['target']
        else:
            profile, target = post_profile(post['target'], gh)
        config = policy()
        if target:
            config['public_repository'] = target
        if not post['gist']:
            contents += linked_gists(contents, gh)
        findings = [item for label, data in contents for item in check_text(data, label, profile, config)]
    except (GuardError, OSError, ValueError) as error:
        print(f'repo-guard: {error} gh did not post the text.', file=sys.stderr)
        return 2
    if findings:
        emit(findings)
        print('repo-guard: gh did not post the text. Remove the findings and run the command again.', file=sys.stderr)
        return 1
    if stdin is None:
        os.execve(gh, [gh, *argv], env)
    return subprocess.run([gh, *argv], input=stdin, env=env).returncode


def emit(findings, more='Use prepare-public for the full local audit.'):
    for item in findings[:100]:
        print(f'{item["rule"]}: {item["path"]}:{item["line"]}')
    if len(findings) > 100:
        print(f'{len(findings) - 100} more findings. {more}')
    if not findings:
        print('No findings from active credential and repository policy checks. '
              'Review project-specific content before sharing.')
    elif any(item['rule'] == 'worktree-path' for item in findings):
        print('Use example-* checkout names in public examples. Keep local paths in settings outside Git.')
    return bool(findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('install').add_argument(
        '--skills-dir', action='append', type=Path,
        help='Agent skill folder for the public-repo skill (repeat for more). '
             'Default: ~/.claude/skills and ~/.codex/skills when they exist')
    text = commands.add_parser('check-text')
    text.add_argument('files', nargs='*', default=['-'], help='Text files to check; - reads standard input')
    text.add_argument('--profile', choices=('candidate', 'private'), default='candidate')
    text.add_argument('--target', help='OWNER/REPO whose own links are allowed')
    text.add_argument('--linked-gists', action='store_true', help='Also read and check each gist that the text links')
    for name in ('enable', 'check', 'hook', 'prepare-public', 'review', 'publish', 'status', 'approve-binary', 'approve-finding', 'clean-history'):
        sub = commands.add_parser(name)
        sub.add_argument('--repo', type=Path, default=Path.cwd())
        if name == 'enable':
            sub.add_argument('--profile', choices=('private', 'candidate'), default='candidate')
            sub.add_argument('--allow-private', action='store_true',
                             help='Lower a candidate checkout to the private profile')
        if name == 'check':
            sub.add_argument('--mode', choices=('staged', 'history'), default='staged')
            sub.add_argument('--json', action='store_true', help='Print redacted findings and their exact fingerprints')
        if name == 'hook':
            sub.add_argument('event', choices=tuple(EVENTS))
            sub.add_argument('arguments', nargs='*')
        if name == 'review':
            sub.add_argument('--decision', choices=('ready', 'blocked'), required=True)
            sub.add_argument('--report', type=Path, required=True)
        if name == 'publish':
            sub.add_argument('--confirm')
        if name == 'prepare-public':
            sub.add_argument('--local-only', action='store_true',
                             help='Audit local history only; a full GitHub audit is still required for publication')
            sub.add_argument('--target', help='Planned OWNER/REPO for a local-only audit before creation')
        if name == 'clean-history':
            sub.add_argument('--destination', type=Path, required=True)
            sub.add_argument('--confirm', required=True)
        if name == 'approve-binary':
            sub.add_argument('file', type=Path)
            sub.add_argument('--note', required=True)
        if name == 'approve-finding':
            sub.add_argument('--fingerprint', required=True)
            sub.add_argument('--note', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'install':
            install(args.skills_dir)
            return 0
        if args.action == 'check-text':
            config = policy()
            if args.target:
                config['public_repository'] = args.target
            contents = [('stdin', sys.stdin.buffer.read()) if name == '-' else (name, Path(name).read_bytes())
                        for name in args.files]
            if args.linked_gists:
                contents += linked_gists(contents, real_gh())
            findings = [item for label, data in contents for item in check_text(data, label, args.profile, config)]
            return int(emit(findings))
        repo = repo_root(args.repo)
        if args.action == 'enable':
            print(json.dumps(enable(repo, args.profile, args.allow_private), indent=2))
        elif args.action == 'check':
            findings = check(repo, args.mode)
            if args.json:
                print(json.dumps(findings, indent=2))
                return int(bool(findings))
            return int(emit(findings))
        elif args.action == 'hook':
            data = sys.stdin.buffer.read() if args.event == 'pre-push' else b''
            return int(emit(hook(repo, args.event, args.arguments, data)))
        elif args.action == 'prepare-public':
            result = prepare(repo, local_only=args.local_only, target=args.target)
            emit(result['findings'], f'See {result["report_path"]} for all findings.')
            print(f'Code and content review required. Audit: {state_dir(repo) / "audit.json"}')
            print(f'HTML report: {result["report_path"]}')
            print('Preparation stops at local review. Wait for the user\'s release decision.')
            return int(bool(result['findings']))
        elif args.action == 'review':
            print(json.dumps(review(repo, args.decision, args.report), indent=2))
        elif args.action == 'publish':
            publish(repo, args.confirm)
        elif args.action == 'clean-history':
            print(json.dumps(clean_history(repo, args.destination, args.confirm), indent=2))
        elif args.action == 'status':
            value = {'profile': 'candidate' if public_profile(repo) else 'private',
                     'public_repository': scoped_policy(repo).get('public_repository'),
                     'audit': load(state_dir(repo) / 'audit.json'),
                     'review': load(state_dir(repo) / 'review.json'),
                     'hooks': {event: git(repo, 'hook', 'list', event, allowed=(0, 1)).decode().strip() for event in EVENTS}}
            print(json.dumps(value, indent=2))
        elif args.action == 'approve-binary':
            blob = git(repo, 'show', ':' + args.file.as_posix())
            state = state_dir(repo) / 'binary-reviews.json'
            records = load(state, {})
            records[hashlib.sha256(blob).hexdigest()] = {'path': args.file.as_posix(), 'note': args.note}
            save(state, records)
            print('Recorded content review for this exact staged binary.')
        elif args.action == 'approve-finding':
            approve_finding(repo, args.fingerprint, args.note)
        return 0
    except (GuardError, OSError, ValueError, KeyError) as error:
        print(f'repo-guard: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
