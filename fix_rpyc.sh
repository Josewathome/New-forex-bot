#!/bin/bash
# fix_rpyc.sh — intentionally a no-op.
#
# RPyC startup has been moved into override_start.sh (step 7) which runs
# as the abc user (UID 911) who owns /config/.wine.  Running wine here
# (as root) causes:  wine: '/config/.wine' is not owned by you
#
# This script is kept mounted so the linuxserver custom-init system
# doesn't error on a missing file.
exit 0