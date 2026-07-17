# Security Policy

## Supported versions

The project is currently pre-1.0. Security fixes are applied to the latest commit on the canonical branch. Older commits and unmaintained forks are not supported.

## Report a vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting:

<https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/security/advisories/new>

Include:

- the affected file, command, or workflow;
- impact and realistic attack scenario;
- reproduction steps or a minimal proof of concept;
- affected revisions;
- any suggested mitigation.

Do not include secrets or data from repositories you do not own.

Maintainers will acknowledge a complete report as soon as practical, investigate it privately, and coordinate disclosure after a fix is available. Response times are best-effort because this is a community-maintained project.

## Security boundaries

Reports are especially useful for:

- path traversal or symbolic-link escapes;
- unsafe Git revision handling;
- acceptance of forged, stale, or empty evidence;
- accidental disclosure of repository data;
- commands that can modify files outside the selected Git root;
- workflows that let the Root, Planner, or Evaluator bypass role permissions.

General feature requests and non-sensitive bugs belong in the public issue tracker.
