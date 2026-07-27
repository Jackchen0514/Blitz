#!/bin/bash

set -euo pipefail
trap 'echo -e "\n❌ An error occurred. Aborting."; exit 1' ERR

# ========== Variables ==========
HYSTERIA_INSTALL_DIR="/etc/hysteria"
HYSTERIA_VENV_DIR="$HYSTERIA_INSTALL_DIR/hysteria2_venv"
MIGRATE_SCRIPT_PATH="$HYSTERIA_INSTALL_DIR/core/scripts/db/migrate_users.py"

# ========== Color Setup ==========
GREEN=$(tput setaf 2)
RED=$(tput setaf 1)
YELLOW=$(tput setaf 3)
BLUE=$(tput setaf 4)
RESET=$(tput sgr0)

info() { echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] - ${RESET} $1"; }
success() { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] [OK] - ${RESET} $1"; }
warn() { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] - ${RESET} $1"; }
error() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] - ${RESET} $1"; }

# ========== Check AVX Support ==========
check_avx_support() {
    info "Checking CPU for AVX support (required for MongoDB)..."
    if grep -q -m1 -o -E 'avx|avx2|avx512' /proc/cpuinfo; then
        success "CPU supports AVX instruction set."
    else
        error "CPU does not support the required AVX instruction set for MongoDB."
        info "Your system is not compatible with this version."
        info "Please use the 'nodb' upgrade script instead:"
        echo -e "${YELLOW}bash <(curl -sL https://raw.githubusercontent.com/Jackchen0514/Blitz/nodb/upgrade.sh)${RESET}"
        error "Upgrade aborted."
        exit 1
    fi
}

# ========== Fix Caddy Repository ==========
fix_caddy_repo() {
    info "Checking Caddy repository configuration..."
    local caddy_source_list="/etc/apt/sources.list.d/caddy-stable.list"
    local new_caddy_keyring="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
    local old_caddy_key="/etc/apt/trusted.gpg.d/caddy.asc"

    if [[ -f "$old_caddy_key" ]] || { [[ -f "$caddy_source_list" ]] && grep -q "caddy.asc" "$caddy_source_list"; }; then
        warn "Outdated Caddy repository configuration detected. Fixing it..."
        
        if [[ -f "$old_caddy_key" ]]; then
            rm -f "$old_caddy_key"
            info "Removed old Caddy GPG key."
        fi
        
        rm -f "$new_caddy_keyring"
        info "Downloading new Caddy GPG key..."
        if ! curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o "$new_caddy_keyring"; then
            error "Failed to download or process the Caddy GPG key."
            exit 1
        fi
        
        info "Updating Caddy sources list..."
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee "$caddy_source_list" > /dev/null
        
        chmod o+r "$new_caddy_keyring"
        chmod o+r "$caddy_source_list"
        
        info "Running apt update to apply repository changes..."
        apt-get update -qq
        success "Caddy repository configuration has been updated."
    else
        success "Caddy repository configuration is up-to-date."
    fi
}

# ========== Install MongoDB ==========
install_mongodb() {
    info "Checking for MongoDB..."
    if ! command -v mongod &>/dev/null; then
        warn "MongoDB not found. Installing from official repository..."
        
        local os_name os_version
        os_name=$(grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"')
        os_version=$(grep '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '"')
        
        apt-get update 
        apt-get install -y gnupg curl lsb-release
        
        curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor
        
        if [[ "$os_name" == "ubuntu" ]]; then
            if [[ "$os_version" == "24.04" ]]; then
                echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" > /etc/apt/sources.list.d/mongodb-org-8.0.list
            elif [[ "$os_version" == "22.04" ]]; then
                echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/8.0 multiverse" > /etc/apt/sources.list.d/mongodb-org-8.0.list
            else
                echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/8.0 multiverse" > /etc/apt/sources.list.d/mongodb-org-8.0.list
            fi
        elif [[ "$os_name" == "debian" ]]; then
            # Debian 12 (bookworm) and 13 (trixie) both use the bookworm MongoDB repo
            echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" > /etc/apt/sources.list.d/mongodb-org-8.0.list
        else
            error "Unsupported OS for MongoDB installation: $os_name $os_version"
            exit 1
        fi
        
        apt-get update -qq
        apt-get install -y mongodb-org
        systemctl start mongod
        systemctl enable mongod
        success "MongoDB installed and started successfully."
    else
        success "MongoDB is already installed."
    fi
}

migrate_normalsub_path() {
    local normalsub_env_file="$HYSTERIA_INSTALL_DIR/core/scripts/normalsub/.env"
    info "Checking for NormalSub configuration migration..."

    if systemctl is-active --quiet "hysteria-normal-sub.service" && [[ -f "$normalsub_env_file" ]]; then
        info "Active NormalSub service detected with .env file. Checking subpath format..."
        
        (
            source "$normalsub_env_file"
            
            if [[ -n "${SUBPATH:-}" ]] && ! [[ "$SUBPATH" == *"sub/normal"* ]]; then
                warn "Old NormalSub subpath format detected. Migrating to maintain URL compatibility..."
                local new_subpath="${SUBPATH}/sub/normal"
                
                sed -i "s|SUBPATH=.*|SUBPATH=${new_subpath}|" "$normalsub_env_file"
                
                success "SUBPATH in $normalsub_env_file updated to: $new_subpath"
            else
                success "NormalSub subpath format is already up-to-date or migration is not needed."
            fi
        )
    else
        info "NormalSub service not active or .env file not found. Skipping migration."
    fi
}

# ========== New Function to Migrate Data ==========
migrate_caddy_services() {
    local drop_in_dir="/etc/caddy/conf.d"
    local master_cf="/etc/caddy/Caddyfile"
    local old_wp_cf="$HYSTERIA_INSTALL_DIR/core/scripts/webpanel/Caddyfile"
    local old_ns_cf="$HYSTERIA_INSTALL_DIR/core/scripts/normalsub/Caddyfile.normalsub"
    local new_wp_cf="$drop_in_dir/webpanel.conf"
    local new_ns_cf="$drop_in_dir/normalsub.conf"

    info "Setting up unified caddy.service configuration..."
    mkdir -p "$drop_in_dir"

    cat > "$master_cf" << 'EOF'
{
    admin off
    auto_https disable_redirects
}

import /etc/caddy/conf.d/*.conf
EOF

    # Migrate webpanel config: strip global block from old Caddyfile if new one not yet present
    if [[ -f "$old_wp_cf" && ! -f "$new_wp_cf" ]]; then
        sed '/^# Global configuration/d; /^{$/,/^}$/d' "$old_wp_cf" > "$new_wp_cf"
        info "Migrated webpanel Caddy config → $new_wp_cf"
    fi

    # Migrate normalsub config: strip global block from old Caddyfile.normalsub if new one not yet present
    if [[ -f "$old_ns_cf" && ! -f "$new_ns_cf" ]]; then
        sed '/^# Global configuration/d; /^{$/,/^}$/d' "$old_ns_cf" > "$new_ns_cf"
        info "Migrated normalsub Caddy config → $new_ns_cf"
    fi

    # Write caddy.service override (takes precedence over any package-installed unit)
    cat > /etc/systemd/system/caddy.service << 'EOF'
[Unit]
Description=Caddy
After=network.target

[Service]
WorkingDirectory=/etc/caddy
ExecStart=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

    # Stop and remove old separate service files
    for old_svc in hysteria-caddy.service hysteria-caddy-normalsub.service; do
        systemctl stop "$old_svc" 2>/dev/null || true
        systemctl disable "$old_svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/$old_svc"
    done

    systemctl daemon-reload
    success "Caddy services merged into caddy.service."
}

migrate_json_to_mongo() {
    info "Checking for user data migration..."
    if [[ -f "$HYSTERIA_INSTALL_DIR/users.json" ]]; then
        info "Found users.json. Proceeding with migration to MongoDB."
        if python3 "$MIGRATE_SCRIPT_PATH"; then
            success "Data migration completed successfully."
        else
            error "Data migration script failed. Please check the output above."
            exit 1
        fi
    else
        info "No users.json found. Skipping migration."
    fi
}

download_and_extract_latest_release() {
    local arch
    case $(uname -m) in
        x86_64) arch="amd64" ;;
        aarch64) arch="arm64" ;;
        *)
            error "Unsupported architecture: $(uname -m)"
            exit 1
            ;;
    esac
    info "Detected architecture: $arch"

    local zip_name="Blitz-${arch}.zip"
    local download_url="https://github.com/Jackchen0514/Blitz/releases/latest/download/${zip_name}"
    local temp_zip="/tmp/${zip_name}"

    info "Downloading latest release from ${download_url}..."
    if ! curl -sL -o "$temp_zip" "$download_url"; then
        error "Failed to download the release asset. Please check the URL and your connection."
        exit 1
    fi
    success "Download complete."

    info "Removing old installation directory..."
    rm -rf "$HYSTERIA_INSTALL_DIR"
    mkdir -p "$HYSTERIA_INSTALL_DIR"

    info "Extracting to ${HYSTERIA_INSTALL_DIR}..."
    if ! unzip -q "$temp_zip" -d "$HYSTERIA_INSTALL_DIR"; then
        error "Failed to extract the archive."
        exit 1
    fi
    success "Extracted successfully."

    rm "$temp_zip"
    info "Cleaned up temporary file."
}

# ========== Capture Active Services ==========
declare -a ACTIVE_SERVICES_BEFORE_UPGRADE=()
ALL_SERVICES=(
    caddy.service
    hysteria-caddy.service           # legacy pre-merge
    hysteria-caddy-normalsub.service # legacy pre-merge
    hysteria-conn-limit.service
    hysteria-server.service
    hysteria-auth.service
    hysteria-scheduler.service
    hysteria-telegram-bot.service
    hysteria-normal-sub.service
    hysteria-webpanel.service
    hysteria-ip-limit.service
)

info "Checking for active services before upgrade..."
for SERVICE in "${ALL_SERVICES[@]}"; do
    if systemctl is-active --quiet "$SERVICE"; then
        ACTIVE_SERVICES_BEFORE_UPGRADE+=("$SERVICE")
        info "Service '$SERVICE' is active and will be restarted."
    fi
done

# ========== Check AVX Support Prerequisite ==========
check_avx_support

# ========== Fix Caddy Repo Prerequisite ==========
fix_caddy_repo

# ========== Install MongoDB Prerequisite ==========
install_mongodb

# ========== Migrate NormalSub Path (if necessary) ==========
# migrate_normalsub_path

# ========== Backup Files ==========
cd /root
TEMP_DIR=$(mktemp -d)
FILES=(
    "$HYSTERIA_INSTALL_DIR/ca.key"
    "$HYSTERIA_INSTALL_DIR/ca.crt"
    "$HYSTERIA_INSTALL_DIR/users.json"
    "$HYSTERIA_INSTALL_DIR/config.json"
    "$HYSTERIA_INSTALL_DIR/.configs.env"
    "$HYSTERIA_INSTALL_DIR/nodes.json"
    "$HYSTERIA_INSTALL_DIR/extra.json"
    "$HYSTERIA_INSTALL_DIR/geosite.dat"
    "$HYSTERIA_INSTALL_DIR/geoip.dat"
    "$HYSTERIA_INSTALL_DIR/core/scripts/telegrambot/.env"
    "$HYSTERIA_INSTALL_DIR/core/scripts/normalsub/.env"
    "$HYSTERIA_INSTALL_DIR/core/scripts/normalsub/Caddyfile.normalsub"
    "$HYSTERIA_INSTALL_DIR/core/scripts/webpanel/.env"
    "$HYSTERIA_INSTALL_DIR/core/scripts/webpanel/Caddyfile"
    "/etc/caddy/conf.d/webpanel.conf"
    "/etc/caddy/conf.d/normalsub.conf"
    "/etc/caddy/Caddyfile"
)

info "Backing up configuration and data files to: $TEMP_DIR"
for FILE in "${FILES[@]}"; do
    if [[ -f "$FILE" ]]; then
        mkdir -p "$TEMP_DIR/$(dirname "$FILE")"
        cp -p "$FILE" "$TEMP_DIR/$FILE"
        success "Backed up: $FILE"
    else
        warn "File not found, skipping backup: $FILE"
    fi
done

# Back up SSL cert directory (Let's Encrypt certs issued for Hysteria)
if [[ -d "$HYSTERIA_INSTALL_DIR/ssl" ]]; then
    cp -rp "$HYSTERIA_INSTALL_DIR/ssl" "$TEMP_DIR$HYSTERIA_INSTALL_DIR/ssl"
    success "Backed up: $HYSTERIA_INSTALL_DIR/ssl/"
fi

# ========== Download and Replace Installation ==========
download_and_extract_latest_release

# ========== Restore Backup ==========
info "Restoring configuration and data files..."
for FILE in "${FILES[@]}"; do
    BACKUP="$TEMP_DIR/$FILE"
    if [[ -f "$BACKUP" ]]; then
        cp -p "$BACKUP" "$FILE"
        success "Restored: $FILE"
    else
        warn "Missing backup file, skipping restore: $BACKUP"
    fi
done

# Restore SSL cert directory
if [[ -d "$TEMP_DIR$HYSTERIA_INSTALL_DIR/ssl" ]]; then
    cp -rp "$TEMP_DIR$HYSTERIA_INSTALL_DIR/ssl" "$HYSTERIA_INSTALL_DIR/ssl"
    # Re-apply hysteria user ownership on cert files
    if id -u hysteria >/dev/null 2>&1; then
        chown -R hysteria:hysteria "$HYSTERIA_INSTALL_DIR/ssl" 2>/dev/null || true
        find "$HYSTERIA_INSTALL_DIR/ssl" -name "*.pem" -exec chmod 640 {} \;
    fi
    success "Restored: $HYSTERIA_INSTALL_DIR/ssl/"
fi

# ========== Update Configuration ==========
info "Updating Hysteria configuration for HTTP authentication..."
# If the connection limiter was active before upgrade, preserve its proxy port (28263).
# Otherwise point directly at the Go auth server (28262).
if [[ " ${ACTIVE_SERVICES_BEFORE_UPGRADE[*]} " =~ "hysteria-conn-limit.service" ]]; then
    auth_url="http://127.0.0.1:28263/auth"
    info "Connection limiter was active — keeping auth URL at port 28263."
else
    auth_url="http://127.0.0.1:28262/auth"
fi
auth_block="{\"type\": \"http\", \"http\": {\"url\": \"${auth_url}\"}}"
if [[ -f "$HYSTERIA_INSTALL_DIR/config.json" ]]; then
    jq --argjson auth_block "$auth_block" '.auth = $auth_block' "$HYSTERIA_INSTALL_DIR/config.json" > "$HYSTERIA_INSTALL_DIR/config.json.tmp" && mv "$HYSTERIA_INSTALL_DIR/config.json.tmp" "$HYSTERIA_INSTALL_DIR/config.json"
    success "config.json updated (auth URL: ${auth_url})."
else
    warn "config.json not found after restore. Skipping auth update."
fi

# ========== Permissions ==========
info "Setting ownership and permissions..."
if id -u hysteria >/dev/null 2>&1; then
    chown hysteria:hysteria "$HYSTERIA_INSTALL_DIR/ca.key" "$HYSTERIA_INSTALL_DIR/ca.crt" 2>/dev/null || true
    chmod 640 "$HYSTERIA_INSTALL_DIR/ca.key" "$HYSTERIA_INSTALL_DIR/ca.crt" 2>/dev/null || true
    chown -R hysteria:hysteria "$HYSTERIA_INSTALL_DIR/core/scripts/telegrambot" 2>/dev/null || true
fi
chmod +x "$HYSTERIA_INSTALL_DIR/core/scripts/hysteria2/kick.py"
chmod +x "$HYSTERIA_INSTALL_DIR/core/scripts/auth/user_auth"
success "Permissions updated."

# ========== Virtual Environment ==========
info "Setting up virtual environment and installing dependencies..."
cd "$HYSTERIA_INSTALL_DIR"
python3 -m venv "$HYSTERIA_VENV_DIR"
source "$HYSTERIA_VENV_DIR/bin/activate"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt >/dev/null
success "Python environment ready."

# ========== Default MAX_CONNECTIONS ==========
if [[ -f "$HYSTERIA_INSTALL_DIR/.configs.env" ]] && ! grep -q "^MAX_CONNECTIONS=" "$HYSTERIA_INSTALL_DIR/.configs.env"; then
    echo "MAX_CONNECTIONS=2" >> "$HYSTERIA_INSTALL_DIR/.configs.env"
    info "Added default MAX_CONNECTIONS=2 to .configs.env"
fi

# ========== Hysteria Server Log File + Rotation ==========
info "Ensuring hysteria-server log file and logrotate config..."
mkdir -p /etc/systemd/system/hysteria-server.service.d
if [[ ! -f /etc/systemd/system/hysteria-server.service.d/log.conf ]]; then
    cat > /etc/systemd/system/hysteria-server.service.d/log.conf << 'EOF'
[Service]
StandardOutput=append:/var/log/hysteria-server.log
StandardError=append:/var/log/hysteria-server.log
EOF
    success "Created hysteria-server log.conf drop-in."
fi
cat > /etc/logrotate.d/hysteria-server << 'EOF'
/var/log/hysteria-server.log {
    daily
    rotate 7
    size 50M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
success "logrotate config for hysteria-server.log updated."

# ========== Caddy Services Migration ==========
migrate_caddy_services

# ========== Data Migration ==========
migrate_json_to_mongo

# ========== Systemd Services ==========
info "Ensuring systemd services are configured..."
if source "$HYSTERIA_INSTALL_DIR/core/scripts/scheduler.sh"; then
    if ! check_auth_server_service; then
        setup_hysteria_auth_server && success "Auth server service configured." || warn "Auth server setup failed."
    else
        success "Auth server service already configured."
    fi

    if ! check_scheduler_service; then
        setup_hysteria_scheduler && success "Scheduler service configured." || warn "Scheduler setup failed."
    else
        success "Scheduler service already set."
    fi
else
    warn "Failed to source scheduler.sh, continuing without service setup..."
fi

# ========== Restart Services ==========
info "Reloading systemd daemon..."
systemctl daemon-reload

info "Restarting services that were active before the upgrade..."
caddy_needed=false
if [ ${#ACTIVE_SERVICES_BEFORE_UPGRADE[@]} -eq 0 ]; then
    warn "No relevant services were active before the upgrade. Skipping restart."
else
    for SERVICE in "${ACTIVE_SERVICES_BEFORE_UPGRADE[@]}"; do
        # Old caddy service names are replaced by caddy.service after migration
        if [[ "$SERVICE" == "hysteria-caddy.service" || "$SERVICE" == "hysteria-caddy-normalsub.service" ]]; then
            caddy_needed=true
            info "Service '$SERVICE' merged into caddy.service — will start after restart loop."
            continue
        fi
        info "Attempting to restart $SERVICE..."
        systemctl enable "$SERVICE" &>/dev/null || warn "Could not enable $SERVICE. It might not exist."
        systemctl restart "$SERVICE"
        sleep 2
        if systemctl is-active --quiet "$SERVICE"; then
            success "$SERVICE restarted successfully and is active."
        else
            warn "$SERVICE failed to restart or is not active."
            warn "Showing last 5 log entries for $SERVICE:"
            journalctl -u "$SERVICE" -n 5 --no-pager
        fi
    done
fi

# Start caddy.service if either old caddy service was active, or if it was already active
if [[ "$caddy_needed" == true ]] || [[ " ${ACTIVE_SERVICES_BEFORE_UPGRADE[*]} " =~ " caddy.service " ]]; then
    if compgen -G "/etc/caddy/conf.d/*.conf" > /dev/null 2>&1; then
        info "Starting caddy.service (unified Caddy)..."
        systemctl enable caddy.service &>/dev/null || true
        systemctl restart caddy.service
        sleep 2
        if systemctl is-active --quiet caddy.service; then
            success "caddy.service started successfully."
        else
            warn "caddy.service failed to start."
            journalctl -u caddy.service -n 5 --no-pager
        fi
    else
        warn "No caddy drop-in configs found in /etc/caddy/conf.d/ — skipping caddy.service start."
    fi
fi

# ========== Final Check ==========
if systemctl is-active --quiet hysteria-server.service; then
    success "🎉 Upgrade completed successfully!"
else
    warn "⚠️ hysteria-server.service is not active. Check logs if needed."
fi

# ========== Launch Menu ==========
info "Upgrade process finished. Launching menu..."
cd "$HYSTERIA_INSTALL_DIR"
chmod +x menu.sh
./menu.sh