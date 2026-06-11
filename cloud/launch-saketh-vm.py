#!/usr/bin/env python3
"""
Launch a Lightsail VM to simulate Saketh's machine for Phase 3b testing.

Idempotent: re-running detects an existing instance and reuses it.

What it does:
  1. Launches a $3.50/mo Lightsail nano instance in ap-southeast-1
  2. Downloads the default SSH keypair (saved next to this script)
  3. Waits for the instance + SSH to be ready
  4. rsyncs the teammate-sync project to the VM
  5. Runs cloud/bootstrap-vm.sh with credentials passed via env
  6. Prints the SSH command and next steps

Run from the project root:
    .venv/bin/python cloud/launch-saketh-vm.py
"""
import argparse
import configparser
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


PROJECT_DIR = Path(__file__).resolve().parent.parent
CLOUD_DIR = Path(__file__).resolve().parent
SSH_KEY_PATH = CLOUD_DIR / "saketh-vm.pem"
BOOTSTRAP_PATH = CLOUD_DIR / "bootstrap-vm.sh"

REGION = "ap-southeast-1"
AVAILABILITY_ZONE = "ap-southeast-1a"
INSTANCE_NAME = "teammate-sync-saketh"
BUNDLE_ID = "micro_3_0"         # 1GB RAM — nano (512MB) OOMs during bootstrap. ~$5/mo prorated.
BLUEPRINT_ID = "ubuntu_24_04"
SSH_USER = "ubuntu"


def log(msg: str) -> None:
    print(f"[launch] {msg}", flush=True)


def aws_creds_from_file() -> tuple[str, str]:
    """Read default profile from ~/.aws/credentials."""
    cfg = configparser.ConfigParser()
    cfg.read(Path("~/.aws/credentials").expanduser())
    if "default" not in cfg:
        sys.exit("No [default] profile in ~/.aws/credentials")
    return cfg["default"]["aws_access_key_id"], cfg["default"]["aws_secret_access_key"]


def anthropic_key_from_mcp() -> str:
    """Pull the Anthropic key out of the MCP server config in ~/.claude.json."""
    data = json.loads(Path("~/.claude.json").expanduser().read_text())
    env = data.get("mcpServers", {}).get("teammate-sync", {}).get("env", {})
    key = env.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit(
            "Couldn't find ANTHROPIC_API_KEY in ~/.claude.json under "
            "mcpServers.teammate-sync.env — set it first via `claude mcp add`."
        )
    return key


def ensure_ssh_key(lightsail) -> None:
    """Download the default Lightsail keypair if we don't already have it locally."""
    if SSH_KEY_PATH.exists():
        log(f"reusing SSH key at {SSH_KEY_PATH}")
        return
    SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    log("downloading default Lightsail keypair...")
    resp = lightsail.download_default_key_pair()
    SSH_KEY_PATH.write_text(resp["privateKeyBase64"])
    SSH_KEY_PATH.chmod(0o600)
    log(f"saved to {SSH_KEY_PATH}")


def get_instance(lightsail):
    try:
        return lightsail.get_instance(instanceName=INSTANCE_NAME)["instance"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            return None
        raise


def ensure_instance(lightsail) -> dict:
    """Create the instance if it doesn't exist; return its info dict."""
    existing = get_instance(lightsail)
    if existing:
        log(f"instance {INSTANCE_NAME} already exists (state={existing['state']['name']})")
        return existing

    log(f"creating {INSTANCE_NAME} ({BUNDLE_ID} / {BLUEPRINT_ID}) in {REGION}...")
    lightsail.create_instances(
        instanceNames=[INSTANCE_NAME],
        availabilityZone=AVAILABILITY_ZONE,
        blueprintId=BLUEPRINT_ID,
        bundleId=BUNDLE_ID,
    )

    log("waiting for instance to be running...")
    while True:
        info = get_instance(lightsail)
        state = info["state"]["name"]
        if state == "running":
            log(f"instance is running with IP {info.get('publicIpAddress')}")
            return info
        time.sleep(5)
        log(f"  ...still {state}")


def wait_for_ssh(ip: str, timeout_seconds: int = 180) -> None:
    log(f"waiting for SSH on {ip}...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = subprocess.run(
            [
                "ssh",
                "-i", str(SSH_KEY_PATH),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5",
                "-o", "LogLevel=ERROR",
                f"{SSH_USER}@{ip}",
                "echo ready",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and b"ready" in result.stdout:
            log("SSH is ready")
            return
        time.sleep(5)
    sys.exit(f"SSH did not become ready within {timeout_seconds}s")


def rsync_project(ip: str) -> None:
    log("rsyncing project to VM (excluding venv, caches, secrets)...")
    subprocess.run(
        [
            "rsync", "-az", "--delete",
            "--exclude", ".venv/",
            "--exclude", "__pycache__/",
            "--exclude", "*.pyc",
            "--exclude", "cloud/saketh-vm.pem",
            "--exclude", "example_data/.sync-state.json",
            "-e",
            f"ssh -i {SSH_KEY_PATH} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
            f"{PROJECT_DIR}/",
            f"{SSH_USER}@{ip}:/home/{SSH_USER}/teammate-sync/",
        ],
        check=True,
    )


def run_bootstrap(ip: str, aws_key: str, aws_secret: str, anthropic_key: str) -> None:
    log("running bootstrap on VM (apt + pip + claude install + start daemon)...")
    bootstrap_text = BOOTSTRAP_PATH.read_text()
    env_prefix = (
        f"AWS_ACCESS_KEY_ID={aws_key} "
        f"AWS_SECRET_ACCESS_KEY={aws_secret} "
        f"ANTHROPIC_API_KEY={anthropic_key}"
    )
    cmd = [
        "ssh",
        "-i", str(SSH_KEY_PATH),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        f"{SSH_USER}@{ip}",
        f"{env_prefix} bash -s",
    ]
    result = subprocess.run(cmd, input=bootstrap_text, text=True)
    if result.returncode != 0:
        sys.exit(f"bootstrap failed with exit code {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rebootstrap",
        action="store_true",
        help="Re-run bootstrap on existing instance (idempotent)",
    )
    args = parser.parse_args()

    aws_key, aws_secret = aws_creds_from_file()
    anthropic_key = anthropic_key_from_mcp()

    lightsail = boto3.client("lightsail", region_name=REGION)

    ensure_ssh_key(lightsail)
    instance = ensure_instance(lightsail)
    ip = instance.get("publicIpAddress")
    if not ip:
        sys.exit("instance has no public IP yet — try re-running in a few seconds")

    wait_for_ssh(ip)
    rsync_project(ip)
    run_bootstrap(ip, aws_key, aws_secret, anthropic_key)

    print()
    print("=" * 68)
    print(f"✓ Saketh VM ready at {ip}")
    print("=" * 68)
    print()
    print("SSH in:")
    print(f"  ssh -i {SSH_KEY_PATH} {SSH_USER}@{ip}")
    print()
    print("On the VM:")
    print("  - Daemon runs in screen session 'teammate-daemon'")
    print("    Attach:  screen -r teammate-daemon   (Ctrl-A then D to detach)")
    print("  - Edit Saketh's workspace: ~/saketh-workspace/.claude/CLAUDE.md")
    print("  - Start a Claude Code session as Saketh: claude")
    print()
    print("On YOUR Mac (REQUIRED before testing):")
    print("  pkill -f 'daemon.py'    # stop your local sim daemon to avoid")
    print("                          # collisions on the saketh/ S3 prefix")
    print()
    print("Then in a new Claude Code session on your Mac, ask the MCP")
    print("about what Saketh is working on — you'll see VM-side state.")
    print()
    print("Teardown when done (~$0):")
    print(f"  python3 -c \"import boto3; boto3.client('lightsail', region_name='{REGION}').delete_instance(instanceName='{INSTANCE_NAME}')\"")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
