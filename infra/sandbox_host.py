"""Create and manage the EC2 "sandbox host" that runs RepoLens sandboxes.

The sandbox host is one EC2 virtual machine with Docker installed. Each repository later runs in
its own Docker container on it. Everything this script creates is tagged ``project=repolens`` so
it is easy to find in the AWS Console and in the bill.

Usage (from the repository root, with the AWS CLI profile set up):

    python infra/sandbox_host.py status          # free: what exists and what it costs
    python infra/sandbox_host.py key             # free: create the SSH key pair
    python infra/sandbox_host.py firewall        # free: create/refresh the security group
    python infra/sandbox_host.py up --dry-run    # free: check permissions, launch nothing
    python infra/sandbox_host.py up --yes        # PAID: launch the host
    python infra/sandbox_host.py start --yes     # PAID: start a stopped host
    python infra/sandbox_host.py stop            # stop the host (only the disk keeps costing)
    python infra/sandbox_host.py autostop        # ~$0.10/month: stop the host when it sits idle
    python infra/sandbox_host.py ssh             # print the SSH command
    python infra/sandbox_host.py terminate --yes # delete the host and its disk

Commands that start paid resources refuse to run without ``--yes``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError

PROFILE = os.environ.get("REPOLENS_AWS_PROFILE", "repolens")
REGION = os.environ.get("REPOLENS_AWS_REGION", "us-east-1")

NAME = "repolens-sandbox-host"
KEY_NAME = "repolens-sandbox"
KEY_PATH = Path.home() / ".ssh" / f"{KEY_NAME}.pem"
SECURITY_GROUP = "repolens-sandbox"
TAGS = [{"Key": "project", "Value": "repolens"}]

INSTANCE_TYPE = "t3.large"  # 2 vCPU, 8 GiB RAM
DISK_GB = 60
AVAILABILITY_ZONE = "us-east-1a"  # t3.large is not offered in every zone (e.g. us-east-1e)
JUPYTER_PORTS = (8800, 8899)  # one port per running sandbox container

# Safety net: stop the host when average CPU stays below the threshold for this long. Reading a
# notebook without running cells looks idle too, so this is a backstop, not the main stop button.
AUTOSTOP_ALARM = f"{NAME}-autostop"
AUTOSTOP_CPU_PERCENT = 2.0
AUTOSTOP_MINUTES = 45

# us-east-1 list prices (USD), for the estimates this script prints.
PRICE_INSTANCE_HOUR = 0.0832
PRICE_PUBLIC_IPV4_HOUR = 0.005
PRICE_GP3_GB_MONTH = 0.08
PRICE_CPU_SURPLUS_VCPU_HOUR = 0.05  # t3 "unlimited" mode, only when CPU stays above baseline

# Runs once, as root, on the first boot (EC2 "user data", executed by cloud-init).
USER_DATA = """#!/bin/bash
set -eux
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user
touch /var/lib/repolens-host-ready
"""


def ec2():
    return boto3.Session(profile_name=PROFILE, region_name=REGION).client("ec2")


def cloudwatch():
    return boto3.Session(profile_name=PROFILE, region_name=REGION).client("cloudwatch")


def my_ip() -> str:
    return requests.get("https://checkip.amazonaws.com", timeout=10).text.strip()


def hourly_cost() -> float:
    return PRICE_INSTANCE_HOUR + PRICE_PUBLIC_IPV4_HOUR


def disk_monthly_cost() -> float:
    return DISK_GB * PRICE_GP3_GB_MONTH


def find_instance(client) -> dict | None:
    """The host, if it exists and is not terminated."""
    resp = client.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ])
    instances = [i for r in resp["Reservations"] for i in r["Instances"]]
    return instances[0] if instances else None


def find_security_group(client) -> dict | None:
    resp = client.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [SECURITY_GROUP]}])
    return resp["SecurityGroups"][0] if resp["SecurityGroups"] else None


def latest_amazon_linux(client) -> dict:
    """Newest Amazon Linux 2023 image (x86_64, standard, not minimal)."""
    resp = client.describe_images(Owners=["amazon"], Filters=[
        {"Name": "name", "Values": ["al2023-ami-2023.*-kernel-*-x86_64"]},
        {"Name": "state", "Values": ["available"]},
    ])
    return max(resp["Images"], key=lambda img: img["CreationDate"])


def default_subnet(client) -> str:
    resp = client.describe_subnets(Filters=[
        {"Name": "default-for-az", "Values": ["true"]},
        {"Name": "availability-zone", "Values": [AVAILABILITY_ZONE]},
    ])
    return resp["Subnets"][0]["SubnetId"]


# --- commands --------------------------------------------------------------------------------

def cmd_status(_args) -> None:
    client = ec2()
    print(f"Profile {PROFILE}, region {REGION}")
    print(f"SSH key pair    : {'yes' if key_exists(client) else 'no'}"
          f"{'' if KEY_PATH.exists() else ' (no local .pem)'}")
    sg = find_security_group(client)
    print(f"Security group  : {sg['GroupId'] if sg else 'no'}")
    inst = find_instance(client)
    if not inst:
        print("Sandbox host    : not created")
        return
    state = inst["State"]["Name"]
    print(f"Sandbox host    : {inst['InstanceId']} ({inst['InstanceType']}) — {state}")
    if ip := inst.get("PublicIpAddress"):
        print(f"Public IP       : {ip}")
    if state == "running":
        up_h = (datetime.now(timezone.utc) - inst["LaunchTime"]).total_seconds() / 3600
        print(f"Running for     : {up_h:.1f} h (≈ ${up_h * hourly_cost():.2f} since last start)")
    print(f"Costs           : ≈ ${hourly_cost():.3f}/hour while running; "
          f"disk ≈ ${disk_monthly_cost():.2f}/month even while stopped")


def key_exists(client) -> bool:
    try:
        client.describe_key_pairs(KeyNames=[KEY_NAME])
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
            return False
        raise


def cmd_key(_args) -> None:
    client = ec2()
    if key_exists(client):
        print(f"Key pair {KEY_NAME} already exists in AWS"
              f"{'' if KEY_PATH.exists() else f', but {KEY_PATH} is missing'}.")
        return
    resp = client.create_key_pair(KeyName=KEY_NAME, KeyType="ed25519", KeyFormat="pem",
                                  TagSpecifications=[{"ResourceType": "key-pair", "Tags": TAGS}])
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(resp["KeyMaterial"], encoding="ascii")
    restrict_to_current_user(KEY_PATH)
    print(f"Created key pair {KEY_NAME}; private key saved to {KEY_PATH}")


def restrict_to_current_user(path: Path) -> None:
    """OpenSSH refuses private keys that other users can read."""
    if os.name == "nt":
        user = os.environ["USERNAME"]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:R"],
                       check=True, capture_output=True)
    else:
        path.chmod(0o400)


def cmd_firewall(_args) -> None:
    client = ec2()
    cidr = f"{my_ip()}/32"
    sg = find_security_group(client)
    if not sg:
        vpc = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
        group_id = client.create_security_group(
            GroupName=SECURITY_GROUP, VpcId=vpc["VpcId"],
            Description="RepoLens sandbox host - SSH and Jupyter from owner IP only",
            TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
        print(f"Created security group {group_id}")
    else:
        group_id = sg["GroupId"]
        if sg["IpPermissions"]:  # drop the rules for the old IP
            client.revoke_security_group_ingress(GroupId=group_id, IpPermissions=sg["IpPermissions"])
    client.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
         "IpRanges": [{"CidrIp": cidr, "Description": "SSH"}]},
        {"IpProtocol": "tcp", "FromPort": JUPYTER_PORTS[0], "ToPort": JUPYTER_PORTS[1],
         "IpRanges": [{"CidrIp": cidr, "Description": "Jupyter sandboxes"}]},
    ])
    print(f"Inbound SSH (22) and Jupyter ({JUPYTER_PORTS[0]}-{JUPYTER_PORTS[1]}) "
          f"allowed only from {cidr}")


def cmd_up(args) -> None:
    client = ec2()
    if inst := find_instance(client):
        sys.exit(f"Host already exists: {inst['InstanceId']} ({inst['State']['Name']}). "
                 "Use start/stop instead.")
    sg = find_security_group(client)
    if not sg or not key_exists(client):
        sys.exit("Run `key` and `firewall` first.")
    if not (args.yes or args.dry_run):
        sys.exit(f"This launches a paid {INSTANCE_TYPE} (≈ ${hourly_cost():.3f}/hour while running, "
                 f"disk ≈ ${disk_monthly_cost():.2f}/month). Re-run with --yes, or --dry-run.")
    image = latest_amazon_linux(client)
    try:
        resp = client.run_instances(
            DryRun=args.dry_run,
            ImageId=image["ImageId"], InstanceType=INSTANCE_TYPE, MinCount=1, MaxCount=1,
            KeyName=KEY_NAME, SecurityGroupIds=[sg["GroupId"]], SubnetId=default_subnet(client),
            UserData=USER_DATA,
            BlockDeviceMappings=[{"DeviceName": image["RootDeviceName"], "Ebs": {
                "VolumeSize": DISK_GB, "VolumeType": "gp3", "DeleteOnTermination": True,
                "Encrypted": True}}],
            MetadataOptions={"HttpTokens": "required"},  # IMDSv2 only
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": TAGS + [{"Key": "Name", "Value": NAME}]},
                {"ResourceType": "volume", "Tags": TAGS + [{"Key": "Name", "Value": NAME}]},
            ],
        )
    except ClientError as exc:
        if args.dry_run and exc.response["Error"]["Code"] == "DryRunOperation":
            print(f"Dry run OK: permissions are in place to launch {INSTANCE_TYPE} from "
                  f"{image['Name']} ({image['ImageId']}). Nothing was created.")
            return
        raise
    inst = resp["Instances"][0]
    print(f"Launching {inst['InstanceId']} ... (Docker installs on first boot, ~2 minutes)")
    client.get_waiter("instance_running").wait(InstanceIds=[inst["InstanceId"]])
    cmd_autostop(args)  # the alarm watches one instance ID, so every new host needs its own
    cmd_status(args)


def cmd_start(args) -> None:
    client = ec2()
    inst = find_instance(client) or sys.exit("No host. Run `up` first.")
    if not args.yes:
        sys.exit(f"Starting the host costs ≈ ${hourly_cost():.3f}/hour. Re-run with --yes.")
    client.start_instances(InstanceIds=[inst["InstanceId"]])
    client.get_waiter("instance_running").wait(InstanceIds=[inst["InstanceId"]])
    cmd_status(args)


def cmd_stop(args) -> None:
    client = ec2()
    inst = find_instance(client) or sys.exit("No host.")
    client.stop_instances(InstanceIds=[inst["InstanceId"]])
    print(f"Stopping {inst['InstanceId']} ...")
    client.get_waiter("instance_stopped").wait(InstanceIds=[inst["InstanceId"]])
    print(f"Stopped. Only the disk keeps costing (≈ ${disk_monthly_cost():.2f}/month).")


def cmd_terminate(args) -> None:
    client = ec2()
    inst = find_instance(client) or sys.exit("No host.")
    if not args.yes:
        sys.exit("This permanently deletes the host and its disk. Re-run with --yes.")
    client.terminate_instances(InstanceIds=[inst["InstanceId"]])
    print(f"Terminating {inst['InstanceId']}; its disk is deleted with it.")


def cmd_autostop(_args) -> None:
    inst = find_instance(ec2()) or sys.exit("No host. Run `up` first.")
    period = 300  # basic EC2 monitoring reports CPU every 5 minutes
    cloudwatch().put_metric_alarm(
        AlarmName=AUTOSTOP_ALARM,
        AlarmDescription=f"Stop {NAME} after {AUTOSTOP_MINUTES} min below "
                         f"{AUTOSTOP_CPU_PERCENT}% CPU",
        Namespace="AWS/EC2", MetricName="CPUUtilization", Statistic="Average",
        Dimensions=[{"Name": "InstanceId", "Value": inst["InstanceId"]}],
        Period=period, EvaluationPeriods=AUTOSTOP_MINUTES * 60 // period,
        Threshold=AUTOSTOP_CPU_PERCENT, ComparisonOperator="LessThanThreshold",
        TreatMissingData="missing",  # a stopped host sends no data; leave it alone
        AlarmActions=[f"arn:aws:automate:{REGION}:ec2:stop"],
        Tags=TAGS,
    )
    print(f"Alarm {AUTOSTOP_ALARM}: stops {inst['InstanceId']} after {AUTOSTOP_MINUTES} min "
          f"below {AUTOSTOP_CPU_PERCENT}% average CPU.")


def cmd_ssh(_args) -> None:
    inst = find_instance(ec2())
    if not inst or not inst.get("PublicIpAddress"):
        sys.exit("The host is not running.")
    print(f'ssh -i "{KEY_PATH}" ec2-user@{inst["PublicIpAddress"]}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in [("status", cmd_status), ("key", cmd_key), ("firewall", cmd_firewall),
                       ("up", cmd_up), ("start", cmd_start), ("stop", cmd_stop),
                       ("terminate", cmd_terminate), ("autostop", cmd_autostop),
                       ("ssh", cmd_ssh)]:
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        if name in ("up", "start", "terminate"):
            p.add_argument("--yes", action="store_true", help="confirm a paid or destructive action")
        if name == "up":
            p.add_argument("--dry-run", action="store_true", help="check permissions only")
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
