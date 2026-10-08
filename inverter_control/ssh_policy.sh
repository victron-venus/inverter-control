#!/bin/sh
# Shared by the operator SSH helpers; no settings or trust stores are modified.
ssh_with_key_policy() {
    if [ "$#" -lt 1 ]; then
        printf '%s\n' 'SSH destination is required' >&2
        return 2
    fi
    # Include the command in Match evaluation; -G neither connects nor runs it.
    ssh_policy_config=$(ssh -G "$@") || return "$?"
    ssh_policy_rsa=$(printf '%s\n' "$ssh_policy_config" | awk '
        tolower($1) == "requiredrsasize" {
            count++
            if (NF != 2 || count > 1) invalid = 1
            value = $2
        }
        END {
            if (count != 1 || invalid) exit 1
            print value
        }
    ') || {
        printf '%s\n' 'SSH requires OpenSSH 9.1+ with a valid RequiredRSASize setting' >&2
        return 2
    }
    case "$ssh_policy_rsa" in
        ''|*[!0-9]*)
            printf '%s\n' 'Invalid SSH RequiredRSASize setting' >&2
            return 2
            ;;
    esac
    # Bound the decimal conversion before shell arithmetic; never accept overflow.
    if [ "${#ssh_policy_rsa}" -gt 9 ]; then
        printf '%s\n' 'SSH RequiredRSASize setting is out of range' >&2
        return 2
    fi
    if [ "$ssh_policy_rsa" -lt 2048 ]; then
        ssh_policy_rsa=2048
    fi
    # A pre-existing multiplexed session may have authenticated weaker host keys.
    ssh -S none -o "RequiredRSASize=$ssh_policy_rsa" "$@"
}
