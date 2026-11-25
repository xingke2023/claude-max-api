#!/bin/bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
stamp=/var/lib/claude-code-update/last-success
now=$(date +%s)
if [[ -f "$stamp" ]]; then
    last=$(cat "$stamp")
    if (( now - last < 259200 )); then
        echo 'Claude Code update check is not due yet.'
        exit 0
    fi
fi
before=$(claude --version)
latest=$(npm view @anthropic-ai/claude-code@latest version)
if [[ "${before%% *}" != "$latest" ]]; then
    npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@latest
    after=$(claude --version)
    echo "Claude Code updated: $before -> $after"
    systemctl try-restart claude-max-proxy.service
else
    echo "Claude Code is current: $before"
fi
# npm 不会自动清理缓存，每次 sudo npm install 都会在 /root/.npm 堆积安装包
npm cache clean --force || echo 'npm cache clean failed' >&2
date +%s > "$stamp"
