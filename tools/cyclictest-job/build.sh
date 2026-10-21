#!/usr/bin/env bash
# Build cyclictest-job on the Jetson: rt-tests 2.2 (the version of the Ubuntu package
# installed there) plus cyclictest-job.patch and h3track.h. Run from a directory that contains
# rt-tests-2.2.tar.xz, cyclictest-job.patch, h3track.h, h3track_test.c and this script.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
echo "48346ab2b5bf2ffbdb1ed0d4540e9e8e22f6665e6bb3d59c22614f9722b13a91  rt-tests-2.2.tar.xz" | sha256sum -c
rm -rf rt-tests-2.2
tar -xJf rt-tests-2.2.tar.xz
patch -d rt-tests-2.2 -p1 < cyclictest-job.patch
cp h3track.h rt-tests-2.2/src/cyclictest/
make -C rt-tests-2.2 cyclictest
cp rt-tests-2.2/cyclictest cyclictest-job
# Same algorithm outside cyclictest: tracking check and input hash on this machine
gcc -O2 -Wall -o h3track_test h3track_test.c
./h3track_test
sha256sum cyclictest-job
