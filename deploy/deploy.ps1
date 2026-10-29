<#
.SYNOPSIS
  Build the image, push it to ECR, and roll it out to the k3s cluster.

.DESCRIPTION
  Run after `terraform apply` in deploy/terraform. Safe to re-run: every run
  builds a fresh image tag and rolls the app pods one at a time.

.EXAMPLE
  .\deploy\deploy.ps1                # HTTP on the Elastic IP, 3 app pods
  .\deploy\deploy.ps1 -Https         # HTTPS on <ip>.sslip.io via Let's Encrypt
  .\deploy\deploy.ps1 -Replicas 4
#>
param(
    [int]$Replicas = 3,
    [switch]$Https,
    [string]$Tag = (Get-Date -Format "yyyyMMdd-HHmmss")
)

# Not "Stop": Windows PowerShell turns any stderr line from a native command
# (docker's build progress, kubectl's notices) into a terminating error.
# Every native step checks $LASTEXITCODE instead; cmdlets pass -ErrorAction Stop.
$ErrorActionPreference = "Continue"
$deploy = $PSScriptRoot
$root = Split-Path $deploy -Parent

function Invoke-Native {
    # Run a native command and stop on a non-zero exit, which
    # $ErrorActionPreference alone does not do for executables.
    param([scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Command" }
}

function Write-Utf8 {
    # No BOM: Set-Content -Encoding utf8 on Windows PowerShell adds one.
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

Write-Host "==> Reading Terraform outputs"
$tf = terraform "-chdir=$deploy\terraform" output -json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $tf.public_ip) { throw "Run 'terraform apply' in deploy\terraform first." }
$ip = $tf.public_ip.value
$region = $tf.region.value
$repo = $tf.ecr_repository_url.value
$registry = $repo.Split("/")[0]
$image = "${repo}:$Tag"

Write-Host "==> Building and pushing $image"
Invoke-Native { docker build -t $image $root }
# Piped through cmd: a PowerShell 5.1 pipe between two executables re-encodes
# the token and appends CRLF, and ECR answers 400.
cmd /c "aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry"
if ($LASTEXITCODE -ne 0) { throw "docker login to ECR failed" }
Invoke-Native { docker push $image }

Write-Host "==> Fetching the cluster's kubeconfig (the node publishes it once k3s is up)"
$kubeconfig = Join-Path $deploy ".kubeconfig"
$deadline = (Get-Date).AddMinutes(10)
while ($true) {
    $value = aws ssm get-parameter --region $region --name $tf.kubeconfig_parameter.value `
        --with-decryption --query Parameter.Value --output text
    if ($LASTEXITCODE -eq 0 -and $value -and ($value -join "") -ne "pending") { break }
    if ((Get-Date) -gt $deadline) { throw "The node never published its kubeconfig; see /var/log/cloud-init-output.log on it." }
    Write-Host "    still booting..."
    Start-Sleep -Seconds 10
}
Write-Utf8 $kubeconfig (($value -join "`n") + "`n")
$env:KUBECONFIG = $kubeconfig

Write-Host "==> Waiting for the ECR pull secret"
$deadline = (Get-Date).AddMinutes(3)
while (-not (kubectl -n collabdoc get secret ecr-pull --ignore-not-found -o name)) {
    if ((Get-Date) -gt $deadline) { throw "ecr-pull secret missing; check 'systemctl status ecr-pull-secret' on the node." }
    Start-Sleep -Seconds 5
}

Write-Host "==> Writing the app secret"
$secretYaml = kubectl -n collabdoc create secret generic collabdoc `
    "--from-literal=COLLABDOC_SECRET_KEY=$($tf.app_secret_key.value)" `
    "--from-literal=COLLABDOC_DATABASE_URL=$($tf.database_url.value)" `
    --dry-run=client -o yaml
$secretYaml | kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "applying the app secret failed" }

Write-Host "==> Rendering manifests"
$site = if ($Https) { ($ip -replace "\.", "-") + ".sslip.io" } else { ":80" }
$upstreams = (0..($Replicas - 1) | ForEach-Object { "app-$_.app.collabdoc.svc.cluster.local:8000" }) -join " "
$rendered = Join-Path $deploy ".rendered"
New-Item -ItemType Directory -Force -ErrorAction Stop $rendered | Out-Null

$router = (Get-Content -Raw -ErrorAction Stop "$deploy\k8s\router.yaml").Replace("__SITE__", $site).Replace("__UPSTREAMS__", $upstreams)
$sha = [Security.Cryptography.SHA256]::Create()
$hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($router)) | Select-Object -First 8 | ForEach-Object { $_.ToString("x2") })
Write-Utf8 "$rendered\router.yaml" $router.Replace("__CONFIG_HASH__", $hash)

$app = (Get-Content -Raw -ErrorAction Stop "$deploy\k8s\app.yaml").
    Replace("__IMAGE__", $image).
    Replace("__BUCKET__", $tf.upload_bucket.value).
    Replace("__REGION__", $region).
    Replace("__REPLICAS__", "$Replicas")
Write-Utf8 "$rendered\app.yaml" $app
Copy-Item -ErrorAction Stop "$deploy\k8s\kafka.yaml" "$rendered\kafka.yaml"

Write-Host "==> Applying"
Invoke-Native { kubectl apply -f "$rendered\kafka.yaml" }
Invoke-Native { kubectl -n collabdoc rollout status deployment/kafka --timeout=5m }
Invoke-Native { kubectl apply -f "$rendered\app.yaml" }
Invoke-Native { kubectl -n collabdoc rollout status statefulset/app --timeout=10m }
Invoke-Native { kubectl apply -f "$rendered\router.yaml" }
Invoke-Native { kubectl -n collabdoc rollout status deployment/caddy --timeout=5m }

$url = if ($Https) { "https://$site" } else { "http://$ip" }
Write-Host ""
Write-Host "Deployed $image with $Replicas app pods."
Write-Host "  App:      $url"
Write-Host "  Pods:     `$env:KUBECONFIG='$kubeconfig'; kubectl -n collabdoc get pods -o wide"
