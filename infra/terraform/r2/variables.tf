variable "cloudflare_account_id" {
  type        = string
  description = "32-character Cloudflare account ID."

  validation {
    condition     = can(regex("^[a-f0-9]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a lowercase 32-character hexadecimal ID."
  }
}

variable "backup_bucket_name" {
  type        = string
  description = "Dedicated private R2 bucket for pgBackRest objects."

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.backup_bucket_name))
    error_message = "backup_bucket_name must be 3-63 lowercase characters, digits, or hyphens."
  }
}

variable "bucket_location" {
  type        = string
  default     = null
  nullable    = true
  description = "Optional first-create R2 location hint; null leaves the provider default."

  validation {
    condition     = var.bucket_location == null || contains(["apac", "eeur", "enam", "weur", "wnam", "oc"], var.bucket_location)
    error_message = "bucket_location must be one of apac, eeur, enam, weur, wnam, or oc when set."
  }
}
