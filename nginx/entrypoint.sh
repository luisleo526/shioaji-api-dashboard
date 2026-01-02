#!/bin/sh
set -e

# Generate IP whitelist configuration from ALLOWED_IPS environment variable
# ALLOWED_IPS should be comma-separated, e.g., "192.168.1.1,10.0.0.0/8,172.16.0.0/12"

ALLOWLIST_FILE="/etc/nginx/conf.d/allowlist.conf"

echo "=== NGINX IP Whitelist Generator ==="

if [ -z "$ALLOWED_IPS" ] || [ "$ALLOWED_IPS" = "" ]; then
    echo "ALLOWED_IPS not set or empty - allowing all IPs"
    cat > "$ALLOWLIST_FILE" << 'EOF'
# No IP whitelist configured - allow all
geo $ip_whitelist {
    default 1;
}
EOF
else
    echo "Generating whitelist from: $ALLOWED_IPS"
    
    # Start the geo block
    cat > "$ALLOWLIST_FILE" << 'EOF'
# IP Whitelist - auto-generated from ALLOWED_IPS environment variable
# Only whitelisted IPs can access the API
geo $ip_whitelist {
    default 0;
EOF
    
    # Split by comma and add each IP
    echo "$ALLOWED_IPS" | tr ',' '\n' | while read -r ip; do
        # Trim whitespace
        ip=$(echo "$ip" | xargs)
        if [ -n "$ip" ]; then
            echo "  - Allowing: $ip"
            echo "    $ip 1;" >> "$ALLOWLIST_FILE"
        fi
    done
    
    # Close the geo block
    echo "}" >> "$ALLOWLIST_FILE"
fi

echo ""
echo "Generated allowlist config:"
cat "$ALLOWLIST_FILE"
echo ""
echo "=== Starting NGINX ==="

# Execute the original nginx entrypoint
exec /docker-entrypoint.sh "$@"

