#!/usr/bin/env bash
# skm 安装脚本
#
#   bash install.sh              从当前克隆安装（默认：软链到 ~/.local/bin）
#   bash install.sh --copy       复制而不是软链（克隆目录可随意移动）
#   bash install.sh --pipx       用 pipx 装成隔离的独立包（需 pipx）
#   bash install.sh --binary     只把 bin/skm 复制到 ~/.local/bin，需自己维护源码
#   bash install.sh --dir DIR    安装到 DIR（默认 ~/.local/bin）
#   bash install.sh --repo DIR   仓库根目录（默认 ~/.skill-manager）
#   bash install.sh --uninstall  卸载（删掉 skm 命令；默认不动数据目录）
#
# 安装动作只有两件：把 skm 命令放进 PATH，并在首次安装时生成配置文件。
# 数据目录（仓库 / 配置）永远不动，除非你显式传 --repo。
set -euo pipefail

MODE="link"
BIN_DIR="${HOME}/.local/bin"
REPO_DIR="${HOME}/.skill-manager"
MOVE_DATA=0
UNINSTALL=0

die() { echo "install.sh: $*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --link) MODE="link" ;;
    --copy) MODE="copy" ;;
    --pipx) MODE="pipx" ;;
    --binary) MODE="binary" ;;
    --dir) shift; BIN_DIR="${1:-}" ;;
    --repo) shift; REPO_DIR="${1:-}"; MOVE_DATA=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
  shift
done

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------- 卸载
if [ "$UNINSTALL" -eq 1 ]; then
  echo "卸载 skm"
  removed=0
  for candidate in "${BIN_DIR}/skm" "${HOME}/.local/bin/skm"; do
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
      rm -f "$candidate" && info "已删除 $candidate" && removed=1
    fi
  done
  # --binary 会额外把实现复制到 <BIN_DIR>/skm-cli；不清掉就会留下孤儿目录，
  # 而它的名字（skm-cli）会让用户以为是别的程序的安装结果。
  payload="${BIN_DIR}/skm-cli"
  if [ -d "$payload" ]; then
    rm -rf "$payload" && info "已删除自包含安装目录 $payload" && removed=1
  fi
  [ "$removed" -eq 1 ] || info "没找到已安装的 skm 命令"
  info "数据目录保持不动：${REPO_DIR}（要删请自行确认后 rm -rf）"
  exit 0
fi

# ---------------------------------------------------------------- pipx
if [ "$MODE" = "pipx" ]; then
  command -v pipx >/dev/null 2>&1 \
    || die "没找到 pipx。先装 pipx，或改用默认的 --link 方式。"
  echo "用 pipx 从 $ROOT 安装"
  pipx install --force "$ROOT"
  info "命令：$(command -v skm || echo 'skm（pipx 的 bin 目录）')"
  echo
  echo "下一步：初始化配置"
  echo "  skm config init                        # 使用默认仓库 ~/.skill-manager"
  echo "  skm config init --base-dir ${REPO_DIR}  # 或指定仓库根目录"
  exit 0
fi

[ -d "${ROOT}/src/skm" ] || die "在 ${ROOT} 下找不到 src/skm，请从仓库根目录运行本脚本。"
command -v python3 >/dev/null 2>&1 || die "没找到 python3（需要 3.11+）。"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "需要 Python 3.11+（当前 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')）。"

mkdir -p "$BIN_DIR"
TARGET="${BIN_DIR}/skm"

echo "安装 skm"
case "$MODE" in
  link)
    # 软链：bin/skm 会 readlink -f 解析到脚本真身，因此升级只需 git pull
    ln -sfn "${ROOT}/bin/skm" "$TARGET"
    info "已软链 ${TARGET} -> ${ROOT}/bin/skm"
    ;;
  copy)
    # 生成一个指向克隆目录的短包装脚本。用它的理由：某些环境不欢迎 PATH 里的软链。
    # 包装脚本必须写死克隆路径 —— 否则它会像软链一样按自身位置推导 ROOT，却推导错。
    cat >"$TARGET" <<EOF
#!/usr/bin/env bash
# 由 skm 的 install.sh --copy 生成；实现与数据位置都在原克隆目录里。
exec "${ROOT}/bin/skm" "\$@"
EOF
    chmod +x "$TARGET"
    info "已写入包装脚本 ${TARGET}（指向 ${ROOT}/bin/skm）"
    info "克隆目录移动后需重新运行安装"
    ;;
  binary)
    # 自包含：把实现一起复制进去，之后克隆目录可以删掉
    # 布局必须与 bin/skm 的推导一致（它取「自己所在目录的上一级」当 ROOT，
    # 再去找 ROOT/src/skm），所以这里是 <install_dir>/bin/skm + <install_dir>/src/skm
    install_dir="${BIN_DIR}/skm-cli"
    rm -rf "$install_dir"
    mkdir -p "$install_dir/src" "$install_dir/bin"
    cp -r "${ROOT}/src/skm" "$install_dir/src/"
    cp "${ROOT}/bin/skm" "$install_dir/bin/skm"
    chmod +x "$install_dir/bin/skm"
    ln -sfn "$install_dir/bin/skm" "$TARGET"
    info "已自包含安装到 ${install_dir}，命令 ${TARGET}"
    ;;
esac

# ---------------------------------------------------------------- PATH 检查
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo
     echo "提示：${BIN_DIR} 不在 PATH 里，把下面这行加到 ~/.bashrc："
     echo "  export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# ---------------------------------------------------------------- 首次配置
echo
CONFIG_FILE="${REPO_DIR}/config.toml"
if [ -f "$CONFIG_FILE" ]; then
  info "配置已存在，未改动：${CONFIG_FILE}"
elif [ "$MOVE_DATA" -eq 1 ]; then
  "$TARGET" config init --base-dir "$REPO_DIR" >/dev/null
  info "已生成配置：${CONFIG_FILE}"
else
  info "未生成配置文件（用内置默认：仓库 ~/.skill-manager，目标 ~/.agents/skills）"
  info "想把它固化成文件：skm config init"
fi

echo
echo "完成。验证一下："
echo "  skm --version && skm agents && skm list"
echo
echo "首次使用建议流程："
echo "  1) 把 skill 目录放进仓库：~/.skill-manager/skills/<名字>/SKILL.md"
echo "     （分类就是中间加一层目录：~/.skill-manager/skills/<分类>/<名字>/SKILL.md）"
echo "  2) skm list                 看看认到了没有"
echo "  3) skm enable <名字>        启用（会在 ~/.agents/skills 建软链）"
echo "  4) 重启 agent 生效"