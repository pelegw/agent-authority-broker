#!/usr/bin/env bash
# Lock the EC2 security group so port 443 accepts traffic ONLY from Cloudflare's
# published IP ranges — the network-layer half of origin lockdown (the app also
# checks the secret header, and Caddy does mTLS). Run where the AWS CLI is
# configured with permissions to modify the security group.
#
#   SG_ID=sg-0abc123 deploy/update-security-group.sh
#
# Env vars:
#   SG_ID       (required)  the instance's security group id
#   SSH_CIDR    (optional)  your IP/32 to keep SSH (22) open to; skip to leave 22 as-is
set -euo pipefail

: "${SG_ID:?set SG_ID=sg-xxxx}"

echo "==> Fetching Cloudflare IP ranges"
mapfile -t V4 < <(curl -fsSL https://www.cloudflare.com/ips-v4)
mapfile -t V6 < <(curl -fsSL https://www.cloudflare.com/ips-v6)
echo "    ${#V4[@]} IPv4 + ${#V6[@]} IPv6 ranges"

authorize() {
  local cidr="$1" ver="$2"
  if [ "$ver" = v4 ]; then
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=$cidr,Description=cloudflare}]" \
      >/dev/null 2>&1 || echo "    (443 <- $cidr already present)"
  else
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,Ipv6Ranges=[{CidrIpv6=$cidr,Description=cloudflare}]" \
      >/dev/null 2>&1 || echo "    (443 <- $cidr already present)"
  fi
}

echo "==> Allowing 443 from Cloudflare only"
for c in "${V4[@]}"; do authorize "$c" v4; done
for c in "${V6[@]}"; do authorize "$c" v6; done

# Remove the world-open 443 rule if it exists (ignore error if it doesn't).
echo "==> Removing any 0.0.0.0/0 rule on 443"
aws ec2 revoke-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true

if [ -n "${SSH_CIDR:-}" ]; then
  echo "==> Ensuring SSH (22) is open to ${SSH_CIDR}"
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "$SSH_CIDR" >/dev/null 2>&1 || echo "    (already present)"
fi

echo
echo "Done. 443 now reachable only from Cloudflare. Re-run after Cloudflare"
echo "updates its ranges (rare). The origin IP is otherwise unreachable on 443."
