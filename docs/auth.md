# Logging in to your Claude subscription

claude-farm runs the official `claude` CLI, so it logs in the same way Claude Code does. claude-farm never reads,
copies, logs or transmits your credentials. It only asks `claude auth status` whether a login exists, and it prints
instructions until one does.

Everything Claude Code stores lives in `CLAUDE_CONFIG_DIR=/home/farm/.claude`, which is the `claude-home` Docker
volume: the login, `.claude.json`, session transcripts (needed to resume parents) and settings. Restarts, image
upgrades and `docker compose down` (without `-v`) keep you logged in.

## A. Log in remotely (recommended)

```bash
docker exec -it claude-farm claude-farm login          # local or any Docker host
ssh -t myserver docker exec -it claude-farm claude-farm login
deploy/aws/deploy.sh login                        # the AWS deploy, over SSM Session Manager
```

`claude-farm login` runs `claude auth login`. Because the container has no browser, Claude Code prints a URL. Open it on
any device where you're signed in to Claude (a phone is fine), approve, copy the code it shows and paste it back into
the terminal. The farm notices the login within a few seconds and starts. No inbound port is needed.

This is a full Claude Code login, the same as on a laptop, so Remote Control works with it.

Switch accounts with `claude-farm login --force`. Log out with `claude-farm logout`, or delete the volume.

## B. A long-lived token (`claude setup-token`)

On your own computer, with Claude Code installed and logged in:

```bash
claude setup-token        # opens a browser, prints a token valid for about a year
```

Put it in `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...` and restart the container. This suits headless workers and CI,
because nothing interactive happens on the server.

Caveats:
- The token is a password for your subscription. Keep `.env` out of git (it already is, via `.gitignore`), or inject
  it from a secret store (AWS SSM Parameter Store, Docker secrets, Kubernetes secrets).
- Remote Control is designed around the full login. If `claude remote-control` refuses to start with a token, use
  option A on that box, or set `FARM_REMOTE_CONTROL=0` and use only headless workers. `docs/testing.md` records
  what we observed.

## C. Reuse an existing Linux login

Claude Code on Linux keeps its login in `~/.claude/.credentials.json`. Mount a copy of that directory:

```yaml
    volumes:
      - /home/me/.claude:/home/farm/.claude
```

This works because the file format is the same. **Don't run the same login on two machines at once**, though.
OAuth refresh tokens rotate: when one machine refreshes, the other copy can stop working. Move the login instead of
copying it, or log in separately with A (every login is its own session).

**macOS:** Claude Code keeps the login in the Keychain, not in a file, so there's nothing to mount. Use A or B.

## Which account?

Use your own subscription for your own work. For a team, give each person or each farm its own seat (Claude Team or
Enterprise). claude-farm deliberately has no feature for switching between accounts to get around limits, and it won't
get one.
