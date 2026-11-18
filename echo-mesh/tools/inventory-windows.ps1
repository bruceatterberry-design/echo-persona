# Echo Mesh read-only inventory (Windows). Changes nothing, sends nothing.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File inventory-windows.ps1
$o = [ordered]@{}
$o.host = $env:COMPUTERNAME
$o.os = (Get-CimInstance Win32_OperatingSystem).Caption + " " + (Get-CimInstance Win32_OperatingSystem).Version
$c = Get-CimInstance Win32_Processor | Select-Object -First 1
$o.cpu = $c.Name; $o.cores = $c.NumberOfCores; $o.threads = $c.NumberOfLogicalProcessors
$o.ram_gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)
$o.gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name) | driver $($_.DriverVersion)" })
$o.disk_free_gb = @(Get-PSDrive -PSProvider FileSystem | Where-Object Free | ForEach-Object { "$($_.Name): $([math]::Round($_.Free/1GB,0))" })
$o.python = (& { try { python --version 2>&1 } catch { "none" } })
$o.git = (& { try { git --version 2>&1 } catch { "none" } })
$o.ollama = (& { try { ollama --version 2>&1 } catch { "none" } })
$o.ollama_models = (& { try { (ollama list 2>&1) -join "; " } catch { "none" } })
$o.nvidia_smi = (& { try { (nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1) -join "; " } catch { "none" } })
$o.tailscale = (& { try { tailscale version 2>&1 | Select-Object -First 1 } catch { "none" } })
$o.listening_ai_ports = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 11434,52415,8080,1234,5000,47810 } | ForEach-Object { $_.LocalPort })
$o.lan_ip = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "10.*" -or $_.IPAddress -like "192.168.*" } | ForEach-Object { $_.IPAddress })
$o.ai_processes = @(Get-Process | Where-Object { $_.ProcessName -match "ollama|llama|lmstudio|comfy|python" } | Select-Object -ExpandProperty ProcessName -Unique)
$o | ConvertTo-Json -Depth 3
