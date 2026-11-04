#!/bin/bash
set -euo pipefail

openssl verify cert.pem || exit 1
echo "check ok"
