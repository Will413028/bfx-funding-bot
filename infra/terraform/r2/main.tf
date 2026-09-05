resource "cloudflare_r2_bucket" "backup" {
  account_id    = var.cloudflare_account_id
  name          = var.backup_bucket_name
  location      = var.bucket_location
  storage_class = "Standard"
}

resource "cloudflare_r2_bucket_lifecycle" "backup" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.backup.name

  rules = [{
    id = "abort-incomplete-multipart-uploads"
    conditions = {
      prefix = ""
    }
    enabled = true
    abort_multipart_uploads_transition = {
      condition = {
        type    = "Age"
        max_age = 86400
      }
    }
  }]
}
