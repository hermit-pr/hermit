#!/bin/bash
set -uo pipefail

openssl verify cert.pem
openssl x509 -noout -in cert.pem
echo "checks done"
