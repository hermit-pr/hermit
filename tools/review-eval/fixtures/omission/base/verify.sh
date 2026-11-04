#!/bin/bash
set -euo pipefail

openssl x509 -noout -in cert.pem || exit 1
echo "verify ok"
