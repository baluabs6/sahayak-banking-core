# Terraform for Sahayak Banking Core's AWS footprint.
# This is reference infrastructure-as-code — review, adjust CIDRs/sizing,
# and run through your org's security review before applying.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  default = "ap-south-1" # Mumbai — data residency for Indian banking data
}

variable "environment" {
  default = "staging"
}

# --- S3: KYC documents (encrypted, versioned) ---
resource "aws_s3_bucket" "kyc_docs" {
  bucket = "sahayak-kyc-documents-${var.environment}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kyc_docs_encryption" {
  bucket = aws_s3_bucket.kyc_docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_versioning" "kyc_docs_versioning" {
  bucket = aws_s3_bucket.kyc_docs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "kyc_docs_block_public" {
  bucket                  = aws_s3_bucket.kyc_docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- S3: Data lake landing zone (batch exports for analytics/model training) ---
resource "aws_s3_bucket" "data_lake" {
  bucket = "sahayak-data-lake-${var.environment}"
}

# --- DynamoDB: sessions (TTL-based) ---
resource "aws_dynamodb_table" "sessions" {
  name         = "sahayak-user-sessions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# --- DynamoDB: real-time fraud velocity signals ---
resource "aws_dynamodb_table" "fraud_signals" {
  name         = "sahayak-fraud-signals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "window_key"

  attribute {
    name = "user_id"
    type = "S"
  }
  attribute {
    name = "window_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# --- SNS topics ---
resource "aws_sns_topic" "fraud_alerts" {
  name = "fraud-alerts"
}

resource "aws_sns_topic" "loan_notifications" {
  name = "loan-notifications"
}

# --- CloudWatch: alarm on elevated fraud-flag rate ---
resource "aws_cloudwatch_metric_alarm" "high_fraud_flag_rate" {
  alarm_name          = "high-fraud-flag-rate"
  namespace           = "SahayakBankingCore/Backend"
  metric_name         = "FraudFlagsRaised"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 50
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.fraud_alerts.arn]
}

# --- EC2: application host (dev/staging single-instance; use ECS/EKS for prod HA) ---
resource "aws_instance" "app_server" {
  ami                    = var.app_ami_id
  instance_type          = "t3.medium"
  vpc_security_group_ids = [aws_security_group.app_sg.id]

  tags = {
    Name        = "sahayak-banking-core-${var.environment}"
    Environment = var.environment
  }
}

variable "app_ami_id" {
  description = "AMI ID for the app server (Ubuntu 22.04 LTS recommended)"
}

resource "aws_security_group" "app_sg" {
  name        = "sahayak-banking-core-sg"
  description = "Allow HTTPS inbound, app port from API Gateway VPC link"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"] # restrict to VPC/API Gateway VPC link range
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

output "kyc_bucket_name" {
  value = aws_s3_bucket.kyc_docs.bucket
}

output "app_server_public_ip" {
  value = aws_instance.app_server.public_ip
}
