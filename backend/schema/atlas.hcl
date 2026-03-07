env "local" {
  src = "file://schema.hcl"
  dev = "docker://postgres/17/dev?search_path=public"
  migration {
    dir = "file://migrations"
  }
}

env "neon" {
  src = "file://schema.hcl"
  url = getenv("DATABASE_URL")
  migration {
    dir = "file://migrations"
  }
}
