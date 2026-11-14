output "public_ip" {
  value = aws_instance.aegis.public_ip
}

output "urls" {
  value = { for name, port in local.ports : name => "http://${aws_instance.aegis.public_dns}:${port}" }
}

output "bootstrap_log" {
  value = "Bootstrap takes ~6-8 minutes. Progress: sudo tail -f /var/log/aegis-bootstrap.log (or EC2 console > Get system log)"
}
