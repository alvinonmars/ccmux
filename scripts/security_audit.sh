#!/usr/bin/env bash
# security_audit.sh — Data collection script for hybrid security audit
# Must be run with sudo. Outputs structured JSON to stdout.
# Usage: sudo bash security_audit.sh [audit|forensic]

set -uo pipefail

# Ensure sbin dirs are in PATH (needed for sysctl, iptables, etc. when not root)
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODE="${1:-audit}"
export MODE
if [[ "$MODE" != "audit" && "$MODE" != "forensic" ]]; then
    echo '{"error": "Invalid mode. Use: audit or forensic"}' >&2
    exit 1
fi

TMPDIR="$(mktemp -d /tmp/security_audit.XXXXXX)"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT

# Check root — warn but continue with degraded results
IS_ROOT=true
if [[ $EUID -ne 0 ]]; then
    echo '[!] WARNING: Not running as root. Some checks will be skipped or degraded.' >&2
    IS_ROOT=false
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Run a command, capture output, return status. Writes raw output to TMPDIR.
run_check() {
    local label="$1"
    shift
    local outfile="$TMPDIR/${label}.txt"
    if "$@" > "$outfile" 2>&1; then
        cat "$outfile"
        return 0
    else
        cat "$outfile"
        return 0  # Don't abort on check failure
    fi
}

# Check if a tool is available
tool_available() {
    command -v "$1" &>/dev/null
}

# JSON-escape a string (handle backslashes, quotes, newlines, tabs)
json_escape() {
    python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null
}

# Wrap a check section: captures output, emits JSON fragment
# Usage: section_json "key" "description" <command producing text>
# We'll build JSON via python3 at the end for correctness.

# We accumulate results in an associative array-like temp file structure
RESULTS_DIR="$TMPDIR/results"
mkdir -p "$RESULTS_DIR"

save_result() {
    local category="$1"
    local key="$2"
    local description="$3"
    local status="$4"  # ok, warning, error, info, skipped
    local data="$5"    # raw text data

    local dir="$RESULTS_DIR/$category"
    mkdir -p "$dir"

    # Write metadata
    cat > "$dir/${key}.json" <<ENDJSON
{
    "key": "$key",
    "description": "$description",
    "status": "$status",
    "data": $(echo "$data" | json_escape)
}
ENDJSON
}

# ---------------------------------------------------------------------------
# AUDIT MODE CHECKS
# ---------------------------------------------------------------------------

echo "Running security audit in $MODE mode..." >&2

# === System Info ===
echo "[*] Collecting system info..." >&2
{
    SYSINFO=""
    SYSINFO+="Hostname: $(hostname)\n"
    SYSINFO+="Kernel: $(uname -r)\n"
    SYSINFO+="OS: $(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '"')\n"
    SYSINFO+="Uptime: $(uptime -p 2>/dev/null || uptime)\n"
    SYSINFO+="Last reboot: $(who -b 2>/dev/null | awk '{print $3, $4}')\n"
    SYSINFO+="Date: $(date -u '+%Y-%m-%d %H:%M:%S UTC')\n"
    SYSINFO+="Architecture: $(uname -m)\n"
    save_result "system" "info" "Basic system information" "info" "$SYSINFO"
}

# === User Accounts ===
echo "[*] Checking user accounts..." >&2
{
    # All users with shells
    SHELL_USERS=$(awk -F: '$7 !~ /(nologin|false)/ {print $1 ":" $3 ":" $6 ":" $7}' /etc/passwd)
    save_result "users" "shell_users" "Users with login shells" "info" "$SHELL_USERS"

    # UID 0 accounts
    UID0=$(awk -F: '$3 == 0 {print $1}' /etc/passwd)
    UID0_COUNT=$(echo "$UID0" | grep -c . || true)
    if [[ "$UID0_COUNT" -gt 1 ]]; then
        save_result "users" "uid0" "Accounts with UID 0 (should only be root)" "warning" "$UID0"
    else
        save_result "users" "uid0" "Accounts with UID 0" "ok" "$UID0"
    fi

    # Empty passwords
    EMPTY_PW=$(awk -F: '($2 == "" || $2 == "!") {print $1}' /etc/shadow 2>/dev/null || echo "Cannot read /etc/shadow")
    if [[ -n "$EMPTY_PW" && "$EMPTY_PW" != "Cannot read /etc/shadow" ]]; then
        # Filter out system accounts with "!" (locked)
        TRULY_EMPTY=$(awk -F: '$2 == "" {print $1}' /etc/shadow 2>/dev/null)
        if [[ -n "$TRULY_EMPTY" ]]; then
            save_result "users" "empty_passwords" "Accounts with empty passwords" "warning" "$TRULY_EMPTY"
        else
            save_result "users" "empty_passwords" "Accounts with empty passwords" "ok" "None found"
        fi
    else
        save_result "users" "empty_passwords" "Accounts with empty passwords" "ok" "None found"
    fi

    # Recently changed passwords (last 30 days)
    RECENT_PW=""
    while IFS=: read -r user pw lastchg rest; do
        if [[ "$lastchg" =~ ^[0-9]+$ ]] && [[ "$lastchg" -gt 0 ]]; then
            days_since_epoch=$(( $(date +%s) / 86400 ))
            if (( days_since_epoch - lastchg < 30 )); then
                change_date=$(date -d "@$(( lastchg * 86400 ))" '+%Y-%m-%d' 2>/dev/null || echo "epoch:$lastchg")
                RECENT_PW+="$user (changed: $change_date)\n"
            fi
        fi
    done < /etc/shadow 2>/dev/null
    save_result "users" "recent_pw_changes" "Password changes in last 30 days" "info" "${RECENT_PW:-None}"

    # Locked/expired accounts
    LOCKED=""
    while IFS=: read -r user pw rest; do
        if [[ "$pw" == "!"* || "$pw" == "*" ]]; then
            LOCKED+="$user (locked)\n"
        fi
    done < /etc/shadow 2>/dev/null
    save_result "users" "locked_accounts" "Locked or expired accounts" "info" "${LOCKED:-None}"

    # Sudoers
    SUDOERS=$(getent group sudo 2>/dev/null | cut -d: -f4)
    save_result "users" "sudoers" "Users in sudo group" "info" "${SUDOERS:-None found}"
}

# === SSH Configuration ===
echo "[*] Checking SSH configuration..." >&2
{
    SSHD_CONFIG="/etc/ssh/sshd_config"
    if [[ -f "$SSHD_CONFIG" ]]; then
        # PermitRootLogin
        ROOT_LOGIN=$(grep -i "^PermitRootLogin" "$SSHD_CONFIG" 2>/dev/null | tail -1 || echo "Not set (default: prohibit-password)")
        if echo "$ROOT_LOGIN" | grep -qi "yes"; then
            save_result "ssh" "root_login" "SSH PermitRootLogin" "warning" "$ROOT_LOGIN"
        else
            save_result "ssh" "root_login" "SSH PermitRootLogin" "ok" "$ROOT_LOGIN"
        fi

        # PasswordAuthentication
        PW_AUTH=$(grep -i "^PasswordAuthentication" "$SSHD_CONFIG" 2>/dev/null | tail -1 || echo "Not set (default: yes)")
        if echo "$PW_AUTH" | grep -qi "yes" || echo "$PW_AUTH" | grep -qi "default: yes"; then
            save_result "ssh" "password_auth" "SSH PasswordAuthentication" "warning" "$PW_AUTH"
        else
            save_result "ssh" "password_auth" "SSH PasswordAuthentication" "ok" "$PW_AUTH"
        fi

        # Include additional config dirs
        INCLUDE_DIRS=$(grep -i "^Include" "$SSHD_CONFIG" 2>/dev/null || true)
        SSH_EXTRA=""
        if [[ -n "$INCLUDE_DIRS" ]]; then
            while read -r _ pattern; do
                for f in $pattern; do
                    if [[ -f "$f" ]]; then
                        SSH_EXTRA+="--- $f ---\n$(cat "$f")\n\n"
                    fi
                done
            done <<< "$INCLUDE_DIRS"
        fi
        if [[ -n "$SSH_EXTRA" ]]; then
            save_result "ssh" "extra_configs" "Additional SSH config files" "info" "$SSH_EXTRA"
        fi

        # Authorized keys audit
        AUTH_KEYS=""
        for homedir in /home/* /root; do
            if [[ -f "$homedir/.ssh/authorized_keys" ]]; then
                user=$(basename "$homedir")
                [[ "$homedir" == "/root" ]] && user="root"
                key_count=$(wc -l < "$homedir/.ssh/authorized_keys")
                AUTH_KEYS+="$user: $key_count key(s) in $homedir/.ssh/authorized_keys\n"
            fi
        done
        save_result "ssh" "authorized_keys" "Authorized keys audit" "info" "${AUTH_KEYS:-No authorized_keys files found}"

        # Full sshd_config (non-comment lines)
        SSHD_ACTIVE=$(grep -v '^#' "$SSHD_CONFIG" | grep -v '^$' || true)
        save_result "ssh" "config_active" "Active sshd_config directives" "info" "$SSHD_ACTIVE"
    else
        save_result "ssh" "config" "SSH configuration" "info" "sshd_config not found (SSH may not be installed)"
    fi
}

# === Firewall ===
echo "[*] Checking firewall configuration..." >&2
{
    # UFW
    if tool_available ufw; then
        UFW_STATUS=$(ufw status verbose 2>&1)
        if echo "$UFW_STATUS" | grep -qi "root\|permission denied\|not allowed"; then
            save_result "firewall" "ufw" "UFW firewall status" "skipped" "Cannot check UFW without root ($UFW_STATUS)"
        elif echo "$UFW_STATUS" | grep -q "inactive"; then
            save_result "firewall" "ufw" "UFW firewall status" "warning" "$UFW_STATUS"
        else
            save_result "firewall" "ufw" "UFW firewall status" "info" "$UFW_STATUS"
        fi
    else
        save_result "firewall" "ufw" "UFW firewall" "skipped" "ufw not installed"
    fi

    # iptables
    if tool_available iptables; then
        IPTABLES=$(iptables -L -n -v 2>&1)
        save_result "firewall" "iptables" "iptables rules" "info" "$IPTABLES"
    fi

    # nftables
    if tool_available nft; then
        NFTABLES=$(nft list ruleset 2>&1)
        save_result "firewall" "nftables" "nftables ruleset" "info" "$NFTABLES"
    fi

    # Open ports
    OPEN_PORTS=$(ss -tlnp 2>&1)
    save_result "firewall" "open_ports" "Listening TCP ports" "info" "$OPEN_PORTS"

    # Listening services (UDP too)
    UDP_PORTS=$(ss -ulnp 2>&1)
    save_result "firewall" "udp_ports" "Listening UDP ports" "info" "$UDP_PORTS"
}

# === File Permissions ===
echo "[*] Checking file permissions..." >&2
{
    # SUID files
    echo "[*]   Finding SUID files..." >&2
    SUID_FILES=$(find / -perm -4000 -type f 2>/dev/null | sort)
    SUID_COUNT=$(echo "$SUID_FILES" | grep -c . || true)
    save_result "permissions" "suid_files" "SUID files ($SUID_COUNT found)" "info" "$SUID_FILES"

    # SGID files
    echo "[*]   Finding SGID files..." >&2
    SGID_FILES=$(find / -perm -2000 -type f 2>/dev/null | sort)
    SGID_COUNT=$(echo "$SGID_FILES" | grep -c . || true)
    save_result "permissions" "sgid_files" "SGID files ($SGID_COUNT found)" "info" "$SGID_FILES"

    # World-writable files (excluding /proc, /sys, /dev, /run)
    echo "[*]   Finding world-writable files..." >&2
    WW_FILES=$(find / -xdev -perm -0002 -type f ! -path "/proc/*" ! -path "/sys/*" ! -path "/dev/*" ! -path "/run/*" 2>/dev/null | head -100)
    if [[ -n "$WW_FILES" ]]; then
        save_result "permissions" "world_writable" "World-writable files" "warning" "$WW_FILES"
    else
        save_result "permissions" "world_writable" "World-writable files" "ok" "None found"
    fi

    # Sensitive file permissions
    SENSITIVE=""
    for f in /etc/shadow /etc/passwd /etc/gshadow /etc/sudoers; do
        if [[ -e "$f" ]]; then
            SENSITIVE+="$(ls -la "$f")\n"
        fi
    done
    save_result "permissions" "sensitive_files" "Sensitive file permissions" "info" "$SENSITIVE"

    # /tmp permissions — check for sticky bit (t or T in the 10th char of permissions)
    TMP_PERMS=$(ls -ld /tmp)
    TMP_PERM_FIELD=$(echo "$TMP_PERMS" | awk '{print $1}')
    if echo "$TMP_PERM_FIELD" | grep -q '[tT]$'; then
        save_result "permissions" "tmp_sticky" "/tmp sticky bit" "ok" "$TMP_PERMS"
    else
        save_result "permissions" "tmp_sticky" "/tmp sticky bit" "warning" "$TMP_PERMS (missing sticky bit)"
    fi

    # SSH key permissions
    SSH_KEY_PERMS=""
    for homedir in /home/* /root; do
        if [[ -d "$homedir/.ssh" ]]; then
            user=$(basename "$homedir")
            [[ "$homedir" == "/root" ]] && user="root"
            SSH_KEY_PERMS+="--- $user ---\n"
            SSH_KEY_PERMS+="$(ls -la "$homedir/.ssh/" 2>/dev/null)\n\n"
        fi
    done
    save_result "permissions" "ssh_keys" "SSH key file permissions" "info" "${SSH_KEY_PERMS:-No .ssh directories found}"
}

# === Package Updates ===
echo "[*] Checking package updates..." >&2
{
    if tool_available apt; then
        # Refresh is slow; use cached data if recent
        APT_CACHE_AGE=0
        if [[ -f /var/cache/apt/pkgcache.bin ]]; then
            APT_CACHE_AGE=$(( $(date +%s) - $(stat -c %Y /var/cache/apt/pkgcache.bin) ))
        fi
        if (( APT_CACHE_AGE > 86400 )); then
            apt-get update -qq 2>&1 >/dev/null || true
        fi

        UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -v "^Listing" || true)
        UPGRADABLE_COUNT=$(echo "$UPGRADABLE" | grep -c . || true)
        if (( UPGRADABLE_COUNT > 0 )); then
            save_result "packages" "upgradable" "Upgradable packages ($UPGRADABLE_COUNT)" "warning" "$UPGRADABLE"
        else
            save_result "packages" "upgradable" "Upgradable packages" "ok" "All packages up to date"
        fi

        # Security updates specifically
        SEC_UPDATES=$(apt list --upgradable 2>/dev/null | grep -i security || true)
        SEC_COUNT=$(echo "$SEC_UPDATES" | grep -c . || true)
        if (( SEC_COUNT > 0 )); then
            save_result "packages" "security_updates" "Security updates available ($SEC_COUNT)" "warning" "$SEC_UPDATES"
        else
            save_result "packages" "security_updates" "Security updates" "ok" "No pending security updates"
        fi
    else
        save_result "packages" "upgradable" "Package updates" "skipped" "apt not available"
    fi
}

# === Kernel Security Parameters ===
echo "[*] Checking kernel security parameters..." >&2
{
    KERNEL_PARAMS=""
    declare -A EXPECTED=(
        ["kernel.randomize_va_space"]="2"
        ["net.ipv4.tcp_syncookies"]="1"
        ["net.ipv4.ip_forward"]="0"
        ["net.ipv4.conf.all.accept_redirects"]="0"
        ["net.ipv4.conf.all.send_redirects"]="0"
        ["net.ipv4.conf.all.accept_source_route"]="0"
        ["net.ipv6.conf.all.accept_redirects"]="0"
        ["fs.suid_dumpable"]="0"
        ["kernel.core_uses_pid"]="1"
        ["net.ipv4.conf.all.log_martians"]="1"
        ["kernel.dmesg_restrict"]="1"
        ["kernel.kptr_restrict"]="1"
    )

    KERNEL_STATUS="ok"
    for param in "${!EXPECTED[@]}"; do
        actual=$(sysctl -n "$param" 2>/dev/null || echo "N/A")
        expected="${EXPECTED[$param]}"
        if [[ "$actual" == "$expected" ]]; then
            KERNEL_PARAMS+="[OK]      $param = $actual (expected: $expected)\n"
        else
            KERNEL_PARAMS+="[WARNING] $param = $actual (expected: $expected)\n"
            KERNEL_STATUS="warning"
        fi
    done
    save_result "kernel" "security_params" "Kernel security parameters" "$KERNEL_STATUS" "$KERNEL_PARAMS"
}

# === Services ===
echo "[*] Checking services..." >&2
{
    # Enabled systemd services
    ENABLED_SERVICES=$(systemctl list-unit-files --type=service --state=enabled --no-pager 2>/dev/null || echo "systemctl not available")
    save_result "services" "enabled" "Enabled systemd services" "info" "$ENABLED_SERVICES"

    # Running services
    RUNNING_SERVICES=$(systemctl list-units --type=service --state=running --no-pager 2>/dev/null || echo "systemctl not available")
    save_result "services" "running" "Running services" "info" "$RUNNING_SERVICES"

    # Cron jobs for all users
    CRON_JOBS=""
    for user in $(cut -d: -f1 /etc/passwd); do
        user_cron=$(crontab -l -u "$user" 2>/dev/null || true)
        if [[ -n "$user_cron" ]]; then
            CRON_JOBS+="--- $user ---\n$user_cron\n\n"
        fi
    done
    # System cron
    if [[ -d /etc/cron.d ]]; then
        for f in /etc/cron.d/*; do
            if [[ -f "$f" ]]; then
                CRON_JOBS+="--- $f ---\n$(cat "$f")\n\n"
            fi
        done
    fi
    save_result "services" "cron_jobs" "Cron jobs (all users + system)" "info" "${CRON_JOBS:-No cron jobs found}"
}

# === Logging ===
echo "[*] Checking logging configuration..." >&2
{
    # rsyslog
    if systemctl is-active rsyslog &>/dev/null; then
        save_result "logging" "rsyslog" "rsyslog service" "ok" "Active and running"
    elif systemctl is-enabled rsyslog &>/dev/null; then
        save_result "logging" "rsyslog" "rsyslog service" "warning" "Enabled but not running"
    else
        save_result "logging" "rsyslog" "rsyslog service" "info" "Not installed or not enabled"
    fi

    # journald
    if systemctl is-active systemd-journald &>/dev/null; then
        JOURNAL_CONF=""
        if [[ -f /etc/systemd/journald.conf ]]; then
            JOURNAL_CONF=$(grep -v '^#' /etc/systemd/journald.conf | grep -v '^$' || echo "(all defaults)")
        fi
        save_result "logging" "journald" "journald configuration" "ok" "Active. Config:\n$JOURNAL_CONF"
    fi

    # Log file permissions
    LOG_PERMS=""
    for logfile in /var/log/syslog /var/log/auth.log /var/log/kern.log /var/log/messages /var/log/secure; do
        if [[ -f "$logfile" ]]; then
            LOG_PERMS+="$(ls -la "$logfile")\n"
        fi
    done
    save_result "logging" "log_permissions" "Log file permissions" "info" "${LOG_PERMS:-No standard log files found}"

    # auditd
    if tool_available auditctl; then
        AUDIT_STATUS=$(auditctl -s 2>&1)
        AUDIT_RULES=$(auditctl -l 2>&1)
        save_result "logging" "auditd" "Audit framework" "info" "Status:\n$AUDIT_STATUS\n\nRules:\n$AUDIT_RULES"
    else
        save_result "logging" "auditd" "Audit framework" "info" "auditd not installed"
    fi
}

# === Lynis ===
echo "[*] Running Lynis audit (this may take a while)..." >&2
{
    if tool_available lynis; then
        LYNIS_OUTPUT=$(lynis audit system --quick --no-colors 2>&1 || true)
        # Extract hardening index
        LYNIS_SCORE=$(echo "$LYNIS_OUTPUT" | grep -i "hardening index" | head -1 || echo "Score not found")
        save_result "lynis" "audit" "Lynis system audit" "info" "$LYNIS_OUTPUT"
        save_result "lynis" "score" "Lynis hardening index" "info" "$LYNIS_SCORE"
        # Save warnings and suggestions
        LYNIS_WARNINGS=$(echo "$LYNIS_OUTPUT" | sed -n '/Warnings/,/Suggestions/p' | head -50 || true)
        LYNIS_SUGGESTIONS=$(echo "$LYNIS_OUTPUT" | sed -n '/Suggestions/,/^$/p' | head -100 || true)
        save_result "lynis" "warnings" "Lynis warnings" "info" "${LYNIS_WARNINGS:-None}"
        save_result "lynis" "suggestions" "Lynis suggestions" "info" "${LYNIS_SUGGESTIONS:-None}"
    else
        save_result "lynis" "audit" "Lynis system audit" "skipped" "lynis not installed"
    fi
}

# ---------------------------------------------------------------------------
# FORENSIC MODE CHECKS (additional)
# ---------------------------------------------------------------------------

if [[ "$MODE" == "forensic" ]]; then
    echo "[*] === Forensic mode: additional checks ===" >&2

    # === rkhunter ===
    echo "[*] Running rkhunter..." >&2
    {
        if tool_available rkhunter; then
            # Update database first
            rkhunter --update 2>&1 >/dev/null || true
            RKH_OUTPUT=$(rkhunter --check --skip-keypress --report-warnings-only 2>&1 || true)
            if echo "$RKH_OUTPUT" | grep -qi "warning"; then
                save_result "forensic" "rkhunter" "rkhunter rootkit scan" "warning" "$RKH_OUTPUT"
            else
                save_result "forensic" "rkhunter" "rkhunter rootkit scan" "ok" "$RKH_OUTPUT"
            fi
        else
            save_result "forensic" "rkhunter" "rkhunter rootkit scan" "skipped" "rkhunter not installed"
        fi
    }

    # === chkrootkit ===
    echo "[*] Running chkrootkit..." >&2
    {
        if tool_available chkrootkit; then
            CHKRK_OUTPUT=$(chkrootkit 2>&1 || true)
            if echo "$CHKRK_OUTPUT" | grep -qi "INFECTED"; then
                save_result "forensic" "chkrootkit" "chkrootkit scan" "warning" "$CHKRK_OUTPUT"
            else
                save_result "forensic" "chkrootkit" "chkrootkit scan" "ok" "$CHKRK_OUTPUT"
            fi
        else
            save_result "forensic" "chkrootkit" "chkrootkit scan" "skipped" "chkrootkit not installed"
        fi
    }

    # === unhide (hidden processes) ===
    echo "[*] Running unhide (hidden processes)..." >&2
    {
        if tool_available unhide; then
            UNHIDE_OUTPUT=$(unhide sys 2>&1 || true)
            if echo "$UNHIDE_OUTPUT" | grep -qi "found hidden"; then
                save_result "forensic" "unhide_proc" "Hidden process detection" "warning" "$UNHIDE_OUTPUT"
            else
                save_result "forensic" "unhide_proc" "Hidden process detection" "ok" "$UNHIDE_OUTPUT"
            fi
        else
            save_result "forensic" "unhide_proc" "Hidden process detection" "skipped" "unhide not installed"
        fi
    }

    # === unhide-tcp (hidden ports) ===
    echo "[*] Running unhide-tcp (hidden ports)..." >&2
    {
        if tool_available unhide-tcp; then
            UNHIDE_TCP=$(unhide-tcp 2>&1 || true)
            if echo "$UNHIDE_TCP" | grep -qi "found hidden"; then
                save_result "forensic" "unhide_tcp" "Hidden port detection" "warning" "$UNHIDE_TCP"
            else
                save_result "forensic" "unhide_tcp" "Hidden port detection" "ok" "$UNHIDE_TCP"
            fi
        else
            save_result "forensic" "unhide_tcp" "Hidden port detection" "skipped" "unhide-tcp not installed"
        fi
    }

    # === debsums (package integrity) ===
    echo "[*] Running debsums (package integrity)..." >&2
    {
        if tool_available debsums; then
            DEBSUMS_OUTPUT=$(debsums -s 2>&1 || true)
            if [[ -n "$DEBSUMS_OUTPUT" ]]; then
                save_result "forensic" "debsums" "Package file integrity (changed files)" "warning" "$DEBSUMS_OUTPUT"
            else
                save_result "forensic" "debsums" "Package file integrity" "ok" "No altered package files detected"
            fi
        else
            save_result "forensic" "debsums" "Package file integrity" "skipped" "debsums not installed"
        fi
    }

    # === Suspicious cron / at jobs ===
    echo "[*] Checking for suspicious scheduled jobs..." >&2
    {
        SUSP_CRON=""
        # Check for base64/curl/wget/nc in cron
        for crondir in /etc/cron.d /etc/cron.daily /etc/cron.hourly /etc/cron.weekly /etc/cron.monthly /var/spool/cron/crontabs; do
            if [[ -d "$crondir" ]]; then
                SUSP=$(grep -rl 'base64\|curl.*|.*sh\|wget.*|.*sh\|nc\s\+-\|ncat\|/dev/tcp\|python.*-c\|perl.*-e' "$crondir" 2>/dev/null || true)
                if [[ -n "$SUSP" ]]; then
                    SUSP_CRON+="Suspicious patterns in $crondir:\n$SUSP\n"
                fi
            fi
        done
        # at jobs
        AT_JOBS=$(atq 2>/dev/null || echo "at not available")
        SUSP_CRON+="at queue:\n$AT_JOBS\n"

        if [[ -n "$SUSP_CRON" ]] && echo "$SUSP_CRON" | grep -q "Suspicious"; then
            save_result "forensic" "suspicious_cron" "Suspicious scheduled jobs" "warning" "$SUSP_CRON"
        else
            save_result "forensic" "suspicious_cron" "Suspicious scheduled jobs" "ok" "$SUSP_CRON"
        fi
    }

    # === Suspicious files in /tmp and /dev/shm ===
    echo "[*] Checking /tmp and /dev/shm for suspicious files..." >&2
    {
        SUSP_FILES=""
        for dir in /tmp /dev/shm /var/tmp; do
            if [[ -d "$dir" ]]; then
                # ELF binaries
                ELF=$(find "$dir" -type f -exec file {} \; 2>/dev/null | grep "ELF" || true)
                if [[ -n "$ELF" ]]; then
                    SUSP_FILES+="ELF binaries in $dir:\n$ELF\n\n"
                fi
                # Scripts
                SCRIPTS=$(find "$dir" -type f \( -name "*.sh" -o -name "*.py" -o -name "*.pl" -o -name "*.rb" \) 2>/dev/null || true)
                if [[ -n "$SCRIPTS" ]]; then
                    SUSP_FILES+="Scripts in $dir:\n$SCRIPTS\n\n"
                fi
                # Hidden files
                HIDDEN=$(find "$dir" -maxdepth 2 -name ".*" -type f 2>/dev/null || true)
                if [[ -n "$HIDDEN" ]]; then
                    SUSP_FILES+="Hidden files in $dir:\n$HIDDEN\n\n"
                fi
            fi
        done
        if [[ -n "$SUSP_FILES" ]]; then
            save_result "forensic" "tmp_suspicious" "Suspicious files in temp directories" "warning" "$SUSP_FILES"
        else
            save_result "forensic" "tmp_suspicious" "Suspicious files in temp directories" "ok" "Nothing suspicious found"
        fi
    }

    # === Recently modified system binaries ===
    echo "[*] Checking recently modified system binaries..." >&2
    {
        RECENT_MODS=$(find /usr/bin /usr/sbin /bin /sbin -mtime -7 -type f 2>/dev/null | sort || true)
        if [[ -n "$RECENT_MODS" ]]; then
            save_result "forensic" "recent_bin_mods" "System binaries modified in last 7 days" "warning" "$RECENT_MODS"
        else
            save_result "forensic" "recent_bin_mods" "System binaries modified in last 7 days" "ok" "None found"
        fi
    }

    # === Unusual SUID files (not from packages) ===
    echo "[*] Checking for unusual SUID files..." >&2
    {
        UNUSUAL_SUID=""
        while IFS= read -r suid_file; do
            if ! dpkg -S "$suid_file" &>/dev/null; then
                UNUSUAL_SUID+="$suid_file (not from any installed package)\n"
            fi
        done < <(find / -perm -4000 -type f 2>/dev/null)
        if [[ -n "$UNUSUAL_SUID" ]]; then
            save_result "forensic" "unusual_suid" "SUID files not belonging to packages" "warning" "$UNUSUAL_SUID"
        else
            save_result "forensic" "unusual_suid" "SUID files not belonging to packages" "ok" "All SUID files belong to known packages"
        fi
    }

    # === Process hiding check (ps vs /proc) ===
    echo "[*] Comparing ps output with /proc..." >&2
    {
        PS_PIDS=$(ps -eo pid --no-headers | tr -d ' ' | sort -n)
        PROC_PIDS=$(ls -1 /proc/ | grep '^[0-9]' | sort -n)
        HIDDEN_PIDS=$(comm -13 <(echo "$PS_PIDS") <(echo "$PROC_PIDS") || true)
        # Filter out kernel threads and short-lived processes
        REAL_HIDDEN=""
        for pid in $HIDDEN_PIDS; do
            if [[ -f "/proc/$pid/cmdline" ]]; then
                cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
                if [[ -n "$cmdline" ]]; then
                    REAL_HIDDEN+="PID $pid: $cmdline\n"
                fi
            fi
        done
        if [[ -n "$REAL_HIDDEN" ]]; then
            save_result "forensic" "hidden_procs" "Processes visible in /proc but not in ps" "warning" "$REAL_HIDDEN"
        else
            save_result "forensic" "hidden_procs" "Process hiding check (ps vs /proc)" "ok" "No hidden processes detected"
        fi
    }

    # === Login anomalies ===
    echo "[*] Checking login anomalies..." >&2
    {
        # Failed logins
        FAILED_LOGINS=$(lastb 2>/dev/null | head -50 || echo "lastb not available")
        save_result "forensic" "failed_logins" "Recent failed login attempts" "info" "$FAILED_LOGINS"

        # Last logins
        LAST_LOGINS=$(last -50 2>/dev/null || echo "last not available")
        save_result "forensic" "last_logins" "Recent successful logins" "info" "$LAST_LOGINS"

        # Auth log analysis
        if [[ -f /var/log/auth.log ]]; then
            AUTH_FAILURES=$(grep -i "authentication failure\|failed password" /var/log/auth.log 2>/dev/null | tail -30 || true)
            save_result "forensic" "auth_failures" "Authentication failures from auth.log" "info" "${AUTH_FAILURES:-None found}"
        fi
    }

    # === Network anomalies ===
    echo "[*] Checking network connections..." >&2
    {
        # Established connections
        ESTAB=$(ss -tnp state established 2>&1 || true)
        save_result "forensic" "established_conns" "Established TCP connections" "info" "$ESTAB"

        # DNS configuration
        DNS_CONF=$(cat /etc/resolv.conf 2>/dev/null || echo "Cannot read resolv.conf")
        save_result "forensic" "dns_config" "DNS configuration" "info" "$DNS_CONF"

        # Check for unusual outbound connections (non-standard ports)
        UNUSUAL_CONNS=$(ss -tnp state established 2>/dev/null | awk '$4 !~ /:443$|:80$|:53$|:22$|:8080$/ {print}' | tail -20 || true)
        if [[ -n "$UNUSUAL_CONNS" ]]; then
            save_result "forensic" "unusual_conns" "Connections to non-standard ports" "info" "$UNUSUAL_CONNS"
        fi
    }

    # === Unknown systemd services ===
    echo "[*] Checking for unknown systemd services..." >&2
    {
        UNKNOWN_SVC=""
        while IFS= read -r svc_file; do
            svc_name=$(basename "$svc_file")
            if ! dpkg -S "$svc_file" &>/dev/null 2>&1; then
                UNKNOWN_SVC+="$svc_file (not from a package)\n"
            fi
        done < <(find /etc/systemd/system /usr/lib/systemd/system /lib/systemd/system -name "*.service" -type f 2>/dev/null | sort -u)
        if [[ -n "$UNKNOWN_SVC" ]]; then
            save_result "forensic" "unknown_services" "Systemd services not from packages" "info" "$UNKNOWN_SVC"
        else
            save_result "forensic" "unknown_services" "Systemd services not from packages" "ok" "All services belong to known packages"
        fi
    }

    # === Persistence mechanisms ===
    echo "[*] Checking common persistence mechanisms..." >&2
    {
        PERSIST=""

        # .bashrc injection (check for curl/wget/nc/base64 in user bashrc files)
        for homedir in /home/* /root; do
            for rcfile in .bashrc .bash_profile .profile .zshrc; do
                if [[ -f "$homedir/$rcfile" ]]; then
                    SUSP_RC=$(grep -n 'curl\|wget\|nc \|ncat\|base64\|/dev/tcp\|python.*-c\|perl.*-e\|eval\s' "$homedir/$rcfile" 2>/dev/null || true)
                    if [[ -n "$SUSP_RC" ]]; then
                        PERSIST+="Suspicious in $homedir/$rcfile:\n$SUSP_RC\n\n"
                    fi
                fi
            done
        done

        # ld.so.preload
        if [[ -f /etc/ld.so.preload ]]; then
            LD_PRELOAD_CONTENT=$(cat /etc/ld.so.preload)
            if [[ -n "$LD_PRELOAD_CONTENT" ]]; then
                PERSIST+="ld.so.preload exists with content:\n$LD_PRELOAD_CONTENT\n\n"
            fi
        fi

        # PAM config modifications (check for non-package PAM modules)
        PAM_MODS=$(find /etc/pam.d -type f -newer /var/log/installer/syslog 2>/dev/null || true)
        if [[ -n "$PAM_MODS" ]]; then
            PERSIST+="Recently modified PAM configs:\n$PAM_MODS\n\n"
        fi

        # Check /etc/rc.local
        if [[ -f /etc/rc.local ]]; then
            RC_LOCAL=$(cat /etc/rc.local)
            PERSIST+="rc.local content:\n$RC_LOCAL\n\n"
        fi

        # Check init.d for non-package scripts
        CUSTOM_INIT=""
        for f in /etc/init.d/*; do
            if [[ -f "$f" ]] && ! dpkg -S "$f" &>/dev/null 2>&1; then
                CUSTOM_INIT+="$f (not from package)\n"
            fi
        done
        if [[ -n "$CUSTOM_INIT" ]]; then
            PERSIST+="Non-package init.d scripts:\n$CUSTOM_INIT\n\n"
        fi

        if [[ -n "$PERSIST" ]]; then
            save_result "forensic" "persistence" "Persistence mechanism checks" "warning" "$PERSIST"
        else
            save_result "forensic" "persistence" "Persistence mechanism checks" "ok" "No suspicious persistence mechanisms found"
        fi
    }
fi

# ---------------------------------------------------------------------------
# Assemble final JSON output
# ---------------------------------------------------------------------------
echo "[*] Assembling results..." >&2

python3 << 'PYEOF'
import json
import os
import sys

results_dir = os.environ.get("TMPDIR", "/tmp") + "/results"
if not os.path.isdir(results_dir):
    # Fallback: look at RESULTS_DIR
    print(json.dumps({"error": "Results directory not found"}))
    sys.exit(1)

output = {
    "mode": os.environ.get("MODE", "audit"),
    "categories": {}
}

# Walk the results directory
for category in sorted(os.listdir(results_dir)):
    cat_dir = os.path.join(results_dir, category)
    if not os.path.isdir(cat_dir):
        continue
    output["categories"][category] = {}
    for check_file in sorted(os.listdir(cat_dir)):
        if not check_file.endswith(".json"):
            continue
        filepath = os.path.join(cat_dir, check_file)
        try:
            with open(filepath, "r") as f:
                check_data = json.load(f)
            key = check_data.get("key", check_file.replace(".json", ""))
            output["categories"][category][key] = check_data
        except (json.JSONDecodeError, KeyError) as e:
            output["categories"][category][check_file] = {
                "key": check_file,
                "description": "Parse error",
                "status": "error",
                "data": f"Failed to parse: {e}"
            }

# Output final JSON
print(json.dumps(output, indent=2))
PYEOF

echo "[*] Audit complete. Mode: $MODE" >&2
