module "https_pinned" {
  source = "git::https://github.com/example-org/tf-network.git?ref=v4.2.0"
}

module "scp_floating" {
  source = "git@github.com:example-org/tf-storage.git"
}

module "ssh_url" {
  source = "ssh://git@github.com/example-org/tf-queue.git?ref=main"
}

module "bare_with_subdir" {
  source = "github.com/example-org/tf-dns//modules/zone?ref=v1.0.1"
}

module "bare_no_git_suffix" {
  source = "github.com/example-org/tf-cache"
}

module "registry" {
  source  = "hashicorp/consul/aws"
  version = "0.1.0"
}

module "local" {
  source = "../modules/local-thing"
}
