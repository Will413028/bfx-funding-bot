env "neon" {
  src = "file://schema.hcl"
  url = getenv("DATABASE_URL")
  dev = "docker://postgres/17/dev?search_path=public"
  migration {
    dir = "file://migrations"
  }
}
