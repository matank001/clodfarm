# Changelog

## 0.1.0 (2026-09-25)

First public release.

- `claude-farm run`: a supervisor with a Remote Control keeper and N headless Claude Code workers.
- DynamoDB single-table queue: priorities, leases, retries, parent and child tasks, resuming a parent in its own
  session.
- Budget governor on Claude Code's real `rate_limit_event` utilization: weekly pacing (80% target by default), a
  5-hour ceiling (85%), pause on rejection, no paid overage. API-key mode with a daily dollar cap.
- A planner that keeps the agents busy from `MISSION.md`.
- A git worktree per task. Sub-tasks branch from their parent, and top-level tasks rebase and fast-forward into main.
- Docker image, docker compose with DynamoDB Local, and an AWS CloudFormation deploy with no inbound ports.
- Quality gate (`FARM_VERIFY_CMD`): check the rebased branch before it lands; resume the agent to fix failures.
- Timeouts resume the session instead of starting over; a circuit breaker pauses the farm after repeated failures.
- Notifications to ntfy, Slack or Discord (`FARM_NOTIFY_URL`); `FARM_EFFORT`.
- Usage-limit detection reads only structured events and error results, never the agent's own text.
- Login three ways: remote `claude-farm login`, a `claude setup-token` token, or an existing Linux profile.
