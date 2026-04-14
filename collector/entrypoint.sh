#!/bin/sh
if [ -f /etc/otelcol-contrib/custom_config.yaml ]; then
  exec /otelcolcontrib --config=/etc/otelcol-contrib/custom_config.yaml "$@"
else
  exec /otelcolcontrib --config=/etc/otelcol-contrib/config.yaml "$@"
fi
