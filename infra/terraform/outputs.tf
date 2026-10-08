output "controlnode_urls" {
  description = "URLs públicas de los dos ControlNodes"
  value       = join(",", [for name in keys(local.controlnodes) : "https://${aws_instance.node[name].public_ip}:8000"])
}

output "comando_config" {
  description = "Comando para configurar el cliente DFSha contra este despliegue"
  value       = "dfsha config ${join(",", [for name in keys(local.controlnodes) : "https://${aws_instance.node[name].public_ip}:8000"])} certs/ca.crt"
}

output "ips_publicas" {
  description = "IP pública de cada nodo"
  value       = { for name, instance in aws_instance.node : name => instance.public_ip }
}

output "ssh" {
  description = "Comandos SSH para entrar a cada nodo (labsuser.pem se descarga del Learner Lab)"
  value       = { for name, instance in aws_instance.node : name => "ssh -i labsuser.pem ubuntu@${instance.public_ip}" }
}
