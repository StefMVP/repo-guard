"""Make a private, self-contained HTML release report from an audit."""
from collections import Counter
from datetime import datetime
from html import escape
from html.parser import HTMLParser
import re


class Document(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.tags = set()
        self.text = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)

    def handle_data(self, data):
        self.text.append(data)


def review_body(content):
    document = Document(content)
    body = re.search(r'<body\b[^>]*>(.*?)</body\s*>', content, re.I | re.S)
    if not {'html', 'body'} <= document.tags or not body or not ''.join(document.text).strip():
        raise ValueError('The review must be a complete HTML document with a nonempty body.')
    return body[1]


def render(audit, review=None, decision=None):
    name = escape(audit['github']['full_name'])
    head = escape(audit['head'])
    counts = Counter(item['rule'] for item in audit['findings'])
    count_text = ', '.join(f'{value} {escape(key)}' for key, value in sorted(counts.items())) or 'No automated findings'
    status = 'Blocked' if audit['findings'] or decision == 'blocked' else ('Ready' if decision == 'ready' else 'Code review required')
    local_only = audit.get('scope') == 'local'
    if local_only:
        status = 'Local review blocked' if audit['findings'] or decision == 'blocked' else ('Local review ready' if decision == 'ready' else 'Local code review required')
    visibility = ('Local source only. GitHub audit required.' if local_only else
                  f'Repository stays {"private" if audit["github"]["private"] else "public"}.')
    coverage = ('The audit covers local Git history only. GitHub content, remote refs, logs and artifacts were not checked.' if local_only else
                'The audit covers fetched history and accessible GitHub text, retained Actions logs and artifacts.')
    timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')
    rows = ''.join('<tr><td>' + escape(str(item.get('rule', ''))) + '</td><td>' +
                   escape(str(item.get('path', ''))) + '</td><td>' + escape(str(item.get('line', ''))) +
                   '</td><td><code>' + escape(str(item.get('commit', ''))) + '</code></td></tr>'
                   for item in audit['findings'])
    media = ''.join('<li><code>' + escape(item['path']) + '</code> · <code>' +
                    escape(item['object']) + '</code></li>' for item in audit.get('media_to_review', []))
    manual = ''.join('<li>' + escape(item) + '</li>' for item in audit.get('manual_surfaces', []))
    guide = ('<p><a href="' + escape(audit['review_guide'], quote=True) + '">Release review criteria</a></p>'
             if audit.get('review_guide') else '')
    metadata = ''
    if not local_only and 'description' in audit['github']:
        fields = [('Description', audit['github'].get('description') or 'Missing description'),
                  ('Topics', ', '.join(audit['github'].get('topics') or []) or 'No topics'),
                  ('Website', audit['github'].get('homepage') or 'Not set (optional)'),
                  ('GitHub Actions', {True: 'Enabled', False: 'Disabled', None: 'Not checked'}[audit['github'].get('actions_enabled')])]
        metadata_rows = ''.join('<tr><th scope="row">' + label + '</th><td>' +
                                escape(value) + '</td></tr>' for label, value in fields)
        metadata = ('<details><summary>GitHub metadata</summary><table>' + metadata_rows +
                    '</table><p>Review the description and topics for purpose and accuracy. '
                    'A website is optional. Check any supplied link.</p></details>')
    configuration = ''
    if 'configuration' in audit:
        settings = audit['configuration']
        setting_rows = ''.join('<tr><td>' + escape(item['kind']) + '</td><td><code>' + escape(item['value']) +
                               '</code></td><td>' + escape(f'{item["path"]}:{item["line"]}') + '</td><td>' +
                               escape(str(item['count'])) + '</td></tr>' for item in settings)
        configuration = (f'<details><summary>Configuration review · {len(settings)} values</summary>'
                         '<p class="muted">Hardcoded paths, hosts, identities, commands, constants and tunable '
                         'numbers in the current tree. Tests and fixtures are excluded. For each value, decide: '
                         'keep, infer, configure or document.</p><div class="scroll"><table><thead><tr><th>Kind</th>'
                         '<th>Value</th><th>First location</th><th>Uses</th></tr></thead><tbody>' + setting_rows +
                         '</tbody></table></div></details>')
    analysis = '<section aria-label="Code review">' + review_body(review) + '</section>' if review else '<p>The code and content review is pending. Automated checks do not approve a public release.</p>'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{name} · Public release review</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f6f8;color:#192331;font:15px/1.55 system-ui,sans-serif}}
main{{max-width:980px;margin:32px auto;padding:24px;background:white;border:1px solid #dce1e8;border-radius:12px}}
h1{{font-size:27px;margin:0}}h2{{font-size:20px;margin:22px 0 8px}}h3{{font-size:16px}}p{{margin:10px 0}}
a{{color:#1559a8}}code{{font:12px/1.5 ui-monospace,monospace;overflow-wrap:anywhere}}
.status{{display:inline-block;padding:3px 10px;border:1px solid #dabf8b;border-radius:6px;background:#fff5db;font-weight:650}}
.muted{{color:#566476}}details{{margin:12px 0;border:1px solid #dce1e8;border-radius:7px;padding:10px 13px}}
summary{{cursor:pointer;font-weight:600}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}
td,th{{padding:7px;text-align:left;border-bottom:1px solid #e5e8ed;vertical-align:top}}li{{margin:7px 0}}
@media(max-width:640px){{main{{margin:0;border:0;border-radius:0;padding:18px}}h1{{font-size:23px}}}}
</style></head><body><main>
<h1>{name}</h1><h2>Update · {timestamp}</h2>
<p><span class="status">{status}</span> · {visibility}</p>
<p>This report records checks. The user's release decision is a separate step.</p>
<p>{len(audit['findings'])} automated findings. {count_text}.</p>
{analysis}
{metadata}
{configuration}
<details><summary>Automated checks · {len(audit['findings'])} findings</summary>
<p class="muted">Matched values are omitted. Paths, line numbers and commits identify each source.</p>
<div class="scroll"><table><thead><tr><th>Rule</th><th>Source</th><th>Line</th><th>Commit</th></tr></thead><tbody>{rows}</tbody></table></div></details>
<details><summary>Manual checks and historical media</summary>{guide}<ul>{manual}</ul><ul>{media}</ul></details>
<details><summary>Audit evidence</summary><p>Revision: <code>{head}</code></p>
<p>Snapshot: <code>{escape(audit['snapshot'])}</code></p><p>Policy: <code>{escape(audit['policy_hash'])}</code></p>
<p>{coverage}
The recorded review states the tests and content checks. A later code or policy change requires a new audit.</p></details>
</main></body></html>'''
