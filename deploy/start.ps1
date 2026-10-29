<#
.SYNOPSIS
  Resume the stack after stop.ps1: database first, then the node, then the app pods.

.EXAMPLE
  .\deploy\start.ps1
  .\deploy\start.ps1 -Replicas 2    # must match what the router was deployed with
#>
param([int]$Replicas = 3)

$ErrorActionPreference = "Continue"
$deploy = $PSScriptRoot
$tf = terraform "-chdir=$deploy\terraform" output -json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $tf.instance_id) { throw "No Terraform outputs; is the stack deployed?" }
$region = $tf.region.value
$db = $tf.db_identifier.value
$env:KUBECONFIG = Join-Path $deploy ".kubeconfig"

Write-Host "==> Starting the database (takes a few minutes)"
$status = aws rds describe-db-instances --region $region --db-instance-identifier $db `
    --query "DBInstances[0].DBInstanceStatus" --output text
if ($status -eq "stopped") {
    aws rds start-db-instance --region $region --db-instance-identifier $db | Out-Null
}
aws rds wait db-instance-available --region $region --db-instance-identifier $db
if ($LASTEXITCODE -ne 0) { throw "The database did not become available." }

Write-Host "==> Starting the EC2 node"
aws ec2 start-instances --region $region --instance-ids $tf.instance_id.value | Out-Null
aws ec2 wait instance-running --region $region --instance-ids $tf.instance_id.value
if ($LASTEXITCODE -ne 0) { throw "The node did not start." }

Write-Host "==> Waiting for Kubernetes"
$deadline = (Get-Date).AddMinutes(5)
while (-not (kubectl get nodes --request-timeout=5s 2>$null | Select-String " Ready")) {
    if ((Get-Date) -gt $deadline) { throw "The cluster did not come back; check the node in the EC2 console." }
    Start-Sleep -Seconds 5
}

Write-Host "==> Starting $Replicas app pods"
kubectl -n collabdoc rollout status deployment/kafka --timeout=5m
kubectl -n collabdoc scale statefulset app --replicas=$Replicas
kubectl -n collabdoc rollout status statefulset/app --timeout=10m
if ($LASTEXITCODE -ne 0) { throw "The app pods did not become ready: kubectl -n collabdoc get pods" }

$url = "http://$($tf.public_ip.value)"
Write-Host ""
Write-Host "Running: $url"
