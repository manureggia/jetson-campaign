#!/usr/bin/env bash
# Build cyclictest-job on the Jetson: rt-tests 2.2 (the version of the Ubuntu package
# installed there) plus cyclictest-job.patch. Run from a directory that contains
# rt-tests-2.2.tar.xz, cyclictest-job.patch and this script.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
echo "48346ab2b5bf2ffbdb1ed0d4540e9e8e22f6665e6bb3d59c22614f9722b13a91  rt-tests-2.2.tar.xz" | sha256sum -c
rm -rf rt-tests-2.2
tar -xJf rt-tests-2.2.tar.xz
patch -d rt-tests-2.2 -p1 < cyclictest-job.patch
make -C rt-tests-2.2 cyclictest
cp rt-tests-2.2/cyclictest cyclictest-job
sha256sum cyclictest-job
