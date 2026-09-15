#!/usr/bin/env bash
# install.sh 的验证套件（离线，不联网，不触碰真实 HOME）
#
# 用法：bash tests/install.sh
#
# 重点验证「自包含」这一契约：安装后删掉源码目录，skm 仍必须可用。
# 每个用例都用独立临时 HOME，绝不碰真实 ~/.local/bin 与 ~/.skill-manager。
set -uo pipefail

# 以本仓库为源码。bin/skm 会按自身位置推导实现目录，而安装逻辑也会把源码复制走，
# 所以每个用例都用源码副本，避免用例之间互相污染、也不会误伤工作区。
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=$(mktemp -d /tmp/skm-install-test-XXXXXX)

pass=0; fail=0
ok()    { pass=$((pass+1)); echo "  ✓ $1"; }
bad()   { fail=$((fail+1)); echo "  ✗ $1"; }
want_ok()  { if "${@:2}" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
want_err() { if "${@:2}" >/dev/null 2>&1; then bad "$1"; else ok "$1"; fi; }
has()   { if [ -e "$2" ] || [ -L "$2" ]; then ok "$1"; else bad "$1"; fi; }
gone()  { if [ -e "$2" ] || [ -L "$2" ]; then bad "$1"; else ok "$1"; fi; }
screen(){ if grep -qF -- "$2" <<<"$3"; then ok "$1"; else bad "$1"; echo "$3" | sed 's/^/      /'; fi; }

src_copy() { # $1 = 目标目录；返回可用的源码副本
  cp -r "$REPO" "$1"
  rm -rf "$1/.git"
  echo "$1"
}
newhome() { mktemp -d "$WORK/home-XXXXXX"; }

echo "源码：$REPO"
echo "工作目录：$WORK"

# ---------------------------------------------------------------- 默认：自包含
echo
echo "== 默认安装：自包含 =="
SRC=$(src_copy "$WORK/src-default")
H=$(newhome); export HOME="$H"
out=$(cd "$SRC" && bash install.sh 2>&1)
screen "报告自包含安装" "自包含" "$out"
screen "实现装进 ~/.skill-manager/lib" "$H/.skill-manager/lib/skm" "$out"
has "实现已落在 lib/skm/cli.py" "$H/.skill-manager/lib/skm/cli.py"
has "启动脚本已落在 bin/skm" "$H/.skill-manager/bin/skm"
has "命令已就位" "$H/.local/bin/skm"
has "仓库目录已建" "$H/.skill-manager/skills"
has "配置已生成" "$H/.skill-manager/config.toml"
screen "命令指向 BASE_DIR 而不是源码" "$H/.skill-manager/bin/skm" "$(readlink "$H/.local/bin/skm")"
want_ok "skm --version 可运行" "$H/.local/bin/skm" --version
screen "版本号正确" "skm 0.1.0" "$("$H/.local/bin/skm" --version)"

echo "  -- 关键契约：删掉源码目录后仍可用 --"
rm -rf "$SRC"
gone "源码目录已删除" "$SRC"
want_ok "删源码后 skm --version 仍可用" "$H/.local/bin/skm" --version
want_ok "删源码后 skm list 仍可用" "$H/.local/bin/skm" list
want_ok "删源码后 skm agents 仍可用" "$H/.local/bin/skm" agents
has "删源码后仍不影响数据" "$H/.skill-manager/config.toml"

echo "  -- 装上就能干活 --"
mkdir -p "$H/.agents/skills" "$H/.skill-manager/skills/demo"
printf -- '---\nname: demo\ndescription: 安装验证\n---\n\nbody\n' \
  >"$H/.skill-manager/skills/demo/SKILL.md"
screen "list 认出 skill" "demo" "$("$H/.local/bin/skm" list)"
want_ok "enable 成功" "$H/.local/bin/skm" enable demo
has "链接已建立" "$H/.agents/skills/demo"
screen "status 显示 linked" "linked" "$("$H/.local/bin/skm" status demo)"
want_ok "disable 成功" "$H/.local/bin/skm" disable demo
gone "链接已删除" "$H/.agents/skills/demo"
has "仓库真身保留" "$H/.skill-manager/skills/demo/SKILL.md"

echo "  -- 幂等与升级 --"
SRC=$(src_copy "$WORK/src-again")
out=$(cd "$SRC" && bash install.sh 2>&1)
screen "重复安装不报错" "安装完成" "$out"
has "重复安装后配置仍在" "$H/.skill-manager/config.toml"
# 模拟升级：改动副本里的实现，重装后应生效
sed -i 's/^__version__ = .*/__version__ = "9.9.9"/' "$SRC/src/skm/__init__.py"
(cd "$SRC" && bash install.sh >/dev/null 2>&1)
screen "重装后新版本生效（升级路径）" "skm 9.9.9" "$("$H/.local/bin/skm" --version)"
sed -i 's/^__version__ = .*/__version__ = "0.1.0"/' "$SRC/src/skm/__init__.py"

echo "  -- 旧实现残留不应被带进新安装 --"
SRC=$(src_copy "$WORK/src-pyc")
mkdir -p "$SRC/src/skm/__pycache__"; touch "$SRC/src/skm/__pycache__/stale.pyc"
(cd "$SRC" && bash install.sh >/dev/null 2>&1)
gone "已清理 __pycache__" "$H/.skill-manager/lib/skm/__pycache__"

# ---------------------------------------------------------------- --dev
echo
echo "== --dev：软链到源码（改动立即生效，但依赖源码）=="
SRC=$(src_copy "$WORK/src-dev")
H=$(newhome); export HOME="$H"
out=$(cd "$SRC" && bash install.sh --dev 2>&1)
screen "报告开发模式" "开发模式" "$out"
want_ok "skm 可运行" "$H/.local/bin/skm" --version
screen "命令指向源码目录" "$SRC/bin/skm" "$(readlink "$H/.local/bin/skm")"
screen "已提示依赖源码" "删掉源码后 skm 将不可用" "$out"
# 注意：这里用长度不同的版本号。CPython 的 pyc 校验只看「整数秒 mtime + 文件大小」，
# 同一秒内做等长修改会被判定为未变更而复用旧字节码（Python 固有行为，非本工具问题）。
sed -i 's/^__version__ = .*/__version__ = "9.9.800"/' "$SRC/src/skm/__init__.py"
screen "改源码立即生效" "skm 9.9.800" "$("$H/.local/bin/skm" --version)"

# ---------------------------------------------------------------- --bin-dir / --base-dir
echo
echo "== --bin-dir 与 --base-dir =="
SRC=$(src_copy "$WORK/src-dirs")
H=$(newhome); export HOME="$H"
out=$(cd "$SRC" && bash install.sh --bin-dir "$H/mybin" --base-dir "$H/mydata" 2>&1)
screen "命令装到自定义 bin" "mybin/skm" "$out"
has "自定义 bin 里的命令" "$H/mybin/skm"
has "实现装到自定义 base" "$H/mydata/lib/skm/cli.py"
screen "配置指向自定义 base" "$H/mydata" "$(grep -F base_dir "$H/mydata/config.toml")"
want_ok "自定义布局可运行" "$H/mybin/skm" --version

# ---------------------------------------------------------------- 参数与前置条件
echo
echo "== 参数与前置条件 =="
H=$(newhome)
screen "未知参数被拒绝" "未知参数" "$(cd "$WORK/src-dirs" && HOME="$H" bash install.sh --bogus 2>&1)"
want_ok "--help 可用" env HOME="$H" bash "$WORK/src-dirs/install.sh" --help
screen "--help 说明自包含" "自包含" "$(cd "$WORK/src-dirs" && HOME="$H" bash install.sh --help 2>&1)"
want_err "非源码目录下被拒绝" env HOME="$H" bash "$WORK/nope/install.sh"

# ---------------------------------------------------------------- 卸载
echo
echo "== 卸载（默认保留数据）=="
SRC=$(src_copy "$WORK/src-uninstall")
H=$(newhome); export HOME="$H"
(cd "$SRC" && bash install.sh >/dev/null 2>&1)
has "卸载前：命令在" "$H/.local/bin/skm"
has "卸载前：实现在" "$H/.skill-manager/lib/skm/cli.py"
out=$(cd "$SRC" && bash install.sh --uninstall 2>&1)
screen "报告已删命令" "已删除命令" "$out"
gone "卸载后：命令已移除" "$H/.local/bin/skm"
gone "卸载后：实现已移除" "$H/.skill-manager/lib"
gone "卸载后：启动脚本已移除" "$H/.skill-manager/bin"
has "卸载后：skills 保留" "$H/.skill-manager/skills"
has "卸载后：config 保留" "$H/.skill-manager/config.toml"
screen "提示数据保留与 purge 用法" "--purge --yes" "$out"
out=$(cd "$SRC" && bash install.sh --uninstall 2>&1)
screen "重复卸载不报错" "没有找到已安装的程序" "$out"

echo "  -- --purge 必须显式二次确认 --"
SRC=$(src_copy "$WORK/src-purge")
H=$(newhome); export HOME="$H"
(cd "$SRC" && bash install.sh >/dev/null 2>&1)
mkdir -p "$H/.skill-manager/skills/keepme"
printf -- '---\nname: keepme\ndescription: x\n---\n' >"$H/.skill-manager/skills/keepme/SKILL.md"
out=$(cd "$SRC" && HOME="$H" bash install.sh --uninstall --purge 2>&1)
screen "未加 --yes 时明确拒绝" "需要再加 --yes" "$out"
has "被拒绝后数据完好（skills）" "$H/.skill-manager/skills/keepme/SKILL.md"
has "被拒绝后数据完好（config）" "$H/.skill-manager/config.toml"
want_err "未加 --yes 时退出码非 0" bash -c "cd '$SRC' && HOME='$H' bash install.sh --uninstall --purge"
out=$(cd "$SRC" && bash install.sh --uninstall --purge --yes 2>&1)
screen "加了 --yes 才真删" "已删除整个" "$out"
gone "purge 后整个 BASE_DIR 消失" "$H/.skill-manager"

echo "  -- 早期版本的 <bin-dir>/skm-cli 残留应被清理 --"
H=$(newhome); export HOME="$H"
mkdir -p "$H/.local/bin/skm-cli/src/skm"
(cd "$SRC" && bash install.sh --uninstall 2>&1) >/dev/null
gone "旧式安装目录已清掉" "$H/.local/bin/skm-cli"

# ---------------------------------------------------------------- pipx
echo
echo "== --pipx =="
if command -v pipx >/dev/null 2>&1; then
  SRC=$(src_copy "$WORK/src-pipx")
  H=$(newhome); export HOME="$H"
  if env HOME="$H" PIPX_HOME="$H/pipx" PIPX_BIN_DIR="$H/pipxbin" pipx install --force "$SRC" >/dev/null 2>&1; then
    ok "pipx 安装成功"
    want_ok "pipx 安装的 skm 可运行" "$H/pipxbin/skm" --version
    screen "pipx 版本正确" "skm 0.1.0" "$("$H/pipxbin/skm" --version)"
  else
    bad "pipx 安装失败"
  fi
else
  echo "  – 跳过：本机没有 pipx"
fi

echo
echo "通过 $pass，失败 $fail"
[ "$fail" -eq 0 ]
