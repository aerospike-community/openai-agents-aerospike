# Security Policy

## Supported Versions

This project is in early development. Security fixes are applied to the latest `main` branch and to the most recent published release.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.

Instead, report it privately via GitHub's [security advisory](https://github.com/aerospike-community/openai-agents-aerospike/security/advisories/new) workflow on this repository. Include:

- A description of the issue and its impact.
- Steps to reproduce, or a minimal proof of concept.
- The affected version(s) and any relevant environment details.

We will acknowledge the report within a reasonable timeframe and work with you on a coordinated disclosure. Once a fix is ready, we will publish a release and a GitHub security advisory crediting the reporter (unless you prefer to remain anonymous).

## Scope

This project is an integration between the OpenAI Agents SDK and Aerospike. Vulnerabilities in those upstream projects should be reported to their respective maintainers:

- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python/security)
- [Aerospike](https://aerospike.com/trust-center/)

Issues in the integration code itself (session, tools, examples, CI) are in scope for this repository.
