#!/usr/bin/env bash
# install.sh 的验证套件（离线，不联网，不触碰真实 HOME）
#
# 用法：bash tests/install.sh
#
# 每个安装方式都用一个临时 HOME，验证：命令可用、数据目录不被擅动、
# 重复执行幂等、卸载干净（含 --binary 的自包含目录）、错误参数被拒绝。
set -uo pipefail

# 以本仓库为源码；bin/skm 会按自身位置推导 ROOT，因此这里必须复制后再测，
# 否则 "删掉源码目录仍可用" 这类用例会误伤工作区。
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=$(mktemp -d /tmp/skm-install-test-XXXXXX)
SRC="$WORK/src"

pass=0; fail=0
ok()    { pass=$((pass+1)); echo "  ✓ $1"; }
bad()   { fail=$((fail+1)); echo "  ✗ $1"; }
want_ok()  { if "${@:2}" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
want_err() { if "${@:2}" >/dev/null 2>&1; then bad "$1"; else ok "$1"; fi; }
has()   { if [ -e "$2" ] || [ -L "$2" ]; then ok "$1"; else bad "$1"; fi; }
gone()  { if [ -e "$2" ] || [ -L "$2" ]; then bad "$1"; else ok "$1"; fi; }
hasnt() { gone "$@"; }
screen(){ if grep -qF -- "$2" <<<"$3"; then ok "$1"; else bad "$1"; echo "$3" | sed 's/^/      /'; fi; }

cp -r "$ROOT" "$SRC"
rm -rf "$SRC/.git"          # 只留文件，避免误把用例绑在 git 状态上
newhome() { mktemp -d "$WORK/home-XXXXXX"; }

echo "源码：$SRC"
echo "工作目录：$WORK"

# ---------------------------------------------------------------- --link
echo
echo "== --link（默认）=="
H=$(newhome); export HOME="$H"
out=$(cd "$SRC" && bash install.sh --link 2>&1)
screen "报告已软链" "已软链" "$out"
has "命令已就位" "$H/.local/bin/skm"
want_ok "skm --version 可运行" "$H/.local/bin/skm" --version
screen "版本号正确" "skm 0.1.0" "$("$H/.local/bin/skm" --version)"
want_ok "skm list 可运行" "$H/.local/bin/skm" list
screen "缺配置时用内置默认" "使用内置默认" "$("$H/.local/bin/skm" agents)"
gone "未擅自生成配置文件" "$H/.skill-manager/config.toml"

out=$(cd "$SRC" && bash install.sh --link 2>&1)
screen "重复安装不报错" "已软链" "$out"
want_ok "重复安装后仍可运行" "$H/.local/bin/skm" --version

echo "  -- 装上就能干活（建仓库 → 启用 → 停用）--"
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

# ---------------------------------------------------------------- --copy
echo
echo "== --copy（包装脚本）=="
H=$(newhome); export HOME="$H"
out=$(cd "$SRC" && bash install.sh --copy 2>&1)
screen "报告已写入包装脚本" "已写入包装脚本" "$out"
screen "是脚本而非软链" 'exec "' "$(cat "$H/.local/bin/skm")"
want_ok "包装脚本可运行" "$H/.local/bin/skm" --version

# ---------------------------------------------------------------- --binary
echo
echo "== --binary（自包含：删掉源码目录后仍可用）=="
H=$(newhome); export HOME="$H"
HAS_SRC="$WORK/src-binary"; cp -r "$SRC" "$HAS_SRC"
out=$(cd "$HAS_SRC" && bash install.sh --binary 2>&1)
screen "报告自包含安装" "自包含安装" "$out"
has "自包含目录已创建" "$H/.local/bin/skm-cli/src/skm/cli.py"
want_ok "安装后可运行" "$H/.local/bin/skm" --version
rm -rf "$HAS_SRC"
gone "源码目录已删除" "$HAS_SRC"
want_ok "删掉源码后仍可运行" "$H/.local/bin/skm" --version
want_ok "删掉源码后 list 仍可运行" "$H/.local/bin/skm" list

# ---------------------------------------------------------------- --dir / --repo
echo
echo "== --dir 与 --repo =="
H=$(newhome); export HOME="$H"
out=$(cd "$SRC" && bash install.sh --link --dir "$H/mybin" 2>&1)
screen "装到自定义目录" "mybin/skm" "$out"
has "自定义目录里的命令" "$H/mybin/skm"
out=$(cd "$SRC" && bash install.sh --link --dir "$H/mybin" --repo "$H/myrepo" 2>&1)
screen "生成了自定义仓库的配置" "已生成配置" "$out"
has "自定义仓库目录" "$H/myrepo/skills"
screen "配置指向自定义仓库" "$H/myrepo" "$(grep -F base_dir "$H/myrepo/config.toml")"

# ---------------------------------------------------------------- 错误处理
echo
echo "== 参数与前置条件 =="
H=$(newhome)
screen "未知参数被拒绝" "未知参数" "$(cd "$SRC" && HOME="$H" bash install.sh --bogus 2>&1)"
want_ok "--help 可用" env HOME="$H" bash "$SRC/install.sh" --help
screen "--help 列出卸载" "uninstall" "$(cd "$SRC" && HOME="$H" bash install.sh --help 2>&1)"
want_err "非仓库目录下被拒绝" env HOME="$H" bash "$WORK/nope/install.sh"

# ---------------------------------------------------------------- 卸载
echo
echo "== --uninstall =="
H=$(newhome); export HOME="$H"
(cd "$SRC" && bash install.sh --link >/dev/null 2>&1)
"$H/.local/bin/skm" config init >/dev/null 2>&1
has "卸载前：命令在" "$H/.local/bin/skm"
has "卸载前：数据在" "$H/.skill-manager/config.toml"
out=$(cd "$SRC" && bash install.sh --uninstall 2>&1)
screen "报告已删除命令" "已删除" "$out"
gone "卸载后：命令已移除" "$H/.local/bin/skm"
has "卸载后：数据保留" "$H/.skill-manager/config.toml"
screen "提示数据未动" "数据目录保持不动" "$out"
out=$(cd "$SRC" && bash install.sh --uninstall 2>&1)
screen "重复卸载不报错" "没找到已安装的 skm 命令" "$out"

echo "  -- --binary 卸载必须连自包含目录一起清掉（回归用例）--"
H=$(newhome); export HOME="$H"
(cd "$SRC" && bash install.sh --binary >/dev/null 2>&1)
has "卸载前：自包含目录在" "$H/.local/bin/skm-cli"
out=$(cd "$SRC" && bash install.sh --uninstall 2>&1)
screen "卸载报告清理了自包含目录" "已删除自包含安装目录" "$out"
gone "卸载后：命令已移除" "$H/.local/bin/skm"
gone "卸载后：自包含目录已清掉" "$H/.local/bin/skm-cli"

echo
echo "通过 $pass，失败 $fail"
[ "$fail" -eq 0 ]
