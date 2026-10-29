# Deploying CollabDoc on AWS

Several app pods behind a document-aware router, on the cheapest AWS setup
that still runs real Kubernetes: **about $2.5/day while it is up** (c7i-flex.large
+ db.t4g.micro + one public IPv4, in `ap-south-1`). Run `terraform destroy`
when the demo is over.

## Architecture

```
                    Internet
                       │  :80 / :443
                ┌──────▼───────────────────────────────── EC2 c7i-flex (k3s) ─┐
 Elastic IP ───▶│  caddy  ── hash(document id) ──┬──▶ app-0 ─┐                   │
                │  (router, TLS)                 ├──▶ app-1 ─┼──▶ kafka (fan-out) │
                │                                └──▶ app-2 ─┘                   │
                └───────────────────────────────────────┬──────────┬────────────┘
                                                        │          │ instance role
                              private subnets ┌─────────▼───┐  ┌───▼──────┐  ┌─────┐
                                              │ RDS Postgres│  │ S3 bucket│  │ ECR │
                                              └─────────────┘  └──────────┘  └─────┘
```

| Piece | AWS / k8s resource | Why |
|---|---|---|
| App | StatefulSet `app`, 3 pods | Stable pod names for the router to hash onto; ordered start so one pod at a time runs schema migrations |
| Router | Caddy Deployment + LoadBalancer Service | Sends every request for a document to the same pod; optional automatic HTTPS |
| Fan-out | Kafka (KRaft, one pod) | Notifications and presence reach users connected to other pods |
| Database | RDS PostgreSQL 16, private subnets | Shared, durable state for all pods |
| Uploads | S3 bucket, private | Every pod sees every image (`COLLABDOC_UPLOAD_BUCKET`) |
| Image | ECR repository | Pulled by the node with a pull secret refreshed every 6 h from the instance role |
| Cluster | k3s on one EC2 node | Real Kubernetes without the EKS control-plane fee (~$73/month) |
| Access | Kubernetes API (6443) open to your IP only; kubeconfig handed over through SSM Parameter Store | No SSH keys to manage |

### Why the router hashes on the document id

A document's live editing state (the OT version counter, recent operations,
the authorship ledger) lives in the memory of the pod that opened it. If two
pods opened the same document, each would number operations independently and
the copies would drift apart; the operation log has no unique constraint that
would catch it. So Caddy pulls the document id out of `/ws/documents/{id}`,
`/api/documents/{id}/…` and `/p/{id}`, and hashes it (rendezvous hashing over
the stable names `app-0…app-N`). Requests that name no document go to any pod.

Every response carries `X-Served-By: <pod>`, so the affinity is visible in the
browser's network tab, which is handy for a demo.

**Known limitation:** if a pod dies, its documents fail over to the other pods
(health checks every 5 s). When it comes back, its documents move back, and an
editor who stayed connected to the failover pod should reload the page. A
production fix would be a lease per document in the database or in Redis.

## Prerequisites (Windows)

```powershell
winget install Hashicorp.Terraform Amazon.AWSCLI
aws configure            # an IAM user with admin rights; region e.g. ap-south-1
```

Docker Desktop must be running (it builds the image).

## Deploy

```powershell
cd deploy\terraform
terraform init
terraform apply          # ~10 min, most of it RDS
cd ..\..
.\deploy\deploy.ps1      # build, push to ECR, roll out; prints the URL
```

Wait a minute or two after `terraform apply` before running the script: the
node is still installing k3s. The script waits for it.

HTTPS without buying a domain: `.\deploy\deploy.ps1 -Https` serves the app at
`https://<ip-with-dashes>.sslip.io` with a Let's Encrypt certificate. The PWA
and service worker only work over HTTPS. If certificate issuance fails, re-run
without `-Https`.

To ship a code change, run `.\deploy\deploy.ps1` again: new image tag, then a
rolling update one pod at a time.

## Demo commands

```powershell
$env:KUBECONFIG = "$PWD\deploy\.kubeconfig"
kubectl -n collabdoc get pods -o wide            # app-0..2, kafka, caddy
kubectl -n collabdoc logs app-1 -f
kubectl -n collabdoc scale statefulset app --replicas 2   # then re-run deploy.ps1 -Replicas 2 so the router agrees
kubectl -n collabdoc delete pod app-1            # watch it come back, documents fail over meanwhile
aws s3 ls s3://$(terraform -chdir=deploy\terraform output -raw upload_bucket)/uploads/
```

To get a shell on the node without SSH (needs the Session Manager plugin):
`aws ssm start-session --target $(terraform -chdir=deploy\terraform output -raw instance_id)`.

## Start, stop, monitor

```powershell
.\deploy\stop.ps1      # pods to 0 (edits saved), then stop EC2 and RDS: ~$0.25/day while stopped
.\deploy\start.ps1     # RDS first, then EC2, then the pods; same IP as before
.\deploy\status.ps1    # node, database, credits left, alarms, pods, memory, app health
```

AWS restarts a stopped RDS database on its own after 7 days; run `stop.ps1`
again if a pause lasts longer.

In the AWS console:

- **CloudWatch → Dashboards → `collabdoc`**: node CPU and network, database
  CPU, connections and storage, and the alarm states
  (`terraform output dashboard_url`).
- **CloudWatch → Alarms**: node status check, node CPU, database CPU,
  database storage.
- **Billing → Budgets → `collabdoc-monthly`**: usage before credits, so it
  shows how fast credits are being spent.
- **Billing → Free plan**: credits remaining and the plan's expiry.

To have alarms and budget alerts emailed, set an address and re-apply, then
confirm the subscription email AWS sends:

```powershell
cd deploy\terraform
terraform plan -var alert_email=you@example.com -out=tfplan; terraform apply tfplan
```

Inside the cluster: `kubectl -n collabdoc logs app-0 -f`,
`kubectl top pods -n collabdoc`, `kubectl -n collabdoc get events --sort-by=.lastTimestamp`.

### Credits

The account is on the AWS **Free plan**: all usage is paid from its credits,
and AWS cannot charge a card unless the account is upgraded to a paid plan.
Don't upgrade it. When the credits run out or the plan expires (2027-01-29),
AWS closes the free account instead of billing it, and deletes its resources
unless it is upgraded within the grace period, so copy out anything worth keeping first. Running all day costs about $2.5
of credits, so stop the stack whenever you're not using it.

## Tear down

```powershell
cd deploy\terraform
terraform destroy
```

This removes everything, including the S3 bucket's contents, the ECR images and
the database (no final snapshot).

## Files

| Path | What |
|---|---|
| `terraform/main.tf` | VPC, subnets, security groups, EC2 + IAM role, RDS, S3, ECR, SSM parameter |
| `terraform/user-data.sh.tftpl` | First boot: installs k3s, publishes the kubeconfig, sets up the ECR pull-secret timer |
| `k8s/app.yaml` | App StatefulSet + headless Service |
| `k8s/kafka.yaml` | Single-broker Kafka |
| `k8s/router.yaml` | Caddy config, Deployment, LoadBalancer Service |
| `deploy.ps1` | Build → push → render manifests → apply → wait for rollout |

Terraform state (`terraform/*.tfstate`) holds the database password; it is
gitignored, so keep it that way.
