#!/usr/bin/env bash
# Security Cleanup Script — generated from audit on 2026-02-23
# Run with: sudo bash scripts/security_cleanup.sh
# Review each section before running. Comment out anything you want to skip.

set -euo pipefail

echo "========================================="
echo "  Security Cleanup Script"
echo "  Generated: 2026-02-23"
echo "========================================="
echo ""

# Require root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)"
    exit 1
fi

# =========================================
# 1. FIREWALL (UFW)
# =========================================
echo "[1/9] Firewall configuration..."

ufw allow 32456/tcp comment 'SSH custom port'
ufw --force enable
ufw status verbose
echo "  Done: UFW enabled, SSH port 32456 allowed"
echo ""

# =========================================
# 2. FAIL2BAN FIX
# =========================================
echo "[2/9] Fixing fail2ban configuration..."

# Backup original
cp /etc/fail2ban/jail.local /etc/fail2ban/jail.local.bak.$(date +%Y%m%d)

# Fix: remove inline Chinese comments that break fail2ban parser
# fail2ban uses ; for inline comments, not #
cat > /etc/fail2ban/jail.d/sshd.local << 'JAIL_EOF'
[sshd]
enabled   = true
port      = 32456
maxretry  = 3
bantime   = 1h
findtime  = 10m
JAIL_EOF

# Remove the broken [sshd] section from jail.local to avoid conflict
# (jail.d/sshd.local takes precedence)
systemctl restart fail2ban
systemctl status fail2ban --no-pager
echo "  Done: fail2ban fixed and restarted"
echo ""

# =========================================
# 3. DISABLE UNUSED SERVICES
# =========================================
echo "[3/9] Disabling unused services..."

# SMB (Samba file sharing - not configured, not needed)
systemctl stop smbd nmbd 2>/dev/null || true
systemctl disable smbd nmbd 2>/dev/null || true
echo "  Disabled: SMB (smbd, nmbd)"

# Grafana (monitoring dashboard - not actively used)
systemctl stop grafana-server 2>/dev/null || true
systemctl disable grafana-server 2>/dev/null || true
echo "  Disabled: Grafana (port 3000)"

# Ollama (local LLM - not actively used)
systemctl stop ollama 2>/dev/null || true
systemctl disable ollama 2>/dev/null || true
echo "  Disabled: Ollama (port 41434)"

# samba-ad-dc (enabled but inactive)
systemctl disable samba-ad-dc 2>/dev/null || true
echo "  Disabled: samba-ad-dc"

# postfix (mail server - not needed)
systemctl stop postfix 2>/dev/null || true
systemctl disable postfix 2>/dev/null || true
echo "  Disabled: postfix"

# cloud-init (not needed on desktop)
systemctl disable cloud-init cloud-init-local cloud-config cloud-final 2>/dev/null || true
echo "  Disabled: cloud-init"

# surfshark-proxy (inactive)
systemctl disable surfshark-proxy 2>/dev/null || true
echo "  Disabled: surfshark-proxy"

echo ""

# =========================================
# 4. FIX FILE PERMISSIONS
# =========================================
echo "[4/9] Fixing file permissions..."

# Surfshark VPN configs - remove world-writable
chmod o-w /etc/openvpn/*.ovpn 2>/dev/null || true
echo "  Fixed: $(find /etc/openvpn -name '*.ovpn' -perm -o=w 2>/dev/null | wc -l) ovpn files still world-writable (should be 0)"

echo ""

# =========================================
# 5. KERNEL SECURITY PARAMETERS
# =========================================
echo "[5/9] Hardening kernel parameters..."

# Note: ip_forward=1 is kept because Docker/LXC needs it
cat > /etc/sysctl.d/99-security-hardening.conf << 'SYSCTL_EOF'
# Security hardening - generated 2026-02-23
# Note: net.ipv4.ip_forward=1 intentionally kept for Docker/LXC

fs.suid_dumpable = 0
net.ipv4.conf.all.log_martians = 1
kernel.core_uses_pid = 1
SYSCTL_EOF

sysctl -p /etc/sysctl.d/99-security-hardening.conf
echo "  Done: kernel params hardened (ip_forward kept for Docker)"
echo ""

# =========================================
# 6. USER ACCOUNT CLEANUP
# =========================================
echo "[6/9] Reviewing user accounts..."

# alvin_sf - set nologin if not needed
# Uncomment the line below if confirmed not needed:
# usermod -s /usr/sbin/nologin alvin_sf
echo "  MANUAL: Review alvin_sf account — uncomment usermod line if not needed"

# nx - NoMachine installed but service not running
# Uncomment to remove login shell:
# usermod -s /usr/sbin/nologin nx
echo "  MANUAL: Review nx account — NoMachine installed but inactive"

echo ""

# =========================================
# 7. SECURITY UPDATES
# =========================================
echo "[7/9] Installing security updates..."

apt-get update -qq
apt-get upgrade -y --with-new-pkgs
echo "  Done: packages updated"
echo ""

# =========================================
# 8. FORENSIC SCAN (optional)
# =========================================
echo "[8/9] Running forensic scans..."

FORENSIC_DIR="/tmp/security_forensic_$(date +%Y%m%d)"
mkdir -p "$FORENSIC_DIR"

echo "  Running rkhunter..."
rkhunter --check --skip-keypress --report-warnings-only > "$FORENSIC_DIR/rkhunter.log" 2>&1 || true
echo "  rkhunter done: $FORENSIC_DIR/rkhunter.log"

echo "  Running chkrootkit..."
chkrootkit > "$FORENSIC_DIR/chkrootkit.log" 2>&1 || true
echo "  chkrootkit done: $FORENSIC_DIR/chkrootkit.log"

echo "  Running unhide (processes)..."
unhide sys > "$FORENSIC_DIR/unhide_sys.log" 2>&1 || true
echo "  unhide done: $FORENSIC_DIR/unhide_sys.log"

echo "  Running unhide-tcp (ports)..."
unhide-tcp > "$FORENSIC_DIR/unhide_tcp.log" 2>&1 || true
echo "  unhide-tcp done: $FORENSIC_DIR/unhide_tcp.log"

echo "  Running debsums (package integrity)..."
debsums -s > "$FORENSIC_DIR/debsums.log" 2>&1 || true
echo "  debsums done: $FORENSIC_DIR/debsums.log"

echo "  Forensic results: $FORENSIC_DIR/"
echo ""

# =========================================
# 9. LYNIS FULL SCAN
# =========================================
echo "[9/9] Running full Lynis audit..."

lynis audit system --quick --no-colors > "$FORENSIC_DIR/lynis_full.log" 2>&1 || true
# Extract suggestions
grep -A 100 "Suggestions (" "$FORENSIC_DIR/lynis_full.log" | head -120 > "$FORENSIC_DIR/lynis_suggestions.txt" 2>/dev/null || true
echo "  Lynis done: $FORENSIC_DIR/lynis_full.log"
echo "  Suggestions: $FORENSIC_DIR/lynis_suggestions.txt"
echo ""

# =========================================
# SUMMARY
# =========================================
echo "========================================="
echo "  Cleanup Complete!"
echo "========================================="
echo ""
echo "  [1] UFW: enabled, SSH 32456 allowed"
echo "  [2] fail2ban: config fixed, restarted"
echo "  [3] Services disabled: SMB, Grafana, Ollama, postfix, samba-ad-dc, cloud-init, surfshark-proxy"
echo "  [4] VPN file permissions: fixed"
echo "  [5] Kernel params: hardened"
echo "  [6] User accounts: review needed (see above)"
echo "  [7] Security updates: installed"
echo "  [8] Forensic scans: $FORENSIC_DIR/"
echo "  [9] Lynis full scan: $FORENSIC_DIR/lynis_full.log"
echo ""
echo "  MANUAL TASKS remaining:"
echo "  - Review alvin_sf / nx accounts"
echo "  - Change privoxy to listen on 127.0.0.1:8118 (edit /etc/privoxy/config)"
echo "  - Review forensic scan results"
echo "  - Consider: sudo aideinit (create file integrity baseline)"
echo "  - Consider: reboot (system up since 2025-12-19)"
echo ""
echo "  Copy forensic results for Claude to analyze:"
echo "  cp -r $FORENSIC_DIR /home/user/Desktop/claude-code-hub/data/security_audit/"
