# Security policy

## Reporting

Do not publish a security report that contains a private paper, credentials, access tokens, unpublished results, or personally identifiable information. Use a private GitHub security advisory when repository security advisories are available; otherwise contact the repository owner privately through their GitHub profile before opening a public issue.

Include the affected commit/version, operating system, minimal reproduction, expected/observed behavior, and whether untrusted PDF/URL input is required.

## Untrusted inputs

Treat PDFs, arXiv downloads, citation-provider responses, images, project JSON, and presentation assets as untrusted input. Work in a user-selected project directory, inspect source identity, and do not execute content embedded in papers.

External citation providers are optional and cannot override source-paper metadata or checkpoint rules. Unverified citation fields remain marked unverified.

## Sensitive data

Never commit:

- user papers or unpublished manuscripts;
- generated digest/checkpoint/review/delivery projects;
- API keys, GitHub tokens, cookies, provider credentials, or email addresses;
- absolute personal filesystem paths;
- `.venv`, `node_modules`, browser profiles, or caches.

The package validator scans common secret and private-path patterns, but it is not a substitute for human review.

## Checkpoint integrity

A vulnerability fix must not bypass or weaken explicit CKPT-1/CKPT-2 approval, source hashes, evidence audits, quantitative coverage, or review bindings. Never “repair” a checkpoint by hand-editing JSON or hashes.

## Supported release

Security fixes target the latest commit on `main` and the currently documented Scholar-Slides version. The repository does not promise support for modified installed snapshots, hand-edited checkpoints, or third-party forks.
