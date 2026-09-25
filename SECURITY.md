# Security policy

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** on this repository.
Don't open a public issue for a security problem. We aim to acknowledge reports within 3 business days.

In scope: the clodfarm code, the Docker image and the AWS template. Out of scope: Claude Code itself (report those
to Anthropic) and prompt-injection behaviour of the model, unless clodfarm makes it worse than plain Claude Code.

The security model and hardening advice are in [docs/security.md](docs/security.md).
