#!/usr/bin/env bash
# Echo Mesh read-only inventory (Linux/Omarchy). Changes nothing, sends nothing.
# Run:  bash inventory-linux.sh
echo "host: $(hostname)"; echo "os: $(. /etc/os-release; echo $PRETTY_NAME) kernel $(uname -r)"
echo "cpu: $(lscpu | sed -n 's/^Model name: *//p') threads=$(nproc)"
echo "ram: $(free -g | awk '/Mem:/{print $2" GB total, "$7" GB available"}')"
echo "gpus:"; lspci 2>/dev/null | grep -iE "vga|3d|display" | sed 's/^/  /'
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | sed 's/^/  nvidia: /'
echo "disk free: $(df -h / | awk 'NR==2{print $4}')"
for c in python3 git ollama llama-server tailscale pacman; do printf "%s: " $c; (command -v $c >/dev/null && ($c --version 2>&1 | head -1)) || echo none; done
echo "ollama models:"; (ollama list 2>/dev/null || echo "  none") | sed 's/^/  /'
echo "ai ports listening:"; ss -ltn 2>/dev/null | grep -E ":(11434|52415|8080|1234|47810)\b" | sed 's/^/  /' || true
echo "lan ip: $(ip -4 -o addr show | awk '{print $4}' | grep -E '^(10\.|192\.168\.)' | tr '\n' ' ')"
