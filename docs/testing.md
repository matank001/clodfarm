# How claude-farm is tested

## Automated (`pytest`, runs in CI)

`tests/` runs without a Claude subscription or AWS:
- `test_governor.py`: every pacing rule, the hard stops, overage, API-key mode, reset handling, and parsing of a real
  `rate_limit_event` payload.
- `test_store.py`, `test_farm.py` and `test_multiseat.py` run twice, on SQLite and on DynamoDB (in-process moto, or DynamoDB Local via `FARM_TEST_DYNAMODB`). `test_store.py` covers:
  - priority order and atomic claims under 5-way contention;
  - the depth limit, parent wait/resume, and children finishing during the parent's run;
  - retries, lease reaping and lease ownership;
  - account-wide slots, snapshot ordering, the planner single-flight and back-off, pause, and spend totals.
- `test_farm.py` runs the real supervisor, store and git flow, driven by `tests/fake_claude.py`, which speaks Claude
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
- **Remote login:** `deploy/aws/deploy.sh login` opened an SSM session into `claude-farm login`. The owner opened the
  URL, approved, and pasted the code. The farm noticed within seconds (`authenticated: claude.ai (team)`) and started.
- **Test 1 found two bugs, both fixed:**
  - Remote Control hung silently on its one-time "Enable Remote Control? (y/n)" prompt. claude-farm now pre-answers
    it when `FARM_REMOTE_CONTROL=1`.
  - The end-of-run auto-commit swept `__pycache__` into commits. The parent's later rebase then failed with
    "untracked working tree files would be overwritten", and each resolve-conflict task hit it again. Build
    artifacts are now excluded through `.git/info/exclude`, and conflict tasks are never chained.
  - Both have regression tests.
- **Test 2**, with `FARM_VERIFY_CMD="python3 -m unittest discover -p 'test_*.py'"`:
  - Remote Control connected ("✔ Connected", session visible in the Claude app).
  - The parent implemented slugify, spawned one sub-agent for truncate (29 s), and was resumed (14 s).
  - The check passed, 2 commits merged onto main, no build artifacts were committed and no conflict tasks were
    created.
  - `claude-farm budget` read 14% of the 5-hour window and 21% of the 7-day window from the live
    `rate_limit_event`. DynamoDB access worked through the instance role, with no keys on the box.

**2026-09-25, AWS, after the store rewrite (SQLite + DynamoDB behind one interface):**
- **Test 3** (parent + sub-agent on DynamoDB): it landed with the check passing. It exposed an item written by the
  previous version without a version number, which looped forever. Fixed: legacy items are adopted, and a late
  error on a finished task is a no-op. Both have regression tests.
- **Test 4:** landed, check passed, no errors.
- **Restart test:**
  - `docker compose restart` while a task was running;
  - the stopping box logged `task.restart`, and the new container claimed the same task again within seconds, in
    its own session;
  - before this fix it waited for its 5-minute lease.
- **Zero-config image** (locally, no `.env`, no database): `doctor` showed `farm store: sqlite
  /workspace/.farm/farm.db` and the login banner.
