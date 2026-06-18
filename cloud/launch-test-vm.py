#!/usr/bin/env python3
"""
Minimal Lightsail VM launcher for two-machine validation of teammate-sync.

Bare-bones: launches a $5/mo Ubuntu 24.04 instance, opens SSH, downloads
the default keypair, prints the SSH command. NO bootstrap, NO rsync —
you SSH in and install everything yourself like a real new user would.

Idempotent: if the instance already exists, reuses it.

Run from project root:
    python3 cloud/launch-test-vm.py
"""
import configparser
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


CLOUD_DIR = Path(__file__).resolve().parent
SSH_KEY_PATH = CLOUD_DIR / "test-vm.pem"

REGION = "ap-south-1"           # Mumbai — closest to Delhi
AVAILABILITY_ZONE = "ap-south-1a"
INSTANCE_NAME = "teammate-sync-test"
BUNDLE_ID = "micro_3_1"          # 1GB RAM, ~$7/mo in ap-south-1. nano (512MB) is too small.
BLUEPRINT_ID = "ubuntu_24_04"
SSH_USER = "ubuntu"


def log(msg: str) -> None:
    print(f"[launch] {msg}", flush=True)


def get_client():
    cfg = configparser.ConfigParser()
    cfg.read(Path("~/.aws/credentials").expanduser())
    if "default" not in cfg:
        sys.exit("No [default] profile in ~/.aws/credentials")
    return boto3.client(
        "lightsail",
        region_name=REGION,
        aws_access_key_id=cfg["default"]["aws_access_key_id"],
        aws_secret_access_key=cfg["default"]["aws_secret_access_key"],
    )


def ensure_ssh_key(client) -> None:
    """Download the default SSH keypair for this region if we don't have it."""
    if SSH_KEY_PATH.exists():
        log(f"using existing SSH key at {SSH_KEY_PATH}")
        return
    log(f"downloading default SSH keypair for {REGION}...")
    resp = client.download_default_key_pair()
    SSH_KEY_PATH.write_text(resp["privateKeyBase64"])
    SSH_KEY_PATH.chmod(0o600)
    log(f"saved to {SSH_KEY_PATH} (mode 0600)")


def get_existing_instance(client):
    try:
        return client.get_instance(instanceName=INSTANCE_NAME)["instance"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            return None
        raise


def launch_instance(client) -> None:
    log(f"launching {INSTANCE_NAME} ({BUNDLE_ID}, {BLUEPRINT_ID}) in {REGION}...")
    client.create_instances(
        instanceNames=[INSTANCE_NAME],
        availabilityZone=AVAILABILITY_ZONE,
        blueprintId=BLUEPRINT_ID,
        bundleId=BUNDLE_ID,
    )


def wait_for_running(client) -> dict:
    log("waiting for instance to reach 'running' state...")
    for _ in range(60):
        inst = client.get_instance(instanceName=INSTANCE_NAME)["instance"]
        state = inst["state"]["name"]
        if state == "running":
            log(f"  running. public IP: {inst['publicIpAddress']}")
            return inst
        print(f"  state={state}, waiting...", flush=True)
        time.sleep(5)
    sys.exit("timeout waiting for instance")


def wait_for_ssh(ip: str) -> None:
    log("waiting for SSH to accept connections (Lightsail takes ~30-60s past 'running')...")
    for i in range(40):
        result = subprocess.run(
            ["ssh", "-i", str(SSH_KEY_PATH),
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "LogLevel=ERROR",
             "-o", "ConnectTimeout=5",
             f"{SSH_USER}@{ip}",
             "echo ssh-ok"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and "ssh-ok" in result.stdout:
            log("  SSH is up.")
            return
        time.sleep(5)
    sys.exit("timeout waiting for SSH")


def main() -> None:
    client = get_client()
    ensure_ssh_key(client)
    inst = get_existing_instance(client)
    if inst:
        log(f"reusing existing instance {INSTANCE_NAME} (state={inst['state']['name']})")
    else:
        launch_instance(client)
    inst = wait_for_running(client)
    ip = inst["publicIpAddress"]
    wait_for_ssh(ip)

    print()
    print("=" * 64)
    print("VM READY")
    print("=" * 64)
    print(f"  Name:       {INSTANCE_NAME}")
    print(f"  Region:     {REGION}")
    print(f"  Public IP:  {ip}")
    print(f"  SSH user:   {SSH_USER}")
    print(f"  Key:        {SSH_KEY_PATH}")
    print()
    print("SSH in:")
    print(f"  ssh -i {SSH_KEY_PATH} {SSH_USER}@{ip}")
    print()
    print("To destroy when done:")
    print(f"  python3 cloud/destroy-test-vm.py")
    print()


if __name__ == "__main__":
    main()
