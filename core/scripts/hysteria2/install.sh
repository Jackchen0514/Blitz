#!/bin/bash

source /etc/hysteria/core/scripts/path.sh
source /etc/hysteria/core/scripts/utils.sh
source /etc/hysteria/core/scripts/scheduler.sh
define_colors

HYSTERIA_SSL_DIR="/etc/hysteria/ssl"
ACME_SH="$HOME/.acme.sh/acme.sh"

_install_acmesh() {
    local domain=$1
    if [[ -x "$ACME_SH" ]]; then
        return 0
    fi
    echo "Installing acme.sh..."
    curl -sSL https://get.acme.sh | sh -s email="admin@${domain}" > /dev/null 2>&1
    # shellcheck source=/dev/null
    source "$HOME/.acme.sh/acme.sh.env" 2>/dev/null || true
}

issue_hysteria_cert() {
    local domain=$1
    local method=$2
    local cred1=$3
    local cred2=$4
    local cert_dir="$HYSTERIA_SSL_DIR/$domain"

    _install_acmesh "$domain"
    mkdir -p "$cert_dir"

    echo "Issuing TLS certificate for $domain via $method..."
    case "$method" in
        http01)
            "$ACME_SH" --issue -d "$domain" --standalone --server letsencrypt
            ;;
        dns01-cf)
            export CF_Token="$cred1"
            python3 /etc/hysteria/core/scripts/tls/cleanup_acme_challenge.py cloudflare "$domain" "$cred1" 2>/dev/null || true
            "$ACME_SH" --issue --dns dns_cf -d "$domain" --server letsencrypt --dnssleep 30
            ;;
        dns01-cloudns)
            export CLOUDNS_AUTH_ID="$cred1"
            export CLOUDNS_AUTH_PASSWORD="$cred2"
            python3 /etc/hysteria/core/scripts/tls/cleanup_acme_challenge.py cloudns "$domain" "$cred1" "$cred2" 2>/dev/null || true
            "$ACME_SH" --issue --dns dns_cloudns -d "$domain" --server letsencrypt --dnssleep 30
            ;;
        *)
            echo -e "${red}Unknown TLS method: $method${NC}"
            exit 1
            ;;
    esac

    local acme_exit=$?
    # exit code 2 = already valid, skip renewal — treat as success
    if [[ $acme_exit -ne 0 && $acme_exit -ne 2 ]]; then
        echo -e "${red}Error: certificate issuance failed for $domain${NC}"
        exit 1
    fi

    "$ACME_SH" --install-cert -d "$domain" \
        --fullchain-file "$cert_dir/fullchain.pem" \
        --key-file "$cert_dir/key.pem" \
        --reloadcmd "systemctl restart hysteria-server.service" > /dev/null 2>&1

    chmod 640 "$cert_dir/fullchain.pem" "$cert_dir/key.pem"
    echo "Certificate deployed to $cert_dir"
}

install_hysteria() {
    local port=$1
    local domain=$2
    local tls_method=${3:-http01}
    local cred1=${4:-}
    local cred2=${5:-}

    echo "Installing Hysteria2..."
    bash <(curl -fsSL https://get.hy2.sh/) >/dev/null 2>&1

    mkdir -p /etc/hysteria && cd /etc/hysteria/

    echo "Downloading geo data..."
    wget -O /etc/hysteria/geosite.dat https://raw.githubusercontent.com/Chocolate4U/Iran-v2ray-rules/release/geosite.dat >/dev/null 2>&1
    wget -O /etc/hysteria/geoip.dat https://raw.githubusercontent.com/Chocolate4U/Iran-v2ray-rules/release/geoip.dat >/dev/null 2>&1

    if [[ $port =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )); then
        if ss -tuln | grep -q ":$port\b"; then
            echo -e "${red}Port $port is already in use. Please choose another port.${NC}"
            exit 1
        fi
    else
        echo "Invalid port number. Please enter a number between 1 and 65535."
        exit 1
    fi

    echo "Generating UUID..."
    UUID=$(cat /proc/sys/kernel/random/uuid)

    if ! id -u hysteria &> /dev/null; then
        useradd -r -s /usr/sbin/nologin hysteria
    fi

    networkdef=$(ip route | grep "^default" | awk '{print $5}')

    issue_hysteria_cert "$domain" "$tls_method" "$cred1" "$cred2"

    local cert_dir="$HYSTERIA_SSL_DIR/$domain"

    echo "Customizing config.json..."
    jq --arg port "$port" \
       --arg UUID "$UUID" \
       --arg networkdef "$networkdef" \
       --arg cert "$cert_dir/fullchain.pem" \
       --arg key "$cert_dir/key.pem" \
       '.listen = ":\($port)" |
        .tls.cert = $cert |
        .tls.key = $key |
        del(.tls.pinSHA256) |
        del(.tls.insecure) |
        del(.obfs) |
        .trafficStats.secret = $UUID |
        .outbounds[0].direct.bindDevice = $networkdef' "$CONFIG_FILE" > "${CONFIG_FILE}.temp" && mv "${CONFIG_FILE}.temp" "$CONFIG_FILE"

    echo "Updating hysteria-server.service to use Blitz Panel config.json..."
    sed -i 's|(config.yaml)|(Blitz Panel)|' /etc/systemd/system/hysteria-server.service
    sed -i "s|/etc/hysteria/config.yaml|$CONFIG_FILE|" /etc/systemd/system/hysteria-server.service
    rm -f /etc/hysteria/config.yaml
    sleep 1

    echo "Starting and enabling Hysteria service..."
    systemctl daemon-reload >/dev/null 2>&1
    systemctl start hysteria-server.service >/dev/null 2>&1
    systemctl enable hysteria-server.service >/dev/null 2>&1
    systemctl restart hysteria-server.service >/dev/null 2>&1

    if systemctl is-active --quiet hysteria-server.service; then
        echo -e "${cyan}Hysteria2${NC} has been successfully installed."
    else
        echo -e "${red}Error:${NC} hysteria-server.service is not active."
        exit 1
    fi

    chmod +x /etc/hysteria/core/scripts/hysteria2/kick.py

    if ! check_auth_server_service; then
        echo "Setting up Hysteria auth server..."
        setup_hysteria_auth_server
    fi

    if systemctl is-active --quiet hysteria-auth.service; then
        echo -e "${cyan}Hysteria auth server${NC} has been successfully started."
    else
        echo -e "${red}Error:${NC} hysteria-auth.service is not active."
        exit 1
    fi

    if ! check_scheduler_service; then
        setup_hysteria_scheduler
    fi

    echo "Setting up connection limiter (MAX_CONNECTIONS=2)..."
    if ! grep -q "^MAX_CONNECTIONS=" "$CONFIG_ENV" 2>/dev/null; then
        echo "MAX_CONNECTIONS=2" >> "$CONFIG_ENV"
    fi
    $VENV_PYTHON "$CONN_LIMIT_SCRIPT" start
    if systemctl is-active --quiet hysteria-conn-limit.service; then
        echo -e "${cyan}Connection limiter${NC} started (MAX_CONNECTIONS=2)."
    else
        echo -e "${yellow}Warning:${NC} Connection limiter failed to start. You can enable it later from the menu."
    fi
}

if systemctl is-active --quiet hysteria-server.service; then
    echo -e "${red}Error:${NC} Hysteria2 is already installed and running."
    echo
    echo "If you need to update the core, please use the 'Update Core' option."
else
    echo "Installing and configuring Hysteria2..."
    install_hysteria "$1" "$2" "$3" "$4" "$5"
    echo -e "\n"

    if systemctl is-active --quiet hysteria-server.service; then
        echo "Installation and configuration complete."
        python3 $CLI_PATH add-user --username default --traffic-limit 30 --expiration-days 30
    else
        echo -e "${red}Error:${NC} Hysteria2 service is not active. Please check the logs for more details."
    fi
fi
