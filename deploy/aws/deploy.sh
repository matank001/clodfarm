#!/usr/bin/env bash
# claude-farm on AWS, one command per step. Needs the AWS CLI v2 and, for login/status/shell,
# the Session Manager plugin: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
#
#   deploy/aws/deploy.sh up [--max-workers 3] [--instance-type t4g.medium] [--workspace-repo URL]
#   deploy/aws/deploy.sh login      # log in to your Claude subscription on the box (URL + code)
#   deploy/aws/deploy.sh status     # claude-farm status on the box
#   deploy/aws/deploy.sh logs       # follow the container logs
#   deploy/aws/deploy.sh shell      # a shell inside the container
#   deploy/aws/deploy.sh down       # delete the stack (the DynamoDB table is kept)
#
# Env: STACK (default claude-farm), STACK_TAGS (extra "Key=Value ..." tags), AWS_REGION / AWS_PROFILE as usual.
set -euo pipefail
STACK=${STACK:-claude-farm}
HERE="$(cd "$(dirname "$0")" && pwd)"
REGION=${AWS_REGION:-$(aws configure get region || echo us-east-1)}
cmd=${1:-help}; shift || true

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
on_box() {  # run a command in an interactive SSM session on the instance
  aws ssm start-session --region "$REGION" --target "$(out InstanceId)" \
    --document-name AWS-StartInteractiveCommand --parameters "command=$1"
}
wait_ready() {
  local id; id=$(out InstanceId)
  echo "waiting for $id to finish setup (Docker build takes 3-6 minutes)..."
  for _ in $(seq 1 90); do
    local cid st
    cid=$(aws ssm send-command --region "$REGION" --instance-ids "$id" --document-name AWS-RunShellScript \
          --parameters 'commands=["docker inspect -f {{.State.Running}} claude-farm 2>/dev/null || echo no"]' \
          --query Command.CommandId --output text 2>/dev/null) || { sleep 10; continue; }
    sleep 5
    st=$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$id" \
         --query StandardOutputContent --output text 2>/dev/null || true)
    [[ "$st" == true* ]] && { echo "container is running."; return 0; }
    sleep 5
  done
  echo "not ready yet; check: $0 logs" >&2; return 1
}

case "$cmd" in
  up)
    params=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --max-workers) params+=("MaxWorkers=$2"); shift 2 ;;
        --instance-type) params+=("InstanceType=$2"); shift 2 ;;
        --workspace-repo) params+=("WorkspaceRepo=$2"); shift 2 ;;
        --model) params+=("Model=$2"); shift 2 ;;
        --table) params+=("TableName=$2"); shift 2 ;;
        --repo-url) params+=("RepoUrl=$2"); shift 2 ;;
        --repo-ref) params+=("RepoRef=$2"); shift 2 ;;
        --source-tarball) params+=("SourceTarball=$2"); shift 2 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
      esac
    done
    aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file "$HERE/template.yaml" \
      --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --tags app=claude-farm ${STACK_TAGS:-} \
      ${params[@]+--parameter-overrides "${params[@]}"}
    wait_ready || true
    cat <<EOF

claude-farm is deployed (stack $STACK, instance $(out InstanceId), table $(out Table)).
Next: log in to your Claude subscription. It prints a URL; open it anywhere, approve, paste the code back:

  $0 login
EOF
    ;;
  login)  on_box "sudo docker exec -it claude-farm claude-farm login" ;;
  status) on_box "sudo docker exec -it claude-farm claude-farm status" ;;
  logs)   on_box "sudo docker logs -f --tail 100 claude-farm" ;;
  shell)  on_box "sudo docker exec -it claude-farm bash" ;;
  down)
    aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
    echo "stack deleted. The DynamoDB table is retained; delete it by hand if you no longer need the history." ;;
  *) sed -n '2,13p' "$0" ;;
esac
