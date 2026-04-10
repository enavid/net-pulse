# SSH Key Authentication Guide

This guide explains how to set up secure, passwordless SSH authentication between
NetPulse and your agent servers. Each agent server gets a dedicated system user
with minimal privileges — only enough to run `curl` downloads. If the private key
is ever compromised, the attacker gains no useful shell access.

---

## Overview

NetPulse uses an Ed25519 key pair instead of passwords:

| Item | Location |
|------|----------|
| Private key | Stored in NetPulse DB (coordinator only) |
| Public key | Installed on each agent server for a restricted user |

---

## Step 1) Generate the key pair (on coordinator)

```bash
ssh-keygen -t ed25519 -C "netpulse-agent" -f ~/.ssh/netpulse_agent -N ""
```

Two files are created:
- `~/.ssh/netpulse_agent` — **private key** (paste this into NetPulse)
- `~/.ssh/netpulse_agent.pub` — **public key** (installed on each agent)

---

## Step 2) Install the wrapper script on each agent

**Why a wrapper?** Setting `command="curl"` in `authorized_keys` runs the literal
string `"curl"` with no arguments — not the command NetPulse sends. The fix is a
wrapper that reads `$SSH_ORIGINAL_COMMAND`, validates it, and executes it.

Run this **on each agent server** (or copy `scripts/netpulse-wrapper.sh` from the
project):

```bash
sudo tee /usr/local/bin/netpulse-wrapper << 'EOF'
#!/bin/bash
set -euo pipefail
CMD="${SSH_ORIGINAL_COMMAND:-}"
if [[ -z "$CMD" ]]; then
    echo "Error: no command provided" >&2; exit 1
fi
if ! echo "$CMD" | grep -qE '^curl[[:space:]]'; then
    echo "Error: only curl commands are allowed" >&2; exit 1
fi
if echo "$CMD" | grep -qE '[|;&`$()<>{}]'; then
    echo "Error: illegal characters in command" >&2; exit 1
fi
exec $CMD
EOF
sudo chmod 755 /usr/local/bin/netpulse-wrapper
```

---

## Step 3) Create the restricted user and install the public key

Run these commands **from your coordinator machine**. Replace the variables at the
top for each agent server.

```bash
# ── Configure these for each agent ──────────────────────────────────────────
AGENT_HOST="your-agent-ip"
AGENT_PORT=22
AGENT_ADMIN="root"   # an existing user with sudo access on the agent

# ── Create the netpulse user ─────────────────────────────────────────────────
ssh -p "$AGENT_PORT" "$AGENT_ADMIN@$AGENT_HOST" \
  "useradd --system --create-home --shell /bin/bash netpulse && \
   mkdir -p /home/netpulse/.ssh && \
   chmod 700 /home/netpulse/.ssh"

# ── Install the public key with wrapper restriction ──────────────────────────
PUBKEY=$(cat ~/.ssh/netpulse_agent.pub)
ssh -p "$AGENT_PORT" "$AGENT_ADMIN@$AGENT_HOST" \
  "printf '%s\n' \
   'command=\"/usr/local/bin/netpulse-wrapper\",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding $PUBKEY' \
   > /home/netpulse/.ssh/authorized_keys && \
   chmod 600 /home/netpulse/.ssh/authorized_keys && \
   chown -R netpulse:netpulse /home/netpulse/.ssh"
```

> **Note:** This replaces the separate `ssh-copy-id` step — the public key is
> written directly with the `command=` restriction already in place.

---

## Step 4) Verify the connection

```bash
# Should print curl version output
ssh -i ~/.ssh/netpulse_agent -p "$AGENT_PORT" netpulse@"$AGENT_HOST" \
  "curl --version"

# Should print "Error: only curl commands are allowed" and exit 1
ssh -i ~/.ssh/netpulse_agent -p "$AGENT_PORT" netpulse@"$AGENT_HOST" \
  "ls -la"

# Full download test (replace URL with your source)
ssh -i ~/.ssh/netpulse_agent -p "$AGENT_PORT" netpulse@"$AGENT_HOST" \
  "curl -s -o /dev/null https://your-source-url && echo OK"
```

---

## Step 5) Add the key and agent to NetPulse

1. Go to **Agents → SSH Keys → Add Key**
2. Name it (e.g. `netpulse-agent-key`) and paste the content of
   `~/.ssh/netpulse_agent` (include the `-----BEGIN` and `-----END` lines)
3. Go to **Agents → Add Agent**
4. Set **Authentication** to **SSH Key**
5. Select the named key from the dropdown
6. Set **Username** to `netpulse`

---

## ProxyJump support

If an agent is behind a jump host (equivalent to `ProxyJump` in `~/.ssh/config`):

1. Open the agent modal and expand the **ProxyJump** section
2. Fill in Jump Host, Port, and User
3. Optionally select a named key for the jump host

Equivalent SSH config:
```
Host company
    HostName localhost
    Port 44
    User netpulse
    ProxyJump elk
```

---

## Security hardening checklist

```bash
# Prevent the authorized_keys file from being modified
sudo chattr +i /home/netpulse/.ssh/authorized_keys

# Confirm the user cannot get an interactive shell
su - netpulse         # should fail

# Confirm no sudo access
sudo -l -U netpulse   # should show "not allowed"

# Protect the NetPulse database on the coordinator
chmod 600 logs/netpulse.db
```

---

## Rotating keys

1. Generate a new key pair (Step 1)
2. Remove the old public key from each agent's `authorized_keys`
3. Run Step 3 again with the new public key
4. In NetPulse: delete the old key under **Agents → SSH Keys**, add the new one
5. Edit each agent to select the new key
6. Delete the old key files from the coordinator