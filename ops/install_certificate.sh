#!/bin/sh
set -eu
# Native Certbot has isolated configuration; the existing proxy only gets PEMs.
destination=/var/lib/docker/volumes/stanki-site_certbot_certs/_data/live/ozon-ip
mkdir -p "$destination"
install -m 644 /etc/ozon-analytics/letsencrypt/live/185.255.132.160/fullchain.pem "$destination/fullchain.pem"
install -m 600 /etc/ozon-analytics/letsencrypt/live/185.255.132.160/privkey.pem "$destination/privkey.pem"
docker exec stanki-site-nginx-1 nginx -t
docker exec stanki-site-nginx-1 nginx -s reload
