#!/bin/bash
# Block until archive.org will talk to us again.
#
# Six concurrent indexers plus a scan streaming whole files got this IP
# rate-limited off archive.org and its ia*.us.archive.org data nodes, while
# every other host stayed reachable. Starting an indexer into a block just
# burns retries and prolongs it, so each one now waits for a plain 200 first
# and probes gently -- once every five minutes, one request.
for i in $(seq 1 288); do            # up to 24h
  C=$(curl -sS -o /dev/null -m 20 -w "%{http_code}" https://archive.org/metadata/nes 2>/dev/null)
  if [ "$C" = "200" ]; then
    echo "archive.org reachable (probe $i)"; exit 0
  fi
  sleep 300
done
echo "archive.org still refusing after 24h"; exit 1
