#!/bin/sh
set -eu
apt-get update
apt-get install -y python3 python3-venv python3-pip git ca-certificates curl xz-utils software-properties-common
add-apt-repository -y ppa:git-core/ppa
apt-get update
apt-get install -y git
git --version
