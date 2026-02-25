#!/usr/bin/env python3
"""
security_audit.py — Orchestrator for hybrid security audit.

Runs security_audit.sh with sudo, collects JSON results, and generates
a human-readable markdown report with risk scoring and recommendations.

Usage:
    python3 scripts/security_audit.py --mode audit|forensic [--output PATH]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from ccmux.paths import SECURITY_AUDIT_DIR

BASH_SCRIPT = SCRIPT_DIR / "security_audit.sh"
DEFAULT_OUTPUT_DIR = SECURITY_AUDIT_DIR

# Risk levels (ordered by severity)
RISK_CRITICAL = "Critical"
RISK_HIGH = "High"
RISK_MEDIUM = "Medium"
RISK_LOW = "Low"
RISK_INFO = "Info"

# Score weights
RISK_SCORES = {
    RISK_CRITICAL: 25,
    RISK_HIGH: 15,
    RISK_MEDIUM: 8,
    RISK_LOW: 3,
    RISK_INFO: 0,
}


# ---------------------------------------------------------------------------
# Rule-based analysis engine
# ---------------------------------------------------------------------------

class Finding:
    """Represents a single security finding."""

    def __init__(self, category: str, title: str, risk: str, details: str,
                 remediation: str = ""):
        self.category = category
        self.title = title
        self.risk = risk
        self.details = details
        self.remediation = remediation

    def __repr__(self):
        return f"Finding({self.risk}: {self.title})"


def analyze_results(data: dict) -> list[Finding]:
    """Analyze raw JSON results and produce findings."""
    findings: list[Finding] = []
    categories = data.get("categories", {})

    # --- User account checks ---
    users = categories.get("users", {})

    uid0 = users.get("uid0", {})
    if uid0.get("status") == "warning":
        findings.append(Finding(
            "User Accounts", "Multiple UID 0 accounts",
            RISK_CRITICAL,
            f"Found multiple accounts with UID 0 (root equivalent):\n{uid0.get('data', '')}",
            "Remove or disable extra UID 0 accounts. Only root should have UID 0."
        ))

    empty_pw = users.get("empty_passwords", {})
    if empty_pw.get("status") == "warning":
        findings.append(Finding(
            "User Accounts", "Accounts with empty passwords",
            RISK_CRITICAL,
            f"Accounts with empty passwords found:\n{empty_pw.get('data', '')}",
            "Set strong passwords for all accounts or lock unused accounts."
        ))

    shell_users = users.get("shell_users", {})
    if shell_users.get("data"):
        lines = [l for l in shell_users["data"].strip().split("\n") if l.strip()]
        if len(lines) > 5:
            findings.append(Finding(
                "User Accounts", f"Many accounts with login shells ({len(lines)})",
                RISK_LOW,
                f"Accounts with login shells:\n{shell_users['data']}",
                "Review whether all these accounts need interactive login. "
                "Set shell to /usr/sbin/nologin for service accounts."
            ))

    sudoers = users.get("sudoers", {})
    if sudoers.get("data") and sudoers["data"] != "None found":
        sudo_users = [u.strip() for u in sudoers["data"].split(",") if u.strip()]
        if len(sudo_users) > 3:
            findings.append(Finding(
                "User Accounts", f"Many sudo users ({len(sudo_users)})",
                RISK_MEDIUM,
                f"Users with sudo access: {', '.join(sudo_users)}",
                "Minimize sudo access. Use fine-grained sudoers rules instead of full sudo."
            ))

    # --- SSH checks ---
    ssh = categories.get("ssh", {})

    root_login = ssh.get("root_login", {})
    if root_login.get("status") == "warning":
        findings.append(Finding(
            "SSH", "Root login permitted via SSH",
            RISK_HIGH,
            f"SSH configuration: {root_login.get('data', '')}",
            "Set 'PermitRootLogin no' or 'PermitRootLogin prohibit-password' in sshd_config."
        ))

    pw_auth = ssh.get("password_auth", {})
    if pw_auth.get("status") == "warning":
        findings.append(Finding(
            "SSH", "Password authentication enabled",
            RISK_MEDIUM,
            f"SSH configuration: {pw_auth.get('data', '')}",
            "Set 'PasswordAuthentication no' and use key-based authentication only."
        ))

    # --- Firewall checks ---
    fw = categories.get("firewall", {})

    ufw = fw.get("ufw", {})
    if ufw.get("status") == "warning":
        findings.append(Finding(
            "Firewall", "UFW firewall is inactive",
            RISK_HIGH,
            f"UFW status: {ufw.get('data', '')}",
            "Enable the firewall: 'sudo ufw enable' and configure appropriate rules."
        ))
    elif ufw.get("status") == "skipped":
        # Check if iptables has rules
        iptables = fw.get("iptables", {})
        nftables = fw.get("nftables", {})
        has_rules = False
        if iptables.get("data") and "ACCEPT" in iptables["data"]:
            has_rules = True
        if nftables.get("data") and "table" in nftables.get("data", ""):
            has_rules = True
        if not has_rules:
            findings.append(Finding(
                "Firewall", "No firewall detected",
                RISK_HIGH,
                "No UFW, iptables rules, or nftables rules detected.",
                "Install and configure a firewall (ufw, iptables, or nftables)."
            ))

    open_ports = fw.get("open_ports", {})
    if open_ports.get("data"):
        port_lines = [l for l in open_ports["data"].strip().split("\n")
                      if l.strip() and not l.startswith("State")]
        if len(port_lines) > 10:
            findings.append(Finding(
                "Firewall", f"Many open ports ({len(port_lines)})",
                RISK_MEDIUM,
                f"Listening TCP ports:\n{open_ports['data']}",
                "Review all listening services. Disable unnecessary services and restrict access."
            ))

    # --- File permissions checks ---
    perms = categories.get("permissions", {})

    ww = perms.get("world_writable", {})
    if ww.get("status") == "warning":
        findings.append(Finding(
            "File Permissions", "World-writable files found",
            RISK_MEDIUM,
            f"World-writable files:\n{ww.get('data', '')}",
            "Remove world-writable permissions: chmod o-w <file>"
        ))

    tmp_sticky = perms.get("tmp_sticky", {})
    if tmp_sticky.get("status") == "warning":
        findings.append(Finding(
            "File Permissions", "/tmp missing sticky bit",
            RISK_HIGH,
            f"/tmp permissions: {tmp_sticky.get('data', '')}",
            "Set sticky bit: chmod +t /tmp"
        ))

    suid = perms.get("suid_files", {})
    if suid.get("data"):
        suid_lines = [l for l in suid["data"].strip().split("\n") if l.strip()]
        if len(suid_lines) > 30:
            findings.append(Finding(
                "File Permissions", f"High number of SUID files ({len(suid_lines)})",
                RISK_LOW,
                f"Found {len(suid_lines)} SUID files. Review for unnecessary SUID binaries.",
                "Audit SUID files and remove the SUID bit from any that don't require it."
            ))

    # --- Package checks ---
    pkgs = categories.get("packages", {})

    sec_updates = pkgs.get("security_updates", {})
    if sec_updates.get("status") == "warning":
        findings.append(Finding(
            "Packages", "Security updates available",
            RISK_HIGH,
            f"Pending security updates:\n{sec_updates.get('data', '')}",
            "Apply security updates: sudo apt-get update && sudo apt-get upgrade"
        ))

    upgradable = pkgs.get("upgradable", {})
    if upgradable.get("status") == "warning":
        findings.append(Finding(
            "Packages", "Package updates available",
            RISK_MEDIUM,
            f"Upgradable packages:\n{upgradable.get('data', '')}",
            "Apply updates: sudo apt-get update && sudo apt-get upgrade"
        ))

    # --- Kernel checks ---
    kernel = categories.get("kernel", {})

    kparams = kernel.get("security_params", {})
    if kparams.get("status") == "warning":
        # Count warnings
        data_str = kparams.get("data", "")
        warning_count = data_str.count("[WARNING]")
        findings.append(Finding(
            "Kernel", f"Kernel security parameters not optimal ({warning_count} issues)",
            RISK_MEDIUM,
            f"Kernel parameter check:\n{data_str}",
            "Update /etc/sysctl.conf with recommended values and run 'sysctl -p'."
        ))

    # --- Logging checks ---
    logging_cat = categories.get("logging", {})

    rsyslog = logging_cat.get("rsyslog", {})
    if rsyslog.get("status") == "warning":
        findings.append(Finding(
            "Logging", "rsyslog not running",
            RISK_MEDIUM,
            f"rsyslog status: {rsyslog.get('data', '')}",
            "Start rsyslog: sudo systemctl start rsyslog && sudo systemctl enable rsyslog"
        ))

    auditd = logging_cat.get("auditd", {})
    if auditd.get("data") and "not installed" in auditd.get("data", ""):
        findings.append(Finding(
            "Logging", "Audit framework not installed",
            RISK_LOW,
            "auditd is not installed. System auditing capabilities are limited.",
            "Install auditd: sudo apt-get install auditd and configure audit rules."
        ))

    # --- Lynis checks ---
    lynis = categories.get("lynis", {})

    lynis_score = lynis.get("score", {})
    if lynis_score.get("data") and lynis_score["data"] != "Score not found":
        score_data = lynis_score["data"]
        # Extract numeric score
        import re
        score_match = re.search(r'(\d+)', score_data)
        if score_match:
            score_val = int(score_match.group(1))
            if score_val < 50:
                risk = RISK_HIGH
            elif score_val < 70:
                risk = RISK_MEDIUM
            elif score_val < 85:
                risk = RISK_LOW
            else:
                risk = RISK_INFO
            findings.append(Finding(
                "Lynis", f"Lynis hardening score: {score_val}/100",
                risk,
                f"Lynis reports a hardening index of {score_val}.",
                "Review Lynis suggestions and implement hardening recommendations."
            ))

    lynis_warnings = lynis.get("warnings", {})
    if lynis_warnings.get("data") and lynis_warnings["data"] != "None":
        findings.append(Finding(
            "Lynis", "Lynis warnings",
            RISK_MEDIUM,
            f"Lynis warnings:\n{lynis_warnings.get('data', '')}",
            "Address each Lynis warning. Run 'lynis show details <TEST-ID>' for specifics."
        ))

    # --- Forensic checks ---
    forensic = categories.get("forensic", {})

    rkhunter = forensic.get("rkhunter", {})
    if rkhunter.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Rootkit", "rkhunter found warnings",
            RISK_CRITICAL,
            f"rkhunter output:\n{rkhunter.get('data', '')}",
            "Investigate each rkhunter warning. Some may be false positives "
            "due to system updates."
        ))
    elif rkhunter.get("status") == "ok":
        findings.append(Finding(
            "Forensic - Rootkit", "rkhunter scan clean",
            RISK_INFO,
            "rkhunter did not detect any rootkit indicators.",
            ""
        ))

    chkrootkit = forensic.get("chkrootkit", {})
    if chkrootkit.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Rootkit", "chkrootkit found INFECTED indicators",
            RISK_CRITICAL,
            f"chkrootkit output:\n{chkrootkit.get('data', '')}",
            "Investigate immediately. Verify findings manually — chkrootkit "
            "can produce false positives."
        ))

    unhide_proc = forensic.get("unhide_proc", {})
    if unhide_proc.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Processes", "Hidden processes detected",
            RISK_CRITICAL,
            f"unhide output:\n{unhide_proc.get('data', '')}",
            "Hidden processes are a strong IOC. Investigate immediately."
        ))

    unhide_tcp = forensic.get("unhide_tcp", {})
    if unhide_tcp.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Network", "Hidden TCP ports detected",
            RISK_CRITICAL,
            f"unhide-tcp output:\n{unhide_tcp.get('data', '')}",
            "Hidden ports indicate possible backdoor. Investigate immediately."
        ))

    debsums = forensic.get("debsums", {})
    if debsums.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Integrity", "Package file integrity violations",
            RISK_HIGH,
            f"Changed package files:\n{debsums.get('data', '')}",
            "Reinstall affected packages: sudo apt-get install --reinstall <package>"
        ))

    tmp_susp = forensic.get("tmp_suspicious", {})
    if tmp_susp.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Files", "Suspicious files in temp directories",
            RISK_HIGH,
            f"Suspicious files:\n{tmp_susp.get('data', '')}",
            "Investigate and remove suspicious files from /tmp, /dev/shm, /var/tmp."
        ))

    recent_bins = forensic.get("recent_bin_mods", {})
    if recent_bins.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Integrity", "Recently modified system binaries",
            RISK_HIGH,
            f"Binaries modified in last 7 days:\n{recent_bins.get('data', '')}",
            "Verify these changes correspond to known updates. Use debsums to check integrity."
        ))

    unusual_suid = forensic.get("unusual_suid", {})
    if unusual_suid.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Files", "SUID files not from packages",
            RISK_HIGH,
            f"Unusual SUID files:\n{unusual_suid.get('data', '')}",
            "Investigate each SUID file. Remove SUID bit if not needed: chmod u-s <file>"
        ))

    hidden_procs = forensic.get("hidden_procs", {})
    if hidden_procs.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Processes", "Processes hidden from ps",
            RISK_CRITICAL,
            f"Hidden processes:\n{hidden_procs.get('data', '')}",
            "Processes visible in /proc but not ps indicate kernel-level hiding. "
            "Investigate immediately."
        ))

    persistence = forensic.get("persistence", {})
    if persistence.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Persistence", "Suspicious persistence mechanisms",
            RISK_HIGH,
            f"Persistence checks:\n{persistence.get('data', '')}",
            "Review each finding. Check .bashrc, ld.so.preload, PAM, rc.local, "
            "and init.d scripts for unauthorized modifications."
        ))

    suspicious_cron = forensic.get("suspicious_cron", {})
    if suspicious_cron.get("status") == "warning":
        findings.append(Finding(
            "Forensic - Persistence", "Suspicious cron/at entries",
            RISK_HIGH,
            f"Suspicious scheduled jobs:\n{suspicious_cron.get('data', '')}",
            "Review scheduled jobs for unauthorized entries. Check for encoded "
            "payloads and reverse shells."
        ))

    unknown_svc = forensic.get("unknown_services", {})
    if unknown_svc.get("data") and unknown_svc.get("status") != "ok":
        findings.append(Finding(
            "Forensic - Services", "Non-package systemd services",
            RISK_MEDIUM,
            f"Custom systemd services:\n{unknown_svc.get('data', '')}",
            "Review each custom service. Verify it is legitimate and authorized."
        ))

    return findings


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def compute_score(findings: list[Finding]) -> int:
    """Compute overall security score (0-100, higher is better)."""
    total_deductions = sum(RISK_SCORES.get(f.risk, 0) for f in findings)
    score = max(0, 100 - total_deductions)
    return score


def score_to_grade(score: int) -> str:
    """Convert numeric score to letter grade."""
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    elif score >= 60:
        return "D"
    else:
        return "F"


def generate_report(data: dict, findings: list[Finding], mode: str) -> str:
    """Generate a markdown report from findings."""
    now = datetime.now()
    score = compute_score(findings)
    grade = score_to_grade(score)

    # Count by risk
    risk_counts = {}
    for f in findings:
        risk_counts[f.risk] = risk_counts.get(f.risk, 0) + 1

    # Build report
    lines: list[str] = []
    lines.append(f"# Security Audit Report")
    lines.append(f"")
    lines.append(f"**Date**: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Mode**: {mode.capitalize()}")

    # System info
    sys_info = data.get("categories", {}).get("system", {}).get("info", {})
    if sys_info.get("data"):
        lines.append(f"")
        lines.append(f"## System Information")
        lines.append(f"```")
        lines.append(sys_info["data"].strip())
        lines.append(f"```")

    # Executive summary
    lines.append(f"")
    lines.append(f"## Executive Summary")
    lines.append(f"")
    lines.append(f"**Overall Security Score: {score}/100 (Grade: {grade})**")
    lines.append(f"")

    risk_order = [RISK_CRITICAL, RISK_HIGH, RISK_MEDIUM, RISK_LOW, RISK_INFO]
    risk_emoji = {
        RISK_CRITICAL: "[!!!]",
        RISK_HIGH: "[!!]",
        RISK_MEDIUM: "[!]",
        RISK_LOW: "[.]",
        RISK_INFO: "[i]",
    }

    lines.append(f"| Risk Level | Count |")
    lines.append(f"|------------|-------|")
    for risk in risk_order:
        count = risk_counts.get(risk, 0)
        if count > 0:
            lines.append(f"| {risk_emoji[risk]} {risk} | {count} |")
    lines.append(f"")

    # Score interpretation
    if score >= 90:
        lines.append("The system has a strong security posture. Continue monitoring.")
    elif score >= 80:
        lines.append("The system has a good security posture with minor improvements needed.")
    elif score >= 70:
        lines.append("The system has a moderate security posture. Several improvements recommended.")
    elif score >= 60:
        lines.append("The system has a below-average security posture. Action needed.")
    else:
        lines.append("**The system has a poor security posture. Immediate action required.**")

    # Findings by category
    lines.append(f"")
    lines.append(f"## Findings")

    # Group findings by category
    cat_findings: dict[str, list[Finding]] = {}
    for f in findings:
        cat_findings.setdefault(f.category, []).append(f)

    # Sort categories: those with critical findings first
    def cat_sort_key(cat_name):
        cat_list = cat_findings[cat_name]
        worst = min(risk_order.index(f.risk) for f in cat_list)
        return worst

    for cat_name in sorted(cat_findings, key=cat_sort_key):
        cat_list = cat_findings[cat_name]
        # Sort findings within category by severity
        cat_list.sort(key=lambda f: risk_order.index(f.risk))

        lines.append(f"")
        lines.append(f"### {cat_name}")
        lines.append(f"")

        for f in cat_list:
            lines.append(f"#### {risk_emoji[f.risk]} {f.title}")
            lines.append(f"**Risk**: {f.risk}")
            lines.append(f"")
            # Truncate very long details
            detail_lines = f.details.strip().split("\n")
            if len(detail_lines) > 50:
                detail_text = "\n".join(detail_lines[:50]) + f"\n... ({len(detail_lines) - 50} more lines)"
            else:
                detail_text = f.details.strip()
            lines.append(f"```")
            lines.append(detail_text)
            lines.append(f"```")
            if f.remediation:
                lines.append(f"")
                lines.append(f"**Remediation**: {f.remediation}")
            lines.append(f"")

    # Remediation summary
    actionable = [f for f in findings if f.risk in (RISK_CRITICAL, RISK_HIGH, RISK_MEDIUM)]
    if actionable:
        lines.append(f"## Recommended Actions (Priority Order)")
        lines.append(f"")
        actionable.sort(key=lambda f: risk_order.index(f.risk))
        for i, f in enumerate(actionable, 1):
            lines.append(f"{i}. **[{f.risk}]** {f.title}: {f.remediation}")
        lines.append(f"")

    # Raw data reference
    lines.append(f"## Raw Data Summary")
    lines.append(f"")
    categories = data.get("categories", {})
    for cat_name in sorted(categories):
        checks = categories[cat_name]
        check_count = len(checks)
        statuses = [c.get("status", "unknown") for c in checks.values() if isinstance(c, dict)]
        status_summary = ", ".join(f"{s}: {statuses.count(s)}" for s in set(statuses))
        lines.append(f"- **{cat_name}**: {check_count} checks ({status_summary})")
    lines.append(f"")

    # Footer
    lines.append(f"---")
    lines.append(f"*Report generated by security_audit.py on {now.strftime('%Y-%m-%d %H:%M:%S')}*")
    lines.append(f"*Mode: {mode} | Score: {score}/100 | Grade: {grade}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Security audit orchestrator")
    parser.add_argument("--mode", choices=["audit", "forensic"], default="audit",
                        help="Audit mode (default: audit)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output file path (default: data/security_audit/YYYY-MM-DD_MODE.md)")
    args = parser.parse_args()

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now().strftime('%Y-%m-%d')}_{args.mode}.md"
        output_path = DEFAULT_OUTPUT_DIR / filename

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Check that bash script exists
    if not BASH_SCRIPT.exists():
        print(f"Error: Bash script not found at {BASH_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    # Run the bash script (with sudo if available, otherwise degraded)
    print(f"[*] Running security audit in {args.mode} mode...")
    print(f"[*] This may take several minutes, especially in forensic mode.")
    print()

    # Try sudo first; fall back to non-root with degraded results
    cmd = ["bash", str(BASH_SCRIPT), args.mode]
    try:
        # Check if sudo is available without password
        sudo_check = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5
        )
        if sudo_check.returncode == 0:
            cmd = ["sudo"] + cmd
        else:
            print("[!] sudo requires a password — running without root (some checks degraded)")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("[!] sudo not available — running without root (some checks degraded)")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minute timeout
        )
    except subprocess.TimeoutExpired:
        print("Error: Audit script timed out after 30 minutes.", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: Cannot execute bash script. Is bash installed?", file=sys.stderr)
        sys.exit(1)

    # Print stderr (progress messages)
    if result.stderr:
        for line in result.stderr.strip().split("\n"):
            print(f"  {line}")
        print()

    # Parse JSON output
    stdout = result.stdout.strip()
    if not stdout:
        print("Error: No output from audit script.", file=sys.stderr)
        if result.returncode != 0:
            print(f"Script exited with code {result.returncode}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse JSON output: {e}", file=sys.stderr)
        # Try to find JSON in output (skip any non-JSON prefix)
        for i, char in enumerate(stdout):
            if char == '{':
                try:
                    data = json.loads(stdout[i:])
                    break
                except json.JSONDecodeError:
                    continue
        else:
            print("Could not find valid JSON in output.", file=sys.stderr)
            # Save raw output for debugging
            raw_path = output_path.with_suffix(".raw.txt")
            raw_path.write_text(stdout)
            print(f"Raw output saved to {raw_path}", file=sys.stderr)
            sys.exit(1)

    if "error" in data:
        print(f"Error from audit script: {data['error']}", file=sys.stderr)
        sys.exit(1)

    # Save raw JSON
    json_path = output_path.with_suffix(".json")
    json_path.write_text(json.dumps(data, indent=2))
    print(f"[*] Raw JSON saved to {json_path}")

    # Analyze results
    print(f"[*] Analyzing results...")
    findings = analyze_results(data)

    # Generate report
    report = generate_report(data, findings, args.mode)

    # Save report
    output_path.write_text(report)
    print(f"[*] Report saved to {output_path}")
    print()

    # Print summary to stdout
    score = compute_score(findings)
    grade = score_to_grade(score)

    risk_order = [RISK_CRITICAL, RISK_HIGH, RISK_MEDIUM, RISK_LOW, RISK_INFO]
    risk_counts = {}
    for f in findings:
        risk_counts[f.risk] = risk_counts.get(f.risk, 0) + 1

    print("=" * 60)
    print(f"  SECURITY AUDIT SUMMARY ({args.mode.upper()} MODE)")
    print("=" * 60)
    print(f"  Score: {score}/100 (Grade: {grade})")
    print()
    for risk in risk_order:
        count = risk_counts.get(risk, 0)
        if count > 0:
            print(f"  {risk:>10}: {count}")
    print()
    print(f"  Total findings: {len(findings)}")
    print("=" * 60)

    # Print critical/high findings
    critical_high = [f for f in findings if f.risk in (RISK_CRITICAL, RISK_HIGH)]
    if critical_high:
        print()
        print("  CRITICAL/HIGH FINDINGS:")
        for f in critical_high:
            print(f"  - [{f.risk}] {f.title}")
    print()
    print(f"  Full report: {output_path}")


if __name__ == "__main__":
    main()
