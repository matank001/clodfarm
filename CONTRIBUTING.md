# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest            # unit + end-to-end tests with a fake `claude` and an in-process DynamoDB (moto)
```

- `tests/fake_claude.py` speaks Claude Code's stream-json protocol, so the supervisor, git flow and budget logic run
  end to end without a subscription. Script it with words in the prompt (`SPAWN 2`, `COMMIT x`, `REJECT`).
- To run the tests against DynamoDB Local, set `FARM_TEST_DYNAMODB=http://localhost:8000`.
- The governor is a pure function. Every rule change needs a test in `tests/test_governor.py`.

## Style

- Python 3.10+, standard library plus boto3 only in the runtime.
- Keep modules small and single-purpose (see docs/architecture.md). Prefer clear names over comments.
- User-facing text is plain and short. Say what happened and what to do next.

## Principles we won't trade away

- Stay inside the subscription's limits. No account pooling, rotation or limit evasion.
- Never read, log or transmit credentials.
- No web UI in core. The Claude app, the CLI and the logs are the interface.
