# Deploy on AWS

`deploy/aws/template.yaml` creates:

| Resource | Notes |
|---|---|
| VPC + one public subnet + internet gateway | Its own small network. No NAT gateway, so no NAT cost. |
| Security group | **No inbound rules.** Outbound HTTPS covers Remote Control, the Claude API, git and SSM. |
| EC2 `t4g.medium` (arm64), Ubuntu 24.04 | Docker, 4 GB swap, encrypted gp3, IMDSv2 with hop limit 2 so the container can use the instance role. |
| IAM role | SSM Session Manager, plus Get/Put/Update/Delete/Query/Describe on **this stack's table only**. |
| DynamoDB table | On-demand, point-in-time recovery, TTL. `DeletionPolicy: Retain`, so deleting the stack keeps your history. |

Cost (ESTIMATE, us-east-1 on-demand prices at the time of writing; check the AWS pricing pages):
- a t4g.medium is about $0.034/h, roughly $25/month;
- 30 GB of gp3 is about $2.40/month;
- DynamoDB on-demand for a farm is usually cents per month.

A t4g.small (2 GB) runs one or two agents. Use t4g.large for 6+.

## Steps

```bash
export AWS_REGION=us-east-1           # and AWS_PROFILE if you use profiles
deploy/aws/deploy.sh up                # 3-6 minutes: stack + Docker build on the box
deploy/aws/deploy.sh login             # opens an SSM session straight into `claude-farm login`
deploy/aws/deploy.sh status
```

`up` accepts `--max-workers N`, `--instance-type`, `--model`, `--workspace-repo <git url>`, and `--table <name>` to
share one budget table between several stacks. Set `STACK=name` to run more than one farm.

The login step opens an interactive SSM session that runs `docker exec -it claude-farm claude-farm login` on the box. You
see a URL: open it on any device, approve, and paste the code back. See [auth.md](auth.md).

Day to day:
```bash
deploy/aws/deploy.sh logs     # follow the container logs
deploy/aws/deploy.sh shell    # a shell inside the container (claude-farm task add ..., git log, ...)
deploy/aws/deploy.sh down     # delete everything except the DynamoDB table
```

## Working on a private repo

1. Create a deploy key with write access for that one repo, and keep its private half off the repo.
2. Put it in the container: `deploy/aws/deploy.sh shell`, then
   `mkdir -p ~/.ssh && cat > ~/.ssh/id_ed25519` (paste the key), `chmod 600 ~/.ssh/id_ed25519`, then
   `ssh-keyscan github.com >> ~/.ssh/known_hosts`.
   `~/.ssh` isn't a volume, so for a durable setup mount it or bake it into your own image layer.
3. Deploy with `--workspace-repo git@github.com:you/repo.git`, or set `FARM_REPO_URL` in `/opt/claude-farm/.env`
   and run `sudo docker compose up -d` there.

## Updating

On the box: `cd /opt/claude-farm && sudo git pull && sudo docker compose up -d --build`. The login and the workspace
are in volumes and survive the update.
