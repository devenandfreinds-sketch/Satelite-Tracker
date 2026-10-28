# Railway deploy: serve the static dashboard and globe in site/ with Caddy.
# The pipeline (tracker/, data/) is not shipped; site/data is the committed public snapshot.
FROM caddy:2.8-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY site /srv
