# bash-pve-utils 🛠️

Inspector de servidores Proxmox VE vía SSH. Conectá, escaneá recursos, containers y VMs al toque.

## Requisitos

- **Local:** Python 3.x, SSH client (`ssh`, `scp`)
- **Remoto:** Servidor Proxmox VE con SSH habilitado
- **Opcional:** `sshpass` (`sudo apt install sshpass`) si usás autenticación por password

## Uso

```bash
# Modo interactivo (te pide IP, usuario, puerto, password)
python3 forge.py

# O直接 con argumentos
python3 forge.py 192.168.1.100 --user root --port 22 --password
```

## Output

```
  ▶  Connecting to 10.250.4.23...
  ▷  Fetching server resources...
  ▷  Fetching containers...
  ▷  Fetching VMs...

  ■  alcaravan (10.250.4.23)
     PVE pve-manager/8.2.4  ·  up 1 day, 19 hours

  ⚙  System Resources
     CPU:  Intel(R) Xeon(R) Gold 5318Y  (96 cores)
     RAM:  33G / 125G  (avail: 91G)
     SWAP: 236M / 8.0G
     DISK: 492G / 492G  (free: 0)

  ☰  Containers (LXC)  (33 total)
  ┌──────┬──────────────────────┬──────────┬──────┬───────┬───────┬─────────────┐
  │ VMID │ Hostname             │ Status   │ CPU  │ RAM   │ Swap  │ IP          │
  ├──────┼──────────────────────┼──────────┼──────┼───────┼───────┼─────────────┤
  │ 100  │ vps2                 │ running  │ 12   │ 31.2G │ 3.9G  │ 10.250.4.241│
  │ 101  │ vps3                 │ running  │ 12   │ 31.2G │ 3.9G  │ 10.250.4.242│
  └──────┴──────────────────────┴──────────┴──────┴───────┴───────┴─────────────┘

  ▣  Virtual Machines (QEMU)  (none)
  Done.
```

## ¿Sin permisos?

Si el usuario SSH no tiene acceso a `pct`/`qm`, el script intenta automáticamente:

1. `pct list` directo
2. `sudo -n pct list` (passwordless sudo)
3. `echo <password> | sudo -S pct list` (con la misma password que ingresaste)
