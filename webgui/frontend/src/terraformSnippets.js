// Ready-made Terraform (HCL) snippets for the IDE "Insert resource" palette. Each
// item's `yaml` (HCL, really) is a complete block inserted at the cursor. Grouped
// like the Ansible task library so the two engines feel the same; `search` also
// matches on keywords/resource types. AWS is the worked example because it's the
// most common starting point — swap the provider block for gcp/azurerm and the
// shape carries over.
const t = (name, search, yaml) => ({ name, search, yaml })

export const SNIPPET_GROUPS = [
  {
    group: 'Providers & Setup',
    items: [
      t('Terraform + required providers', 'terraform block version providers', `terraform {\n  required_version = ">= 1.5"\n  required_providers {\n    aws = {\n      source  = "hashicorp/aws"\n      version = "~> 5.0"\n    }\n  }\n}\n`),
      t('AWS provider', 'provider aws region', `provider "aws" {\n  region = var.region\n}\n`),
      t('Remote state (S3 backend)', 'backend s3 remote state', `terraform {\n  backend "s3" {\n    bucket = "my-tf-state"\n    key    = "env/terraform.tfstate"\n    region = "us-east-1"\n  }\n}\n`),
      t('Variable', 'variable input var', `variable "region" {\n  description = "AWS region to deploy into"\n  type        = string\n  default     = "us-east-1"\n}\n`),
      t('Output', 'output export value', `output "instance_ip" {\n  description = "Public IP of the instance"\n  value       = aws_instance.app.public_ip\n}\n`),
      t('Locals', 'locals computed local', `locals {\n  name_prefix = "\${var.environment}-app"\n  common_tags = {\n    Environment = var.environment\n    ManagedBy   = "SLEP"\n  }\n}\n`),
      t('Data source (AMI lookup)', 'data source ami lookup', `data "aws_ami" "ubuntu" {\n  most_recent = true\n  owners      = ["099720109477"] # Canonical\n  filter {\n    name   = "name"\n    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]\n  }\n}\n`),
    ],
  },
  {
    group: 'Compute',
    items: [
      t('EC2 instance', 'aws_instance ec2 vm server', `resource "aws_instance" "app" {\n  ami                    = data.aws_ami.ubuntu.id\n  instance_type          = var.instance_type\n  key_name               = aws_key_pair.deploy.key_name\n  vpc_security_group_ids = [aws_security_group.app.id]\n  subnet_id              = aws_subnet.public.id\n  tags = merge(local.common_tags, { Name = "\${local.name_prefix}" })\n}\n`),
      t('SSH key pair', 'aws_key_pair ssh key', `resource "aws_key_pair" "deploy" {\n  key_name   = "\${local.name_prefix}-deploy"\n  public_key = file("\${path.module}/files/id_ed25519.pub")\n}\n`),
      t('Security group', 'aws_security_group firewall sg', `resource "aws_security_group" "app" {\n  name_prefix = "\${local.name_prefix}-"\n  vpc_id      = aws_vpc.main.id\n  ingress {\n    from_port   = 22\n    to_port     = 22\n    protocol    = "tcp"\n    cidr_blocks = ["0.0.0.0/0"]\n  }\n  egress {\n    from_port   = 0\n    to_port     = 0\n    protocol    = "-1"\n    cidr_blocks = ["0.0.0.0/0"]\n  }\n  tags = local.common_tags\n}\n`),
      t('Auto Scaling Group + launch template', 'asg autoscaling launch template', `resource "aws_launch_template" "app" {\n  name_prefix   = "\${local.name_prefix}-"\n  image_id      = data.aws_ami.ubuntu.id\n  instance_type = var.instance_type\n}\n\nresource "aws_autoscaling_group" "app" {\n  desired_capacity    = 2\n  max_size            = 4\n  min_size            = 1\n  vpc_zone_identifier = [aws_subnet.public.id]\n  launch_template {\n    id      = aws_launch_template.app.id\n    version = "$Latest"\n  }\n}\n`),
    ],
  },
  {
    group: 'Networking',
    items: [
      t('VPC', 'aws_vpc network', `resource "aws_vpc" "main" {\n  cidr_block           = "10.0.0.0/16"\n  enable_dns_hostnames = true\n  tags                 = merge(local.common_tags, { Name = "\${local.name_prefix}-vpc" })\n}\n`),
      t('Subnet', 'aws_subnet', `resource "aws_subnet" "public" {\n  vpc_id                  = aws_vpc.main.id\n  cidr_block              = "10.0.1.0/24"\n  map_public_ip_on_launch = true\n  tags                    = merge(local.common_tags, { Name = "\${local.name_prefix}-public" })\n}\n`),
      t('Internet gateway', 'aws_internet_gateway igw', `resource "aws_internet_gateway" "main" {\n  vpc_id = aws_vpc.main.id\n  tags   = local.common_tags\n}\n`),
      t('Route table + association', 'aws_route_table routing', `resource "aws_route_table" "public" {\n  vpc_id = aws_vpc.main.id\n  route {\n    cidr_block = "0.0.0.0/0"\n    gateway_id = aws_internet_gateway.main.id\n  }\n}\n\nresource "aws_route_table_association" "public" {\n  subnet_id      = aws_subnet.public.id\n  route_table_id = aws_route_table.public.id\n}\n`),
    ],
  },
  {
    group: 'Storage & Databases',
    items: [
      t('S3 bucket', 'aws_s3_bucket object storage', `resource "aws_s3_bucket" "assets" {\n  bucket = "\${local.name_prefix}-assets"\n  tags   = local.common_tags\n}\n\nresource "aws_s3_bucket_versioning" "assets" {\n  bucket = aws_s3_bucket.assets.id\n  versioning_configuration { status = "Enabled" }\n}\n`),
      t('EBS volume + attachment', 'aws_ebs_volume disk block', `resource "aws_ebs_volume" "data" {\n  availability_zone = aws_instance.app.availability_zone\n  size              = 20\n  type              = "gp3"\n  tags              = local.common_tags\n}\n\nresource "aws_volume_attachment" "data" {\n  device_name = "/dev/sdf"\n  volume_id   = aws_ebs_volume.data.id\n  instance_id = aws_instance.app.id\n}\n`),
      t('RDS database', 'aws_db_instance rds postgres mysql', `resource "aws_db_instance" "db" {\n  identifier           = "\${local.name_prefix}-db"\n  engine               = "postgres"\n  engine_version       = "16"\n  instance_class       = "db.t3.micro"\n  allocated_storage    = 20\n  db_name              = "app"\n  username             = "app"\n  password             = var.db_password\n  skip_final_snapshot  = true\n  tags                 = local.common_tags\n}\n`),
    ],
  },
  {
    group: 'DNS & Load Balancing',
    items: [
      t('Route53 record', 'aws_route53_record dns', `resource "aws_route53_record" "app" {\n  zone_id = var.zone_id\n  name    = "app.example.com"\n  type    = "A"\n  ttl     = 300\n  records = [aws_instance.app.public_ip]\n}\n`),
      t('Application load balancer', 'aws_lb alb load balancer', `resource "aws_lb" "app" {\n  name               = "\${local.name_prefix}-alb"\n  load_balancer_type = "application"\n  subnets            = [aws_subnet.public.id]\n  security_groups    = [aws_security_group.app.id]\n  tags               = local.common_tags\n}\n`),
    ],
  },
  {
    group: 'Kubernetes & Helm',
    items: [
      t('Kubernetes namespace', 'kubernetes_namespace k8s', `resource "kubernetes_namespace" "app" {\n  metadata {\n    name = "app"\n  }\n}\n`),
      t('Helm release', 'helm_release chart k8s', `resource "helm_release" "app" {\n  name       = "app"\n  namespace  = kubernetes_namespace.app.metadata[0].name\n  repository = "https://charts.example.com"\n  chart      = "app"\n  version    = "1.2.3"\n}\n`),
    ],
  },
  {
    group: 'Generic & Local',
    items: [
      t('Module', 'module reuse', `module "network" {\n  source      = "./modules/network"\n  environment = var.environment\n  cidr_block  = "10.0.0.0/16"\n}\n`),
      t('null_resource + local-exec', 'null_resource provisioner local-exec shell', `resource "null_resource" "bootstrap" {\n  triggers = { instance_id = aws_instance.app.id }\n  provisioner "local-exec" {\n    command = "echo Provisioned \${aws_instance.app.public_ip}"\n  }\n}\n`),
      t('local_file', 'local_file write file', `resource "local_file" "inventory" {\n  filename = "\${path.module}/inventory.ini"\n  content  = "\${aws_instance.app.public_ip}\\n"\n}\n`),
      t('random_password', 'random_password secret generate', `resource "random_password" "db" {\n  length  = 24\n  special = true\n}\n`),
    ],
  },
]
