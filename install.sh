#!/usr/bin/env bash
# skm 安装脚本
#
#   bash install.sh                  默认：自包含安装（实现复制进 ~/.skill-manager，源码可删）
#   bash install.sh --dev            开发模式：命令软链到克隆目录（改代码立即生效，但需保留源码）
#   bash install.sh --pipx           用 pipx 装成隔离的独立包（需 pipx）
#   bash install.sh --bin-dir DIR    skm 命令放到哪里（默认 ~/.local/bin）
#   bash install.sh --base-dir DIR   实现与数据放在哪里（默认 ~/.skill-manager）
#   bash install.sh --uninstall      卸载程序；skills/ 与 config.toml 保留
#   bash install.sh --uninstall --purge [--yes]
#                                    连数据一起删（先列出要删什么；需再加 --yes 才真删）
#
# 安装后的布局（--base-dir 的默认值即 ~/.skill-manager）：
#
#   ~/.local/bin/skm          → 软链，指向 ~/.skill-manager/bin/skm
#   ~/.skill-manager/
#     bin/skm                 启动脚本（副本）
#     lib/skm/                实现（副本）—— 删掉源码目录不影响使用
#     skills/                 你的 skill 仓库（安装/卸载都不动它）
#     config.toml             配置（安装/卸载都不动它）
#
# 升级：在源码目录 git pull 后重新运行本脚本即可（会覆盖 bin/ 与 lib/）。
set -euo pipefail

MODE="install"
BIN_DIR="${HOME}/.local/bin"
BASE_DIR="${HOME}/.skill-manager"
UNINSTALL=0
PURGE=0
CONFIRM=0

die()  { echo "install.sh: $*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }
note() { printf '  %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dev) MODE="dev" ;;
    --pipx) MODE="pipx" ;;
    --bin-dir) shift; BIN_DIR="${1:-}" ;;
    --base-dir) shift; BASE_DIR="${1:-}" ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --yes|-y) CONFIRM=1 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
  shift
done

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${BIN_DIR}/skm"

# ---------------------------------------------------------------- 卸载
if [ "$UNINSTALL" -eq 1 ]; then
  echo "卸载 skm"
  removed=0

  for candidate in "$TARGET" "${HOME}/.local/bin/skm"; do
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
      rm -f "$candidate" && info "已删除命令 $candidate" && removed=1
    fi
  done
  # 兼容早期版本把自包含目录放在 <bin-dir>/skm-cli 的布局
  if [ -d "${BIN_DIR}/skm-cli" ]; then
    rm -rf "${BIN_DIR}/skm-cli" && info "已删除旧式安装目录 ${BIN_DIR}/skm-cli" && removed=1
  fi

  for payload in "${BASE_DIR}/bin" "${BASE_DIR}/lib"; do
    if [ -e "$payload" ]; then
      rm -rf "$payload" && info "已删除 $payload" && removed=1
    fi
  done

  [ "$removed" -eq 1 ] || info "没有找到已安装的程序"

  if [ "$PURGE" -eq 1 ]; then
    echo
    # 数据是用户的资产：先如实列出要删什么，再要求显式确认，绝不"顺手"删掉
    skill_count=0
    [ -d "${BASE_DIR}/skills" ] && skill_count=$(find "${BASE_DIR}/skills" -name SKILL.md 2>/dev/null | wc -l)
    echo "  --purge 会删除以下数据（不可恢复）："
    echo "      仓库：${BASE_DIR}/skills     （${skill_count} 个 skill）"
    echo "      配置：${BASE_DIR}/config.toml"
    if [ "$CONFIRM" -eq 1 ]; then
      rm -rf "${BASE_DIR}"
      info "已删除整个 ${BASE_DIR}"
    else
      echo
      note "未删除：需要再加 --yes 明确确认（这会永久删除 ${skill_count} 个 skill）。"
      exit 1
    fi
  else
    echo
    info "数据保留：${BASE_DIR}/skills 与 config.toml（要连数据一起删：--uninstall --purge --yes）"
  fi
  exit 0
fi

# ---------------------------------------------------------------- pipx
if [ "$MODE" = "pipx" ]; then
  command -v pipx >/dev/null 2>&1 \
    || die "没找到 pipx。先装 pipx，或改用默认的自包含安装方式。"
  echo "用 pipx 从 $ROOT 安装"
  pipx install --force "$ROOT"
  info "命令：$(command -v skm || echo 'skm（pipx 的 bin 目录）')"
  echo
  echo "下一步：初始化配置"
  echo "  skm config init                        # 使用默认仓库 ~/.skill-manager"
  echo "  skm config init --base-dir ${BASE_DIR}  # 或指定仓库根目录"
  exit 0
fi

[ -d "${ROOT}/src/skm" ] || die "在 ${ROOT} 下找不到 src/skm，请从源码目录运行本脚本。"
[ -f "${ROOT}/bin/skm" ] || die "在 ${ROOT} 下找不到 bin/skm，请从源码目录运行本脚本。"
command -v python3 >/dev/null 2>&1 || die "没找到 python3（需要 3.11+）。"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "需要 Python 3.11+（当前 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')）。"

mkdir -p "$BIN_DIR"

# ---------------------------------------------------------------- --dev
if [ "$MODE" = "dev" ]; then
  echo "安装 skm（开发模式）"
  ln -sfn "${ROOT}/bin/skm" "$TARGET"
  info "已软链 ${TARGET} -> ${ROOT}/bin/skm"
  info "实现直接取自源码目录 ${ROOT}，改动立即生效"
  note "注意：此模式依赖源码目录存在，删掉源码后 skm 将不可用。"
  note "      日常使用请改用默认安装：bash install.sh"
  if [ ! -f "${BASE_DIR}/config.toml" ]; then
    "${TARGET}" config init --base-dir "$BASE_DIR" >/dev/null 2>&1 || true
    [ -f "${BASE_DIR}/config.toml" ] && info "已生成配置：${BASE_DIR}/config.toml"
  else
    info "配置已存在，未改动：${BASE_DIR}/config.toml"
  fi
  echo
  echo "验证：skm --version && skm agents"
  exit 0
fi

# ---------------------------------------------------------------- 默认：自包含安装
echo "安装 skm（自包含）"
mkdir -p "${BASE_DIR}/lib" "${BASE_DIR}/bin" "${BASE_DIR}/skills"

# 实现与启动脚本都复制一份到 BASE_DIR：此后源码目录即可删除。
# 布局必须与 bin/skm 的推导一致：它取「自身所在目录的上一级」当 ROOT，
# 再依次找 ROOT/lib/skm（安装后）与 ROOT/src/skm（源码树）。
rm -rf "${BASE_DIR}/lib/skm"
cp -r "${ROOT}/src/skm" "${BASE_DIR}/lib/"
find "${BASE_DIR}/lib/skm" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "${ROOT}/bin/skm" "${BASE_DIR}/bin/skm"
chmod +x "${BASE_DIR}/bin/skm"
info "实现已安装到 ${BASE_DIR}/lib/skm"

ln -sfn "${BASE_DIR}/bin/skm" "$TARGET"
info "命令 ${TARGET} -> ${BASE_DIR}/bin/skm"

# ---------------------------------------------------------------- 配置
if [ -f "${BASE_DIR}/config.toml" ]; then
  info "配置已存在，未改动：${BASE_DIR}/config.toml"
else
  "$TARGET" config init --base-dir "$BASE_DIR" >/dev/null
  info "已生成配置：${BASE_DIR}/config.toml"
fi

# ---------------------------------------------------------------- PATH 检查
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo
     note "提示：${BIN_DIR} 不在 PATH 里，把下面这行加到 ~/.bashrc："
     note "  export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

echo
echo "安装完成。源码目录现在可以删掉，不影响使用。"
echo
echo "验证："
echo "  skm --version && skm agents && skm list"
echo
echo "首次使用："
echo "  1) 把 skill 放进仓库：${BASE_DIR}/skills/<名字>/SKILL.md"
echo "     （分类就是中间加一层目录：${BASE_DIR}/skills/<分类>/<名字>/SKILL.md）"
echo "     或让 skm 扫描已有 skill 并收进仓库："
echo "       skm import          先看计划（不改动任何文件）"
echo "       skm import --yes    确认后执行（原位置会自动补链接）"
echo "  2) skm list                 看看认到了没有"
echo "  3) skm enable <名字>        启用（会在 agent 目录里建软链）"
echo "  4) 重启 agent 生效"
echo
echo "升级：在源码目录 git pull 后重新运行 bash install.sh"
echo "卸载：bash install.sh --uninstall"
