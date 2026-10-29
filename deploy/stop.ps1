<#
.SYNOPSIS
  Pause the whole stack: app pods, the EC2 node, and the RDS database.

.DESCRIPTION
  Stopped, the stack costs about $0.25/day (disks, the Elastic IP, backups)
  instead of about $2.5/day. Nothing is deleted; start.ps1 brings it back on
  the same IP. AWS restarts a stopped RDS instance by itself after 7 days, so
  run this again if a pause lasts longer than that.
#>
$ErrorActionPreference = "Continue"
$deploy = $PSScriptRoot
$tf = terraform "-chdir=$deploy\terraform" output -json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $tf.instance_id) { throw "No Terraform outputs; is the stack deployed?" }
$region = $tf.region.value
$env:KUBECONFIG = Join-Path $deploy ".kubeconfig"

Write-Host "==> Scaling the app to 0 (each pod writes its unsaved edits to the database)"
kubectl -n collabdoc scale statefulset app --replicas=0 --timeout=2m
if ($LASTEXITCODE -eq 0) {
    kubectl -n collabdoc wait --for=delete pod -l app=collabdoc --timeout=2m | Out-Null
} else {
    Write-Warning "Could not reach the cluster; stopping the node anyway."
}

Write-Host "==> Stopping the EC2 node"
aws ec2 stop-instances --region $region --instance-ids $tf.instance_id.value | Out-Null
aws ec2 wait instance-stopped --region $region --instance-ids $tf.instance_id.value
if ($LASTEXITCODE -ne 0) { throw "The node did not stop." }

Write-Host "==> Stopping the database"
aws rds stop-db-instance --region $region --db-instance-identifier $tf.db_identifier.value | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning "The database did not accept the stop (it may already be stopped)." }

Write-Host ""
Write-Host "Stopped. Run .\deploy\start.ps1 to bring it back."
