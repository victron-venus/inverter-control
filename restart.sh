#!/bin/sh
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit
. "$SCRIPT_DIR/inverter_control/ssh_policy.sh"
ssh_with_key_policy Cerbo 'svc -t /service/inverter-control'
