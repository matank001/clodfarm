# How claude-farm is tested

## Automated (`pytest`, runs in CI)

`tests/` runs without a Claude subscription or AWS:
- `test_governor.py`: every pacing rule, the hard stops, overage, API-key mode, reset handling, and parsing of a real
  `rate_limit_event` payload.
- `test_store.py`, against an in-process DynamoDB (moto) or DynamoDB Local (`FARM_TEST_DYNAMODB`):
  - priority order and atomic claims under 5-way contention;
  - the depth limit, parent wait/resume, and children finishing during the parent's run;
  - retries, lease reaping and lease ownership;
  - account-wide slots, snapshot ordering, the planner single-flight and back-off, pause, and spend totals.
- `test_farm.py`: the real supervisor, store and git flow, driven by `tests/fake_claude.py`, which speaks Claude
  Code's stream-json protocol. Covered:
  - a parent spawns sub-agents, is resumed once in its own session, merges them, and everything lands on main with
    branches cleaned up;
  - the Remote Control keeper;
  - a rate-limit rejection pauses the farm and gives the attempt back;
  - the 5-hour throttle;
  - the planner working from `MISSION.md`;
  - pause and resume;
  - the farm guide reaching agents.

## Manual runs against real Claude Code

**2026-09-25, macOS, Claude Code 2.1.282, Opus 5.5, Team subscription, native (not in Docker)**, 2 workers, moto as
DynamoDB.

The task: write `strutil.slugify` yourself, delegate `truncate` to exactly one sub-agent, then merge and test.

- **Run 1** used the first git design, where every task merged into main.
  - The sub-agent was spawned, finished, and the parent was resumed.
  - But the parent's unfinished work had reached main while it waited, so the child conflicted. That produced two
    chained "resolve merge conflict" tasks before converging.
  - This led to the current hierarchical design: sub-tasks branch from the parent, and the parent merges them.
- **Run 2** used the hierarchical design.
  - Parent run 32.5 s (1,374 output tokens), child 28.3 s (1,270), resumed parent 37.1 s (396).
  - The parent merged the child branch without conflicts.
  - 11 tests passed, main was linear (`init`, `slugify`, `truncate`), and no task branches were left.
  - `claude-farm budget` showed the account at 9% of the 5-hour window and 18% of the 7-day window, as reported by
    Claude Code.

**2026-09-25, AWS eu-central-1, `deploy/aws/deploy.sh up`, t4g.medium.**
- The stack was created, the image built on the box, and the container started. It printed the login banner and
  waited.
- `claude-farm doctor` passed for the claude binary (2.1.274), DynamoDB through the instance role, git and the
  workspace. It failed only on the login, as expected before logging in.
- Remote login and the queued sub-agent task: see the release notes once complete.
