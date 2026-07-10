#!/bin/bash
#
# Setup SSL certificate for Inverter Control web interface
# Creates self-signed certificate and installs it to macOS keychain
#

set -e

CERBO_IP="${1:-Cerbo}"
CERT_DIR="$HOME/.inverter-control-ssl"
CERT_NAME="inverter-control"
DAYS_VALID=3650  # 10 years
SEPARATOR="=============================================="

echo "$SEPARATOR"
echo "  SSL Certificate Setup for Inverter Control"
echo "$SEPARATOR"
echo ""
echo "Cerbo IP: $CERBO_IP"
echo "Cert dir: $CERT_DIR"
echo ""

# Create directory
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

# Create OpenSSL config with SAN
cat > "$CERT_NAME.cnf" << EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
C = US
ST = California
L = San Francisco
O = Home
OU = Inverter Control
CN = $CERBO_IP

[v3_req]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
subjectAltName = @alt_names

[alt_names]
IP.1 = $CERBO_IP
DNS.1 = cerbo
DNS.2 = cerbo.local
EOF

echo ">>> Generating private key and certificate..."
openssl req -x509 -nodes -days $DAYS_VALID \
    -newkey rsa:2048 \
    -keyout "$CERT_NAME.key" \
    -out "$CERT_NAME.crt" \
    -config "$CERT_NAME.cnf"

echo ">>> Certificate created:"
echo "    Key:  $CERT_DIR/$CERT_NAME.key"
echo "    Cert: $CERT_DIR/$CERT_NAME.crt"
echo ""

# Create combined PEM for Python
cat "$CERT_NAME.crt" "$CERT_NAME.key" > "$CERT_NAME.pem"
echo ">>> Combined PEM: $CERT_DIR/$CERT_NAME.pem"
echo ""

# Display certificate info
echo ">>> Certificate details:"
openssl x509 -in "$CERT_NAME.crt" -noout -subject -dates
echo ""

# Add to macOS keychain
echo ">>> Adding certificate to macOS System keychain..."
echo "    (You may be prompted for your password)"

# Remove old certificate if exists
sudo security delete-certificate -c "$CERBO_IP" /Library/Keychains/System.keychain 2>/dev/null || true

# Add new certificate and trust it
sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain \
    "$CERT_NAME.crt"

echo ">>> Certificate added to System keychain and marked as trusted"
echo ""

# Copy to Cerbo
echo ">>> Copying certificate to Cerbo..."
scp "$CERT_NAME.crt" "$CERT_NAME.key" "r:/data/inverter_control/"

# Update config to enable SSL
echo ">>> Enabling SSL in config..."
ssh r "cd /data/inverter_control && \
    if ! grep -q 'SSL_ENABLED' config.py; then \
        echo '' >> config.py; \
        echo '# SSL Configuration' >> config.py; \
        echo 'SSL_ENABLED = True' >> config.py; \
        echo 'SSL_CERT = \"/data/inverter_control/inverter-control.crt\"' >> config.py; \
        echo 'SSL_KEY = \"/data/inverter_control/inverter-control.key\"' >> config.py; \
    else \
        sed -i 's/SSL_ENABLED = False/SSL_ENABLED = True/' config.py; \
    fi"

echo ""
echo "$SEPARATOR"
echo "  SSL Setup Complete!"
echo "$SEPARATOR"
echo ""
echo "Certificate files:"
echo "  Local:  $CERT_DIR/$CERT_NAME.crt"
echo "  Cerbo:  /data/inverter_control/$CERT_NAME.crt"
echo ""
echo "Access web interface at:"
echo "  https://$CERBO_IP:8080"
echo ""
echo "Note: Restart the inverter-control service:"
echo "  ssh r 'svc -t /service/inverter-control'"
echo ""
