#!/usr/bin/env python3

import os
import sys
import json
import subprocess
from pathlib import Path
from init_paths import *
from paths import *

sys.path.insert(0, str(Path(__file__).parent.parent / "tls"))
from cleanup_acme_challenge import cleanup_cloudns, cleanup_cloudflare

HYSTERIA_SSL_DIR = "/etc/hysteria/ssl"
ACME_SH = os.path.expanduser("~/.acme.sh/acme.sh")


def run(cmd, env=None, check=False):
    return subprocess.run(cmd, shell=True, env=env, check=check)


def install_acmesh(domain):
    if os.path.isfile(ACME_SH) and os.access(ACME_SH, os.X_OK):
        return True
    print("Installing acme.sh...")
    ret = run(f"curl -sSL https://get.acme.sh | sh -s email=admin@{domain}")
    return ret.returncode == 0


def issue_cert(domain, method, cred1="", cred2=""):
    cert_dir = os.path.join(HYSTERIA_SSL_DIR, domain)
    os.makedirs(cert_dir, exist_ok=True)

    if not install_acmesh(domain):
        print("Error: failed to install acme.sh")
        return False

    print(f"Issuing TLS certificate for {domain} via {method}...")
    env = os.environ.copy()

    if method == "http01":
        cmd = f"{ACME_SH} --issue -d {domain} --standalone --server letsencrypt"
    elif method == "dns01-cf":
        env["CF_Token"] = cred1
        try:
            cleanup_cloudflare(domain, cred1)
        except Exception:
            pass
        cmd = f"{ACME_SH} --issue --dns dns_cf -d {domain} --server letsencrypt --dnssleep 30"
    elif method == "dns01-cloudns":
        env["CLOUDNS_AUTH_ID"] = cred1
        env["CLOUDNS_AUTH_PASSWORD"] = cred2
        try:
            cleanup_cloudns(domain, cred1, cred2)
        except Exception:
            pass
        cmd = f"{ACME_SH} --issue --dns dns_cloudns -d {domain} --server letsencrypt --dnssleep 30"
    else:
        print(f"Unknown TLS method: {method}")
        return False

    ret = subprocess.run(cmd, shell=True, env=env)
    # returncode 2 = cert already valid, acme.sh skipped renewal — treat as success
    if ret.returncode not in (0, 2):
        print(f"Certificate issuance failed for {domain}")
        return False

    reload_cmd = "systemctl restart hysteria-server.service"
    install_cmd = (
        f"{ACME_SH} --install-cert -d {domain} "
        f"--fullchain-file {cert_dir}/fullchain.pem "
        f"--key-file {cert_dir}/key.pem "
        f"--reloadcmd '{reload_cmd}'"
    )
    subprocess.run(install_cmd, shell=True)
    subprocess.run(f"chown hysteria:hysteria {cert_dir}/fullchain.pem {cert_dir}/key.pem", shell=True)
    subprocess.run(f"chmod 640 {cert_dir}/fullchain.pem {cert_dir}/key.pem", shell=True)
    print(f"Certificate deployed to {cert_dir}")
    return True


def update_config(domain):
    cert_dir = os.path.join(HYSTERIA_SSL_DIR, domain)
    fullchain = os.path.join(cert_dir, "fullchain.pem")
    key = os.path.join(cert_dir, "key.pem")

    if not os.path.isfile(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found")
        return False

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    config.setdefault("tls", {})
    config["tls"]["cert"] = fullchain
    config["tls"]["key"] = key
    config["tls"].pop("pinSHA256", None)
    config["tls"].pop("insecure", None)
    config.pop("obfs", None)

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    print(f"config.json updated with cert paths for {domain}")
    return True


def update_sni_env(domain):
    lines = []
    sni_found = False

    if os.path.isfile(CONFIG_ENV):
        with open(CONFIG_ENV, "r") as f:
            for line in f:
                if line.startswith("SNI="):
                    lines.append(f"SNI={domain}\n")
                    sni_found = True
                else:
                    lines.append(line)

    if not sni_found:
        lines.append(f"SNI={domain}\n")

    with open(CONFIG_ENV, "w") as f:
        f.writelines(lines)

    print(f"SNI updated to {domain} in {CONFIG_ENV}")


def update_sni(domain, method="http01", cred1="", cred2=""):
    if not domain:
        print("Error: domain is required")
        return 1

    if not issue_cert(domain, method, cred1, cred2):
        return 1

    if not update_config(domain):
        return 1

    update_sni_env(domain)

    subprocess.run(
        f"python3 {CLI_PATH} restart-hysteria2 > /dev/null 2>&1",
        shell=True,
    )
    print(f"Hysteria2 restarted with new domain: {domain}")
    print(f"TLS: Let's Encrypt ({method}), insecure mode DISABLED")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <domain> [http01|dns01-cf|dns01-cloudns] [cred1] [cred2]")
        sys.exit(1)

    _domain = sys.argv[1]
    _method = sys.argv[2] if len(sys.argv) > 2 else "http01"
    _cred1 = sys.argv[3] if len(sys.argv) > 3 else ""
    _cred2 = sys.argv[4] if len(sys.argv) > 4 else ""

    sys.exit(update_sni(_domain, _method, _cred1, _cred2))
