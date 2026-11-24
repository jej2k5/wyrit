#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

command -v docker >/dev/null 2>&1 || { echo 'Docker CLI is required' >&2; exit 1; }
docker info --format '{{.OSType}}' | grep -qx linux || {
    echo 'A reachable Linux Docker daemon is required; container probes did not run.' >&2
    exit 1
}

image=wyrit-linux-verification
docker build -f examples/container/Dockerfile -t "$image" .
printf 'Running with Docker default security settings at %s.\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
status=0
docker run --rm "$image" sh -c '
    status=0
    scripts/verify-linux.sh || status=$?
    [ "$status" -ne 0 ] || exit 0

    printf "\nContainer security diagnostics\n"
    grep -E "^(Seccomp|Seccomp_filters|NoNewPrivs):" /proc/self/status || :
    for path in /proc/self/attr/apparmor/current /proc/self/attr/current; do
        if profile=$(cat "$path" 2>/dev/null); then
            printf "AppArmor/LSM profile: %s\n" "$profile"
            break
        fi
    done
    for path in /proc/sys/user/max_user_namespaces \
        /proc/sys/kernel/unprivileged_userns_clone \
        /proc/sys/kernel/apparmor_restrict_unprivileged_userns; do
        if value=$(cat "$path" 2>/dev/null); then
            printf "%s = %s\n" "$path" "$value"
            case "$path=$value" in
                */max_user_namespaces=0|*/unprivileged_userns_clone=0)
                    printf "User namespace creation blocked by %s\n" "$path"
                    ;;
            esac
        fi
    done
    printf "%s\n" "Testing a bare unprivileged user namespace (diagnostic only):"
    unshare --user -- /usr/bin/true && printf "%s\n" "Bare user namespace creation succeeded." || :
    exit "$status"
' || status=$?
if [ "$status" -ne 0 ]; then
    printf '%s\n' 'Docker daemon security options:' >&2
    docker info --format '{{json .SecurityOptions}}' >&2 || :
    diagnostic() {
        printf '\nDiagnostic only (%s). Running the trusted readiness sentinel.\n' "$1"
        if docker run --rm --security-opt "$2" "$image" \
            timeout 45s scripts/verify-linux.sh --readiness-only; then
            printf 'Readiness passed with only the %s change.\n' "$1"
        else
            printf 'Readiness did not pass with the %s change (exit %s).\n' "$1" "$?"
        fi
    }

    if [ -n "${WYRIT_DOCKER_DEFAULT_SECCOMP:-}" ]; then
        profile=$(mktemp "${TMPDIR:-/tmp}/wyrit-seccomp.XXXXXXXX")
        trap 'rm -f "$profile"' EXIT
        if python3 -c '
import json
import sys

with open(sys.argv[1]) as source:
    profile = json.load(source)
with open("examples/container/seccomp-readiness-rules.json") as source:
    rules = json.load(source)
if profile["defaultAction"] != "SCMP_ACT_ERRNO":
    sys.exit("Expected a Docker default-deny seccomp baseline")
profile["syscalls"].extend(rules)
json.dump(profile, sys.stdout)
' "$WYRIT_DOCKER_DEFAULT_SECCOMP" >"$profile"; then
            diagnostic seccomp "seccomp=$profile"
        else
            echo 'Seccomp diagnostic unavailable: could not construct the narrow profile.' >&2
        fi
    else
        echo 'Seccomp diagnostic unavailable: set WYRIT_DOCKER_DEFAULT_SECCOMP to the daemon baseline JSON.' >&2
    fi

    if [ "${WYRIT_DOCKER_APPARMOR_DIAGNOSTIC:-}" = 1 ]; then
        diagnostic AppArmor apparmor=wyrit-readiness
    else
        echo 'AppArmor diagnostic unavailable: load wyrit-readiness on the daemon host and set WYRIT_DOCKER_APPARMOR_DIAGNOSTIC=1.' >&2
    fi

    printf '%s\n' \
        'For a failed setup, correlate bwrap/unshare with the Docker host kernel audit log.' \
        'An AppArmor DENIED record identifies its profile and operation. A SECCOMP denial identifies the syscall.' \
        'Without matching records or a hard namespace limit, EPERM alone leaves attribution unresolved.' >&2
fi
exit "$status"
