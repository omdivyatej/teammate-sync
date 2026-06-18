#!/usr/bin/env python3
"""Destroy the teammate-sync test VM. Run when done with two-machine testing."""
import configparser
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


REGION = "ap-south-1"
INSTANCE_NAME = "teammate-sync-test"


def main() -> None:
    cfg = configparser.ConfigParser()
    cfg.read(Path("~/.aws/credentials").expanduser())
    client = boto3.client(
        "lightsail",
        region_name=REGION,
        aws_access_key_id=cfg["default"]["aws_access_key_id"],
        aws_secret_access_key=cfg["default"]["aws_secret_access_key"],
    )
    try:
        client.delete_instance(instanceName=INSTANCE_NAME)
        print(f"deleted {INSTANCE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            print(f"{INSTANCE_NAME} doesn't exist (already gone)")
        else:
            raise


if __name__ == "__main__":
    main()
