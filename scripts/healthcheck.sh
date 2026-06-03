#!/bin/sh
# Docker HEALTHCHECK script for the Context-Ring proxy.
# Exits 0 (healthy) if /healthz returns HTTP 200, else 1 (unhealthy).

set -e

STATUS=$(wget -qO- --spider --server-response http://localhost:8000/healthz 2>&1 \
    | awk '/HTTP\//{print $2}' | tail -1)

if [ "$STATUS" = "200" ]; then
    exit 0
else
    echo "Healthcheck failed — HTTP $STATUS"
    exit 1
fi
