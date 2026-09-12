# Contributing

Contributions are welcome through GitHub pull requests.

## Maintainer control

`main` is protected. Only `@MakCoder-2004` reviews and approves changes for
merging. Do not request or expect merge access to this repository; contributors
should work from a fork.

The maintainer may approve and merge their own pull requests when required
checks pass so the project remains operable as a single-maintainer project.
Contributors cannot bypass review, required checks, signed-commit rules, or
other repository protections.

## Pull request workflow

1. Fork the repository.
2. Create a short-lived branch in your fork.
3. Make a focused change and add tests where appropriate.
4. Run the project checks locally.
5. Open a pull request against `main` and describe the change, verification,
   and any security or operational impact.

Pull requests must pass the required GitHub Actions checks and receive the
maintainer's approval. Keep the branch up to date with `main`; use a linear
history and the repository's allowed merge methods.

## Local checks

Use Python 3.12 and `uv`:

```powershell
uv sync --locked --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q
```

Tests must use mocks for external providers. Never put API keys, bot tokens,
cookies, production data, or complete scraped pages in commits, logs, tests,
issues, or pull requests.

## Commit signing

Commits merged to `main` must be signed. Configure either SSH or GPG commit
signing in GitHub and verify the commit displays as `Verified` before opening a
pull request. GitHub's documentation explains both supported setup methods:
<https://docs.github.com/en/authentication/managing-commit-signature-verification>.

## Code of conduct

Be respectful, constructive, and security-conscious. Technical disagreement is
expected; harassment, personal attacks, and disclosure of private information
are not acceptable.
