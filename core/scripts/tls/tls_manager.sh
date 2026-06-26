#!/bin/bash
source /etc/hysteria/core/scripts/utils.sh
define_colors

CERT_BASE_DIR="/etc/caddy/ssl"
ACME_SH="$HOME/.acme.sh/acme.sh"
WEBPANEL_SHELL="/etc/hysteria/core/scripts/webpanel/webpanel_shell.sh"
NORMALSUB_SHELL="/etc/hysteria/core/scripts/normalsub/normalsub.sh"

# ── acme.sh installation ──────────────────────────────────────────────────────

_install_acmesh() {
    if [[ -f "$ACME_SH" ]]; then
        return 0
    fi
    echo -e "${yellow}Installing acme.sh...${NC}"
    if ! curl -sSL https://get.acme.sh | sh -s email="acme@$(hostname -f 2>/dev/null || echo localhost)"; then
        echo -e "${red}Failed to install acme.sh.${NC}"
        return 1
    fi
    echo -e "${green}acme.sh installed.${NC}"
}

# ── cert install + Caddy reload hook ─────────────────────────────────────────

_deploy_cert() {
    local domain=$1
    local cert_dir="$CERT_BASE_DIR/$domain"
    mkdir -p "$cert_dir"

    "$ACME_SH" --install-cert -d "$domain" \
        --fullchain-file "$cert_dir/fullchain.pem" \
        --key-file       "$cert_dir/key.pem" \
        --reloadcmd      "bash $WEBPANEL_SHELL updatecaddy 2>/dev/null; bash $NORMALSUB_SHELL updatecaddy 2>/dev/null; systemctl reload caddy.service 2>/dev/null || true"
    echo -e "${green}Certificate deployed to $cert_dir${NC}"
}

# After cert changes regenerate any Caddy drop-in that uses this domain
_refresh_caddy() {
    local domain=$1

    local wp_env="/etc/hysteria/core/scripts/webpanel/.env"
    if [[ -f "$wp_env" ]]; then
        local wp_domain
        wp_domain=$(grep '^DOMAIN=' "$wp_env" | cut -d= -f2-)
        if [[ "$wp_domain" == "$domain" ]]; then
            bash "$WEBPANEL_SHELL" updatecaddy 2>/dev/null
        fi
    fi

    local ns_env="/etc/hysteria/core/scripts/normalsub/.env"
    if [[ -f "$ns_env" ]]; then
        local ns_domain
        ns_domain=$(grep '^HYSTERIA_DOMAIN=' "$ns_env" | cut -d= -f2-)
        if [[ "$ns_domain" == "$domain" ]]; then
            bash "$NORMALSUB_SHELL" updatecaddy 2>/dev/null
        fi
    fi

    if systemctl is-active --quiet caddy.service; then
        systemctl reload caddy.service 2>/dev/null || systemctl restart caddy.service
        echo -e "${green}Caddy reloaded.${NC}"
    fi
}

# ── HTTP-01 ──────────────────────────────────────────────────────────────────

cmd_http01() {
    local domain=$1
    if [[ -z "$domain" ]]; then
        echo -e "${red}Usage: tls_manager.sh issue http01 <domain>${NC}"; exit 1
    fi

    echo -e "${yellow}Switching $domain to HTTP-01 (Caddy automatic HTTPS)...${NC}"
    # Remove any explicit cert so Caddy falls back to auto-HTTPS
    if [[ -d "$CERT_BASE_DIR/$domain" ]]; then
        rm -rf "${CERT_BASE_DIR:?}/$domain"
        echo -e "${green}Explicit certificate removed. Caddy will use HTTP-01 automatically.${NC}"
    else
        echo -e "${green}No explicit certificate found. Caddy already uses HTTP-01.${NC}"
    fi
    _refresh_caddy "$domain"
}

# ── DNS-01 via Cloudflare ─────────────────────────────────────────────────────

cmd_dns01_cf() {
    local domain=$1
    local cf_token=$2
    if [[ -z "$domain" || -z "$cf_token" ]]; then
        echo -e "${red}Usage: tls_manager.sh issue dns01 cloudflare <domain> <CF_Token>${NC}"; exit 1
    fi

    _install_acmesh || exit 1

    echo -e "${yellow}Issuing certificate for ${domain} via DNS-01 (Cloudflare)...${NC}"
    export CF_Token="$cf_token"

    if ! "$ACME_SH" --issue --dns dns_cf -d "$domain" --server letsencrypt; then
        echo -e "${red}Certificate issuance failed.${NC}"
        echo -e "${yellow}Verify that the API token has Zone:DNS:Edit permission for the domain.${NC}"
        exit 1
    fi

    _deploy_cert "$domain"
    _refresh_caddy "$domain"
    echo -e "${green}Done. Certificate for $domain is active.${NC}"
}

# ── DNS-01 via ClouDNS ────────────────────────────────────────────────────────

cmd_dns01_cloudns() {
    local domain=$1
    local auth_id=$2
    local auth_password=$3
    if [[ -z "$domain" || -z "$auth_id" || -z "$auth_password" ]]; then
        echo -e "${red}Usage: tls_manager.sh issue dns01 cloudns <domain> <CLOUDNS_AUTH_ID> <CLOUDNS_AUTH_PASSWORD>${NC}"; exit 1
    fi

    _install_acmesh || exit 1

    echo -e "${yellow}Issuing certificate for ${domain} via DNS-01 (ClouDNS)...${NC}"
    export CLOUDNS_AUTH_ID="$auth_id"
    export CLOUDNS_AUTH_PASSWORD="$auth_password"

    if ! "$ACME_SH" --issue --dns dns_cloudns -d "$domain" --server letsencrypt; then
        echo -e "${red}Certificate issuance failed.${NC}"
        echo -e "${yellow}Verify CLOUDNS_AUTH_ID and CLOUDNS_AUTH_PASSWORD are correct.${NC}"
        exit 1
    fi

    _deploy_cert "$domain"
    _refresh_caddy "$domain"
    echo -e "${green}Done. Certificate for $domain is active.${NC}"
}

# ── status ────────────────────────────────────────────────────────────────────

cmd_status() {
    local domain=${1:-""}
    if [[ -n "$domain" ]]; then
        local cert="$CERT_BASE_DIR/$domain/fullchain.pem"
        if [[ -f "$cert" ]]; then
            echo -e "${green}Explicit certificate for $domain (DNS-01):${NC}"
            openssl x509 -in "$cert" -noout -subject -issuer -dates 2>/dev/null
        else
            echo -e "${cyan}$domain: Caddy automatic HTTPS (HTTP-01, no explicit cert)${NC}"
        fi
        return
    fi

    local found=false
    if compgen -G "$CERT_BASE_DIR/*/fullchain.pem" > /dev/null 2>&1; then
        echo -e "${green}Explicitly managed certificates (DNS-01):${NC}"
        for cert in "$CERT_BASE_DIR"/*/fullchain.pem; do
            local d; d=$(basename "$(dirname "$cert")")
            echo -e "  ${cyan}$d${NC}"
            openssl x509 -in "$cert" -noout -dates 2>/dev/null | sed 's/^/    /'
            found=true
        done
    fi
    if ! $found; then
        echo -e "${yellow}No explicitly managed certificates found.${NC}"
        echo -e "  All domains use Caddy automatic HTTPS (HTTP-01)."
    fi
}

# ── revoke ────────────────────────────────────────────────────────────────────

cmd_revoke() {
    local domain=$1
    if [[ -z "$domain" ]]; then
        echo -e "${red}Usage: tls_manager.sh revoke <domain>${NC}"; exit 1
    fi

    if [[ -f "$ACME_SH" ]]; then
        "$ACME_SH" --revoke -d "$domain" --server letsencrypt 2>/dev/null || true
        "$ACME_SH" --remove -d "$domain" 2>/dev/null || true
    fi

    rm -rf "${CERT_BASE_DIR:?}/$domain"
    echo -e "${green}Certificate for $domain revoked and removed. Caddy will use HTTP-01.${NC}"
    _refresh_caddy "$domain"
}

# ── dispatch ──────────────────────────────────────────────────────────────────

case "$1" in
    issue)
        shift
        case "$1" in
            http01)   cmd_http01 "$2" ;;
            dns01)
                shift
                case "$1" in
                    cloudflare) cmd_dns01_cf     "$2" "$3" ;;
                    cloudns)    cmd_dns01_cloudns "$2" "$3" "$4" ;;
                    *) echo -e "${red}Unknown DNS provider: $1 (cloudflare|cloudns)${NC}"; exit 1 ;;
                esac
                ;;
            *) echo -e "${red}Usage: $0 issue {http01 <domain> | dns01 cloudflare <domain> <token> | dns01 cloudns <domain> <id> <pass>}${NC}"; exit 1 ;;
        esac
        ;;
    status) cmd_status "$2" ;;
    revoke) cmd_revoke "$2" ;;
    *)
        echo -e "${red}Usage: $0 {issue|status|revoke}${NC}"
        exit 1
        ;;
esac
