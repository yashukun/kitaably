# Terraform — Phase 10

Nothing user-visible. A deployed environment.

```
modules/
  network/        VPC, subnets, NAT, security groups
  eks/            cluster, node groups, addons, OIDC provider
  ecr/            one repository per image, lifecycle policy
  irsa/           IAM roles for service accounts, least privilege per workload
  observability/  prometheus/grafana or managed equivalents
envs/
  dev/            backend.tf (remote state), main.tf, terraform.tfvars.example
  prod/           same modules, separate state, different sizing
```

- Remote state in S3 with DynamoDB locking, created once by hand (the bootstrap
  chicken-and-egg) and never destroyed by a plan.
- `dev` and `prod` are separate state files sharing modules. Never one workspace
  with a `count` on the environment.
- No secret values in committed `.tf` or `.tfvars`. Secrets live in SSM Parameter
  Store / Secrets Manager and are read by the cluster.
- `terraform plan` output is a review artefact: it goes on the pull request.
