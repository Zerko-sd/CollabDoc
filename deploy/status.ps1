<#
.SYNOPSIS
  One-screen health check: AWS resources, free-plan credits, pods, and the app.
  Read-only; safe to run any time.
#>
$ErrorActionPreference = "Continue"
$deploy = $PSScriptRoot
$tf = terraform "-chdir=$deploy\terraform" output -json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $tf.instance_id) { throw "No Terraform outputs; is the stack deployed?" }
$region = $tf.region.value
$env:KUBECONFIG = Join-Path $deploy ".kubeconfig"

$node = aws ec2 describe-instances --region $region --instance-ids $tf.instance_id.value `
    --query "Reservations[0].Instances[0].State.Name" --output text
$db = aws rds describe-db-instances --region $region --db-instance-identifier $tf.db_identifier.value `
    --query "DBInstances[0].DBInstanceStatus" --output text
$plan = aws freetier get-account-plan-state --region us-east-1 | ConvertFrom-Json
$alarms = aws cloudwatch describe-alarms --region $region --alarm-name-prefix collabdoc `
    --query "MetricAlarms[].[AlarmName,StateValue]" --output text

Write-Host "AWS"
Write-Host ("  EC2 node      {0}" -f $node)
Write-Host ("  RDS database  {0}" -f $db)
if ($plan) {
    Write-Host ("  Credits left  {0} {1} ({2} plan, expires {3})" -f $plan.accountPlanRemainingCredits.amount,
        $plan.accountPlanRemainingCredits.unit, $plan.accountPlanType, $plan.accountPlanExpirationDate.Substring(0, 10))
}
Write-Host "  Alarms"
$alarms | ForEach-Object { Write-Host ("    {0}" -f ($_ -replace "`t", "  ")) }

if ($node -eq "running") {
    Write-Host ""
    Write-Host "Kubernetes"
    kubectl -n collabdoc get pods --request-timeout=10s
    kubectl top pods -n collabdoc --request-timeout=10s 2>$null
    $url = "http://$($tf.public_ip.value)/api/health"
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 $url
        Write-Host ""
        Write-Host ("App  {0} -> {1} {2} (served by {3})" -f $url, $r.StatusCode, $r.Content, $r.Headers["X-Served-By"])
    } catch {
        Write-Host ""
        Write-Host "App  $url -> DOWN ($($_.Exception.Message))"
    }
}
Write-Host ""
Write-Host "Dashboard: $($tf.dashboard_url.value)"
