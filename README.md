# bash-pve-utils 🛠️

Script de automatización híbrido (Python 3 + Bash remoto) para el aprovisionamiento, configuración e idempotencia de contenedores LXC en entornos Proxmox VE (PVE). 

Está pensado bajo el enfoque de **Infrastructure as Code (IaC)**, lo que te permite declarar el estado deseado de tus máquinas en un archivo JSON local y aplicarlo al servidor con un solo comando.

---

## 🚀 Características Clave

* **Orquestación Remota sin Dependencias:** La CLI corre en tu PC local usando Python 3 estándar (sin librerías de terceros) y utiliza el cliente SSH nativo del sistema. El nodo Proxmox VE no requiere instalar nada.
* **Idempotencia (Cambios en Caliente):** Si el contenedor LXC ya existe, compara la configuración y actualiza en caliente (hotplug) los cores de **CPU**, **RAM**, límites de **Swap** y el inicio automático (`onboot`) sin reiniciar el LXC si no es necesario.
* **Control de SSH Keys:** Inyección automática de llaves públicas autorizadas para desactivar accesos inseguros de root por contraseña.
* **Descarga Automática de Templates:** Verifica la existencia de templates LXC (Debian, Alpine, etc.) y ejecuta `pveam download` remotamente si no están descargados.
* **Multiplexación SSH (ControlMaster):** Abre una conexión maestra de SSH en segundo plano. Si no tenés llaves configuradas y te pide password, **solo la vas a ingresar una vez**. Todas las transferencias de archivos y comandos siguientes se ejecutarán al instante por el mismo socket de forma transparente y segura.

---

## 🛠️ Requisitos

* **Local (Tu PC):** Python 3.x, cliente SSH nativo (`ssh`, `scp`) compatible con sistemas POSIX (Linux/macOS).
* **Remoto (Servidor Proxmox):** Acceso SSH habilitado. Se recomienda correr como `root` o un usuario con permisos `sudo` sin contraseña.

---

## ⚙️ Configuración (`config.json`)

Creá un archivo `config.json` en base al ejemplo `config.json.example`.

```json
{
  "storage": "local-lvm",
  "template_storage": "local",
  "containers": [
    {
      "vmid": 900,
      "hostname": "net-probe-host",
      "cores": 2,
      "memory": 1024,
      "swap": 512,
      "disk": "8G",
      "ostemplate": "debian-12-standard_12.2-1_amd64.tar.zst",
      "bridge": "vmbr0",
      "ip": "dhcp",
      "ssh_key": "ssh-ed25519 AAAAC3...",
      "onboot": 1,
      "bootstrap": "apt-get update && apt-get install -y curl"
    }
  ]
}
```

---

## 💻 Comandos de la CLI

El script principal es `forge.py`. Para ejecutarlo, usá:

### 1. Aplicar cambios (Provisionamiento/Actualización)
Compara el archivo de configuración con el estado real del servidor y aplica los cambios.
```bash
python3 forge.py <IP_SERVIDOR_PVE> --user root --port 22 --config config.json apply
```
* **Modo Dry Run (Lectura y simulación):**
  Agregá `--dry-run` para verificar qué cambios se aplicarían sin tocar nada en el servidor.
  ```bash
  python3 forge.py <IP_SERVIDOR_PVE> apply --dry-run
  ```

### 2. Estado de los contenedores
Muestra una tabla con el estado actual (running, stopped, not found) de todos los contenedores declarados en tu JSON local.
```bash
python3 forge.py <IP_SERVIDOR_PVE> status
```

### 3. Eliminar contenedor
Detiene y elimina un contenedor específico por su VMID de forma segura pidiendo confirmación interactiva.
```bash
python3 forge.py <IP_SERVIDOR_PVE> destroy 900
```
