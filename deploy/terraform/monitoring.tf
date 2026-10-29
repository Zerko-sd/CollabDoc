# Monitoring that stays inside the free allowances: basic EC2/RDS metrics,
# fewer than 10 alarms, one dashboard, two budgets.

variable "alert_email" {
  description = "Where alarms and budget alerts are emailed. Empty: alarms still show in the console, nothing is sent."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Alert when gross usage (before credits) passes this per month."
  type        = number
  default     = 40
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "node_status" {
  alarm_name          = "${var.name}-node-status-check"
  alarm_description   = "The EC2 node failed a status check: the whole app is likely down."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.node.id }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "node_cpu" {
  alarm_name          = "${var.name}-node-cpu"
  alarm_description   = "Node CPU above 80% for 15 minutes."
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = { InstanceId = aws_instance.node.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  alarm_name          = "${var.name}-db-cpu"
  alarm_description   = "Database CPU above 80% for 15 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${var.name}-db-storage"
  alarm_description   = "Less than 2 GB of database storage left."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 2 * 1024 * 1024 * 1024
  alarm_actions       = local.alarm_actions
}

# Budgets count usage before credits are applied (include_credit = false),
# so they show how fast the credits are being spent rather than a net $0.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "notification" {
    for_each = var.alert_email == "" ? [] : [
      { type = "ACTUAL", threshold = 50 },
      { type = "ACTUAL", threshold = 100 },
      { type = "FORECASTED", threshold = 100 },
    ]
    content {
      comparison_operator        = "GREATER_THAN"
      notification_type          = notification.value.type
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "## CollabDoc on k3s\nApp: http://${aws_eip.node.public_ip}  |  Pods: `kubectl -n collabdoc get pods`  |  Memory per pod: `kubectl top pods -n collabdoc`"
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 8, height = 6
        properties = {
          title   = "Node CPU %", region = var.region, stat = "Average", period = 300
          metrics = [["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.node.id]]
        }
      },
      {
        type = "metric", x = 8, y = 2, width = 8, height = 6
        properties = {
          title = "Node network (bytes)", region = var.region, stat = "Sum", period = 300
          metrics = [
            ["AWS/EC2", "NetworkIn", "InstanceId", aws_instance.node.id],
            ["AWS/EC2", "NetworkOut", "InstanceId", aws_instance.node.id],
          ]
        }
      },
      {
        type = "alarm", x = 16, y = 2, width = 8, height = 6
        properties = {
          title = "Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.node_status.arn,
            aws_cloudwatch_metric_alarm.node_cpu.arn,
            aws_cloudwatch_metric_alarm.db_cpu.arn,
            aws_cloudwatch_metric_alarm.db_storage.arn,
          ]
        }
      },
      {
        type = "metric", x = 0, y = 8, width = 8, height = 6
        properties = {
          title   = "Database CPU %", region = var.region, stat = "Average", period = 300
          metrics = [["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.this.identifier]]
        }
      },
      {
        type = "metric", x = 8, y = 8, width = 8, height = 6
        properties = {
          title   = "Database connections", region = var.region, stat = "Maximum", period = 300
          metrics = [["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.this.identifier]]
        }
      },
      {
        type = "metric", x = 16, y = 8, width = 8, height = 6
        properties = {
          title   = "Database free storage (bytes)", region = var.region, stat = "Minimum", period = 300
          metrics = [["AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", aws_db_instance.this.identifier]]
        }
      },
    ]
  })
}
