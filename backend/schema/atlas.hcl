env "neon" {
  src = "file://schema.hcl"
  url = getenv("DATABASE_URL")
  dev = "docker://postgres/18/dev?search_path=public"
  migration {
    dir            = "file://../migrations"
    revisions_schema = "public"
  }
}
