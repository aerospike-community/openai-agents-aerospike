# Contributing

Thanks for your interest in `openai-agents-aerospike`. This project is an open-source integration between the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) and [Aerospike](https://aerospike.com/). Issues, discussion, and pull requests are all welcome.

## Ways to contribute

- **File an issue** for a bug, unexpected behavior, or a missing feature.
- **Open a discussion or issue** to propose a larger change before you start coding.
- **Submit a pull request** for docs fixes, examples, tests, performance work, or new features.
- **Review a pull request.** Code review from anyone is welcome.

Please be patient — review latency depends on maintainer bandwidth.

## Before you start

1. Check [existing issues](https://github.com/aerospike-community/openai-agents-aerospike/issues) and open PRs for related work.
2. For non-trivial changes (new public API, data-model change, new dependency), open an issue first so we can agree on the direction before you invest time.

## Development setup

```bash
git clone https://github.com/aerospike-community/openai-agents-aerospike.git
cd openai-agents-aerospike

python -m venv .venv
source .venv/bin/activate

pip install -e .
pip install pytest pytest-asyncio pytest-cov mypy "ruff==0.9.2"
```

Start a local Aerospike Community Edition server (required for the full test suite):

```bash
docker run -d --name aerospike -p 3000-3002:3000-3002 aerospike/aerospike-server:latest
export AEROSPIKE_HOST=127.0.0.1
```

## Running the checks

The CI job runs the same three steps. Run them locally before pushing:

```bash
ruff format --check .
ruff check .
mypy src/openai_agents_aerospike
pytest -v
```

Tests that require a live Aerospike cluster automatically skip when `AEROSPIKE_HOST` is unset.

## Code style

- Python 3.10+, typed with `mypy --strict` (with the overrides already defined in `pyproject.toml`).
- Formatted and linted with `ruff` (line length 100, Google-style docstrings).
- Keep the public API minimal. If you need internal helpers, prefix them with `_`.
- Avoid comments that just narrate what the code does. Use comments to explain non-obvious intent, tradeoffs, or constraints.

## Tests

- New behavior should come with a test. If you are fixing a bug, add a regression test that fails on `main` and passes on your branch.
- Integration tests that need Aerospike live in `tests/` and use the `aerospike_session` / `aerospike_client` fixtures in `tests/conftest.py`.
- Import-only smoke tests that must run without a database live in `tests/test_import.py`.

## Commit messages and PRs

- Small, focused PRs merge faster than large ones.
- Use a short, imperative commit subject (e.g. `Add rate-limit window override`). Include a paragraph in the body when the reasoning is non-obvious.
- Link the PR to an issue when one exists.
- Sign off on the [Code of Conduct](CODE_OF_CONDUCT.md) by participating.

## Releases and upstream contribution

The medium-term plan is to propose `AerospikeSession` for inclusion in `openai/openai-agents-python` as `agents.extensions.memory.AerospikeSession`. Code written here should be written with that destination in mind: small public surface, strict typing, thorough tests, no project-specific assumptions in the session itself. The reference tools and examples can stay in this repository.

## License

By contributing you agree that your contributions are licensed under the MIT License of this repository.
