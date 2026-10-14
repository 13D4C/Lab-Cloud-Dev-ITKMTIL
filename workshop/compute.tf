resource "aws_key_pair" "workshop" {
  key_name   = "workshop-key"
  public_key = var.ssh_public_key
}

resource "aws_launch_template" "workshop" {
  name                   = "workshop-lt"
  image_id               = var.ami_id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.workshop.key_name
  vpc_security_group_ids = [aws_security_group.ec2.id]
  update_default_version = true
  iam_instance_profile {
    name = var.instance_profile
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/user-data.sh.tftpl", {
    aws_region       = var.aws_region
    alb_dns          = aws_lb.workshop.dns_name
    db_host          = aws_db_instance.workshop.address
    db_password      = var.db_password
    jwt_secret       = var.jwt_secret
    repository       = var.source_repository
    revision         = var.source_revision
    bootstrap_source = base64encode(file("${path.module}/bootstrap.go"))
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "workshop-ec2", Project = "workshop", ManagedBy = "Terraform" }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "workshop-ec2", Project = "workshop", ManagedBy = "Terraform" }
  }
}

resource "aws_autoscaling_group" "workshop" {
  name                      = "workshop-asg"
  min_size                  = 2
  desired_capacity          = 2
  max_size                  = 4
  health_check_type         = "ELB"
  health_check_grace_period = 300
  wait_for_capacity_timeout = "0"
  vpc_zone_identifier       = [aws_subnet.workshop["private-a"].id, aws_subnet.workshop["private-b"].id]
  target_group_arns         = [aws_lb_target_group.frontend.arn, aws_lb_target_group.backend.arn]
  launch_template {
    id      = aws_launch_template.workshop.id
    version = aws_launch_template.workshop.latest_version
  }
  initial_lifecycle_hook {
    name                 = "workshop-bootstrap"
    lifecycle_transition = "autoscaling:EC2_INSTANCE_LAUNCHING"
    heartbeat_timeout    = 900
    default_result       = "CONTINUE"
  }
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 100
      max_healthy_percentage = 200
      instance_warmup        = 300
    }
  }
  dynamic "tag" {
    for_each = { Name = "workshop-ec2", Project = "workshop", ManagedBy = "Terraform" }
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }
  lifecycle {
    ignore_changes = [desired_capacity]
  }
  depends_on = [aws_route_table_association.workshop, aws_lb_listener_rule.api]
}

resource "aws_autoscaling_policy" "cpu" {
  name                   = "workshop-cpu-70"
  autoscaling_group_name = aws_autoscaling_group.workshop.name
  policy_type            = "TargetTrackingScaling"
  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 70
  }
}
