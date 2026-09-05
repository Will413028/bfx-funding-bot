output "bucket_name" {
  description = "R2 bucket name for pgBackRest."
  value       = cloudflare_r2_bucket.backup.name
}

output "s3_endpoint" {
  description = "Account-scoped R2 S3 endpoint for pgBackRest."
  value       = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
}

output "s3_region" {
  description = "R2 S3 region value."
  value       = "auto"
}

output "s3_uri_style" {
  description = "R2 S3 URI style."
  value       = "path"
}

output "repo_path" {
  description = "pgBackRest repository path within the bucket."
  value       = "/pgbackrest"
}
