#!/data/data/com.termux/files/usr/bin/bash
# One-shot bootstrap: run from Termux with gh already authenticated.
# Provisions the Lightsail box and sets the four deploy secrets on GitHub.
# Idempotent: safe to re-run.
set -euo pipefail

HOST=13.127.164.72
USER=ubuntu
REPO=Sammisam8888/day-trader-skill
PEM_SRC=/storage/emulated/0/Download/trading-automation-server-ssh-key.pem
PEM=$HOME/.ssh/lightsail.pem
DEPLOY=$HOME/.ssh/indstocks_deploy

echo "==> deps"
pkg install -y openssh >/dev/null 2>&1 || true
command -v gh >/dev/null || { echo "gh missing"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "run: gh auth login"; exit 1; }

echo "==> instance key"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
# /storage/emulated/0 is FAT: no permission bits, so ssh rejects the key there.
[ -f "$PEM_SRC" ] || { echo "not found: $PEM_SRC"; exit 1; }
cp "$PEM_SRC" "$PEM" && chmod 600 "$PEM"

echo "==> host key"
ssh-keyscan -t ed25519 "$HOST" 2>/dev/null > "$HOME/.ssh/known_hosts_indstocks"
[ -s "$HOME/.ssh/known_hosts_indstocks" ] || { echo "no response from $HOST:22"; exit 1; }

echo "==> deploy keypair"
[ -f "$DEPLOY" ] || ssh-keygen -q -t ed25519 -N '' -f "$DEPLOY" -C "github-actions@$REPO"

SSH="ssh -i $PEM -o UserKnownHostsFile=$HOME/.ssh/known_hosts_indstocks -o BatchMode=yes"

echo "==> provisioning server"
$SSH "$USER@$HOST" bash -seu <<REMOTE
# --- swap (512MB box; builds OOM without it) ---
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap -q /swapfile && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# --- clock: TOTP fails on drift over ~1 minute ---
sudo timedatectl set-ntp true

# --- packages ---
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git fail2ban

# --- ssh hardening: port stays open (mobile IPs rotate), keys only ---
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl enable --now fail2ban >/dev/null 2>&1 || true
sudo systemctl restart ssh || sudo systemctl restart sshd

# --- deploy key for the CI runner ---
mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qF "$(cat $DEPLOY.pub)" ~/.ssh/authorized_keys 2>/dev/null || echo "$(cat $DEPLOY.pub)" >> ~/.ssh/authorized_keys

# --- repo ---
sudo mkdir -p /opt/indstocks && sudo chown $USER:$USER /opt/indstocks
cd /opt/indstocks
[ -d day-trader-skill ] || git clone -q https://github.com/$REPO.git
cd day-trader-skill && git fetch -q origin main && git reset -q --hard origin/main
python3 -m unittest discover -s tests 2>&1 | tail -2

# --- let CI restart the service without a password, nothing else ---
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart indstocks-mcp" | sudo tee /etc/sudoers.d/indstocks >/dev/null
sudo chmod 440 /etc/sudoers.d/indstocks

# --- credential file placeholder, root-only ---
[ -f /etc/indstocks.env ] || { echo '# filled in by: sudo bash /opt/indstocks/day-trader-skill/scripts/set-creds.sh' | sudo tee /etc/indstocks.env >/dev/null; sudo chmod 600 /etc/indstocks.env; }
echo "server ready"
REMOTE

echo "==> github secrets"
gh secret set DEPLOY_HOST        --repo "$REPO" --body "$HOST"
gh secret set DEPLOY_USER        --repo "$REPO" --body "$USER"
gh secret set DEPLOY_SSH_KEY     --repo "$REPO" < "$DEPLOY"
gh secret set DEPLOY_KNOWN_HOSTS --repo "$REPO" < "$HOME/.ssh/known_hosts_indstocks"
gh secret list --repo "$REPO"

echo "==> cleanup"
rm -f "$DEPLOY"          # private half now lives only in the GitHub secret
echo
echo "Done. Deploy key removed from this device; public half is on the server."
echo "Remaining (web only, no API exists for these):"
echo "  1. TOTP setup on indstocks.com  2. whitelist $HOST  3. set credentials on the server"
