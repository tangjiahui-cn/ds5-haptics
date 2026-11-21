#!/usr/bin/env bash
# ============================================================
# DS5-Haptics —— Decky 插件卸载脚本
# 用法：sudo bash scripts/uninstall-decky-plugin.sh
# 说明：只删除插件本体，不动 ~/.local/share/mhwild-haptics/ 的配置。
# ============================================================
set -euo pipefail

PLUGIN_ID="DS5-Haptics"
DEST="/home/deck/homebrew/plugins/${PLUGIN_ID}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行：sudo bash \"$0\""
    exit 1
fi

if [ -d "$DEST" ]; then
    rm -rf "$DEST"
    echo "==> 已删除 ${DEST}"
else
    echo "==> 未找到 ${DEST}（可能未安装）"
fi

systemctl restart plugin_loader
echo "✔ 已卸载并重启 Decky Loader（配置保留在 ~/.local/share/mhwild-haptics/）"
