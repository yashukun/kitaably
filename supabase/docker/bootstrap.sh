#!/bin/sh
set -eu

if [ $# -eq 0 ]; then
    set -- migrate buckets
elif [ "$1" = "all" ] && [ $# -eq 1 ]; then
    set -- migrate buckets seed
fi

for step in "$@"; do
    case "$step" in
        migrate|buckets|seed)
            echo "bootstrap: step $step"
            "/usr/local/bin/$step.sh"
            ;;
        *)
            echo "bootstrap: unknown step '$step' (steps: migrate, buckets, seed, all)" >&2
            exit 2
            ;;
    esac
done

echo "bootstrap: done ($*)"
