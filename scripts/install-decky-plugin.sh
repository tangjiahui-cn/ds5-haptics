#!/usr/bin/env bash
# ============================================================
# DS5-Haptics —— Decky 插件安装脚本
# 用法（在仓库根目录构建好之后）：
#   sudo bash scripts/install-decky-plugin.sh
# 说明：插件运行时不需要 root；这里只负责把插件文件放进 Decky 插件目录。
# ============================================================
set -euo pipefail

PLUGIN_ID="DS5-Haptics"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="/home/deck/homebrew/plugins/${PLUGIN_ID}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行：sudo bash \"$0\""
    exit 1
fi

for f in plugin.json main.py mhwild_haptics.py dist/index.js; do
    if [ ! -e "${REPO_DIR}/${f}" ]; then
        echo "缺少文件：${REPO_DIR}/${f}"
        echo "请先在仓库根目录执行：npm ci && npm run build"
        exit 1
    fi
done

echo "==> 安装到 ${DEST}"
mkdir -p "$DEST"
rm -rf "${DEST}/dist"
cp "${REPO_DIR}/plugin.json" "${REPO_DIR}/package.json" \
   "${REPO_DIR}/main.py" "${REPO_DIR}/mhwild_haptics.py" "$DEST/"
cp -r "${REPO_DIR}/dist" "${DEST}/dist"
chown -R root:root "$DEST"
chmod 755 "$DEST"

echo "==> 重启 Decky Loader ..."
systemctl restart plugin_loader

echo
echo "✔ 安装完成。"
echo "   1) 游戏模式 → QAM → 找到「DS5-Haptics」"
echo "   2) 配置保存在 /home/deck/.local/share/mhwild-haptics/"
echo "   3) 插件日志：~/homebrew/logs/${PLUGIN_ID}/"
