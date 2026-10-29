output "public_ip" {
  value = aws_eip.node.public_ip
}

output "region" {
  value = var.region
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "upload_bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "kubeconfig_parameter" {
  value = aws_ssm_parameter.kubeconfig.name
}

output "instance_id" {
  value = aws_instance.node.id
}

output "database_url" {
  value     = "postgresql+psycopg://collabdoc:${random_password.db.result}@${aws_db_instance.this.address}:5432/collabdoc"
  sensitive = true
}

output "app_secret_key" {
  value     = random_password.app_secret.result
  sensitive = true
}

output "db_identifier" {
  value = aws_db_instance.this.identifier
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.this.dashboard_name}"
}
