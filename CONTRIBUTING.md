# Contributing to agentixmesh

## Running the Tests

```sh
python3 -m pytest -q
```

All tests must be green before submitting a pull request.

## Scope Rule

agentixmesh is a **same-user, single-machine** message channel. Cross-user and cross-machine transport are a separate private layer and are **not part of this repository**.

Pull requests must not add:
- Cross-user message delivery (different OS-level uid)
- Cross-machine or networked transport
- Any code that assumes shared writable paths across users

If your use case requires multi-user or networked messaging, that is out of scope here.

## Pull Request Etiquette

- Keep changes surgical — touch only what the PR is about.
- Match the existing code style.
- Add or update tests for any changed behaviour.
- Ensure `python3 -m pytest -q` is green locally before opening the PR.
- Write a clear description of what the change does and why.

## Reporting Security Issues

See [SECURITY.md](SECURITY.md). Do not open public issues for vulnerabilities.
