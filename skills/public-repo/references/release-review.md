# Public release review criteria

Use these criteria during the first public review. Apply the parts that fit the
project. State the result and evidence in the private HTML report. Separate
confirmed bugs, inspection findings and optional improvements.

## README

Apply the user's global writing instructions. Use ASD-STE100 Simplified
Technical English. Keep the README short: purpose, installation, configuration
and use. Add a limit only when it changes a user's setup or action.

- Start with what the project does. Give the shortest path to a useful result.
- State dependencies before the commands that need them. Use portable paths.
- Explain a command's output only when it is not clear from the command.
- Keep the full option list in CLI help. Keep code checks in the project's
  existing scripts and package configuration.
- Do not add Check changes, Testing, Validation, release checklists or similar
  README sections as an audit requirement. Add contributor instructions only
  when the user asks for them. Run checks and put proof in the private review.
- Remove process narration, screenshot-production captions, implementation
  notes, audit status, disclaimers and provenance that do not help the reader
  use the project. A product image does not need a paragraph about its capture.
- Keep useful image alt text. Add a caption only to explain a visible control
  or state that the image and nearby text do not explain.
- For each sentence, name the user's task it supports. Delete it if removing
  it changes no instruction, required constraint or user decision.
- Check commands, links, image paths and the selected license. Keep the user's
  chosen wording unless they ask to change it.

## GitHub metadata

Review the repository's About section with the README. The audit records its
current description, topics and website. Missing fields are review items;
they are not credential findings.

- Use one clear description of what the project does and why it is useful.
  Keep it consistent with the README. Remove filler and unsupported claims.
- Add a few relevant topics for the project's purpose and technology. Use
  established terms. Check for private names. GitHub topic names are public,
  including names created from a private repository.
- Set a website only when a useful public demo, documentation site or project
  page exists. Confirm the destination. Leave it empty otherwise. Remove stale
  links, local addresses, private services and URLs with access tokens.
- Include metadata changes in the selected release work. A review records
  proposed wording and topics before changes are applied.

See [GitHub's topic guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

## Screenshots and example output

Include a screenshot when it helps a reader understand the UI, workflow or
result before installation. For a library or a simple CLI, a code sample or
terminal output can be sufficient.

Capture the current core flow with populated public sample data. Inspect the
saved image for private paths, names, accounts, browser tabs and credentials.
Use representative sample data. Do not present invented outcomes as measured
results. Keep capture commands, fixture details and screenshot provenance in
the private review. Do not add these details as product-image captions.
Show the useful app area at a readable size. Give the image meaningful alt text.
Keep a repository image in a tracked documentation path and confirm that it
renders on GitHub. Check rights for any included third-party media. Use the
existing exact-content approval when the image passes review.

## Installation, packages and local checks

- Run the documented core flow. Verify the install and first-use commands in a
  clean temporary environment when they change or have not been checked.
- For a distributed package, inspect the built artifact's file list. Confirm
  that it includes the documented entry points and required assets. Check for
  unintended files. Install and start the artifact, not just the source checkout.
- Use the project's existing test, type, lint and format checks as applicable.
  Use the existing check command; a new README check section is not required.
  Test observed bugs and their edge cases: reproduce the bug in a test, fix it, then show the
  test passes.
- Keep publication and deployment separate from local checks. Full-history
  scans and this review belong to release preparation.

## Configuration and portability

A private project often depends on its owner's machine, accounts and
preferences. Find each value that a new user can need to change. The audit's
"Configuration review" lists candidate values from the current tree, without
tests and fixtures. Use the list as a start. Also read the code for assumptions
that a pattern cannot find, such as a fixed directory layout or a required tool.

Check these kinds of values:

- Identities: account, organization and repository names, email addresses and
  repositories with special treatment.
- Paths: home directory layout, install, state and cache locations, and links
  into the directories of other tools.
- Hosts and services: local ports, private hostnames, service URLs, API
  endpoints and download sources.
- Tools: required commands, their minimum versions and the result when a tool
  is missing.
- Numbers: limits, timeouts, sizes, intervals, retries and thresholds.
- Policy: rules, word lists and defaults that show one person's preferences.

For each value, record one decision in the review:

- Keep: the value is a fact of the project or a standard, for example a pinned
  checksum or a protocol limit.
- Infer: the program can get the value from Git, the environment or the
  authenticated account.
- Configure: users can need a different value. Read it from a configuration
  file, an environment variable or an option. Give a safe default and document it.
- Document: the value stays fixed, but users must know it, for example a
  supported platform.

Supply an example configuration with fictional values. Keep the owner's actual
configuration outside the public repository. Add an option only when a user can
need a different value. An optional tool that is missing must give a clear
message or a fallback. It must not stop an unrelated command.

## Architecture and evidence

Review configuration, file and network access, state ownership, error handling
and installation boundaries. Review comments after the code. Remove comments
that repeat the code. First use clear names, types, lint, architecture or a
meaningful test. Do not add tests that only mirror implementation details to
justify a comment. Keep a comment only for a reason those methods cannot
express. Prefer a small fix or deletion when it solves the
problem. For a proposed deletion, state which behavior it removes. Keep an
architecture description in the private HTML report only as detailed as the
project needs.

Name the checked revision and link the proof. Distinguish real integration
checks, simulations and unverified cases. State each material gap. A passing
test count alone does not establish that the documented flow works.

## References

[ghui](https://github.com/kitlangton/ghui) and
[OpenCode](https://github.com/anomalyco/opencode) show purpose, a product image
and installation near the start. Use that order when it helps the reader.
[stack's package setup](https://github.com/kitlangton/stack/blob/main/package.json)
shows explicit package contents and a package check. Adapt the practice to the
project's stack and size.
