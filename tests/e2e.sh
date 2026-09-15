#!/usr/bin/env bash
# skm 端到端验证（隔离环境，不触碰真实数据）
#
# 用法：bash tests/e2e.sh
# 覆盖：列表/树形、单查与批查、启用/停用、幂等、冲突保护（真目录/他人链接/断链）、
#       残留链接清理、多 agent 与 --agent 定向、glob、JSON、退出码。
set -uo pipefail

SKM="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/bin/skm"
T=$(mktemp -d /tmp/skm-e2e-XXXXXX)
export SKM_BASE_DIR="$T/repo"
export HOME="$T/home"
mkdir -p "$HOME/.agents/skills" "$SKM_BASE_DIR/skills"

mk_skill() { # $1 = 仓库内相对路径
  local p="$SKM_BASE_DIR/skills/$1"
  mkdir -p "$p"
  local n; n=$(basename "$p")
  printf -- '---\nname: %s\ndescription: test skill %s\n---\n\nbody\n' "$n" "$n" >"$p/SKILL.md"
}
mk_skill cat-a/skill-one
mk_skill cat-a/skill-three
mk_skill skill-two
mk_skill deep/nested/bad   # 深度 3：不该被当成 skill

cat >"$SKM_BASE_DIR/config.toml" <<EOF
version = 1

[[agents]]
id = "omp"
name = "Oh My Pi (OMP)"
dir = "$HOME/.agents/skills"
shared = true
enabled = true

[[agents]]
id = "ghost"
name = "Not Installed"
dir = "$HOME/.nope/skills"
enabled = true

[[agents]]
id = "codex"
name = "Codex CLI"
dir = "$HOME/.codex/skills"
enabled = true
EOF

# codex 目录存在（用它验证多 agent 的独立启用/停用）
mkdir -p "$HOME/.codex/skills"

pass=0; fail=0
check() { # 描述 / 期望子串 / 实际输出
  if grep -qF -- "$2" <<<"$3"; then pass=$((pass+1)); echo "  ✓ $1"
  else fail=$((fail+1)); echo "  ✗ $1"; echo "$3" | sed 's/^/      /'; fi
}
refute() { # 描述 / 不应出现的子串 / 实际输出
  if grep -qF -- "$2" <<<"$3"; then
    fail=$((fail+1)); echo "  ✗ $1（不应出现「$2」）"; echo "$3" | sed 's/^/      /'
  else pass=$((pass+1)); echo "  ✓ $1"; fi
}
assert_true() { # 描述 / 命令
  shift 0; local desc="$1"; shift
  if "$@"; then pass=$((pass+1)); echo "  ✓ $desc"
  else fail=$((fail+1)); echo "  ✗ $desc"; fi
}

echo "== list（树形、分类、深度边界）=="
out=$("$SKM" list)
check "未分类 skill" "skill-two" "$out"
check "分类显示" "cat-a/" "$out"
check "画了树形枝干" "├──" "$out"
refute "深度 3 的目录不是 skill" "nested" "$out"
check "默认全部未启用" "· skill-two" "$out"

echo "== enable 单个：建链、指向真身、幂等 =="
out=$("$SKM" enable skill-two); check "报告已启用" "已启用" "$out"
assert_true "链接存在" test -L "$HOME/.agents/skills/skill-two"
assert_true "链接指向仓库真身" test "$(readlink "$HOME/.agents/skills/skill-two")" = "$SKM_BASE_DIR/skills/skill-two"
out=$("$SKM" enable skill-two); check "重复启用为幂等" "未改动" "$out"

echo "== status：单查；批量查"
out=$("$SKM" status skill-two); check "显示 linked" "linked" "$out"
check "显示 description" "description" "$out"
out=$("$SKM" status); check "批量查缺省列全部" "skill-one" "$out"

echo "== 目标目录不存在则跳过，不创建 =="
"$SKM" enable cat-a/skill-one >/dev/null
out=$("$SKM" enable --agent ghost cat-a/skill-one); check "报告跳过" "跳过" "$out"
assert_true "没有替它创建目录" test ! -e "$HOME/.nope"

echo "== 按分类批量启用 =="
out=$("$SKM" enable --category cat-a); check "批量命中" "skill-three" "$out"
assert_true "skill-one 已启用" test -L "$HOME/.agents/skills/skill-one"
assert_true "skill-three 已启用" test -L "$HOME/.agents/skills/skill-three"

echo "== 按状态过滤 =="
refute "list --disabled 不含已启用" "skill-two" "$("$SKM" list --disabled)"
check "list --enabled 只含已启用" "skill-two" "$("$SKM" list --enabled)"

echo "== 安全：真目录占位不覆盖、清理时也不动 =="
rm -f "$HOME/.agents/skills/skill-three"
mkdir "$HOME/.agents/skills/skill-three"
echo occupancy >"$HOME/.agents/skills/skill-three/keep.txt"
out=$("$SKM" enable cat-a/skill-three); check "拒绝覆盖真目录" "拒绝" "$out"
check "issues 报告 occupied" "occupied" "$("$SKM" issues)"
check "status 报告 occupied" "occupied" "$("$SKM" status cat-a/skill-three)"
out=$("$SKM" issues --fix); refute "issues --fix 不碰真目录" "已删除" "$out"
assert_true "真目录内容完好" test -f "$HOME/.agents/skills/skill-three/keep.txt"

echo "== 安全：他人链接不覆盖 =="
rm -rf "$HOME/.agents/skills/skill-three"
ln -s "$SKM_BASE_DIR/skills/skill-two" "$HOME/.agents/skills/skill-three"
out=$("$SKM" enable cat-a/skill-three); check "拒绝覆盖他人链接" "拒绝" "$out"

echo "== 断链：默认拒绝，--force 才替换 =="
rm -f "$HOME/.agents/skills/skill-three"
ln -s "$SKM_BASE_DIR/skills/gone" "$HOME/.agents/skills/skill-three"
out=$("$SKM" enable cat-a/skill-three); check "默认拒绝替换断链" "拒绝" "$out"
out=$("$SKM" enable --force cat-a/skill-three); check "--force 成功替换" "已启用" "$out"
assert_true "断链已重指到正确真身" test "$(readlink "$HOME/.agents/skills/skill-three")" = "$SKM_BASE_DIR/skills/cat-a/skill-three"

echo "== disable：删链接、保真身 =="
out=$("$SKM" disable skill-two); check "报告已停用" "已停用" "$out"
assert_true "链接已删" test ! -e "$HOME/.agents/skills/skill-two"
assert_true "仓库真身保留" test -f "$SKM_BASE_DIR/skills/skill-two/SKILL.md"
out=$("$SKM" disable skill-two); check "重复停用为幂等" "未改动" "$out"

echo "== disable 不碰真目录 =="
mkdir -p "$HOME/.agents/skills/fake"
out=$("$SKM" disable fake 2>&1); check "真目录无对应 skill" "没有匹配的 skill" "$out"
assert_true "真目录完好" test -d "$HOME/.agents/skills/fake"

echo "== 残留链接：报告并可清理 =="
ln -s "$SKM_BASE_DIR/skills/skill-two" "$HOME/.agents/skills/leftover"
check "issues 报告残留" "leftover" "$("$SKM" issues)"
out=$("$SKM" issues --fix); check "清理残留" "已删除" "$out"
assert_true "残留链接已删" test ! -e "$HOME/.agents/skills/leftover"

echo "== glob / JSON / 退出码 =="
out=$("$SKM" enable 'skill-*'); check "glob 批量启用" "已启用" "$out"
out=$("$SKM" status skill-two --json); check "JSON 含 state" '"state": "linked"' "$out"
assert_true "JSON 可解析" python3 -c "import json,sys; json.loads(sys.argv[1])" "$out"
out=$("$SKM" enable nonexistent 2>&1); check "无匹配即报错" "没有匹配的 skill" "$out"
"$SKM" enable nonexistent >/dev/null 2>&1
assert_true "退出码为 2" test $? -eq 2

echo "== agents / config =="
out=$("$SKM" agents); check "列出 omp" "omp" "$out"
check "列出 ghost" "ghost" "$out"
check "标出目录不存在" "目录不存在" "$out"
check "config path" "config.toml" "$("$SKM" config path)"
assert_true "config init 可写" test -s "$("$SKM" config init --base-dir "$T/new" >/dev/null 2>&1; echo "$T/new/config.toml")"

echo "== 多 agent：启用写全部目标，可分别停用 =="
out=$("$SKM" enable skill-two)
check "两个目标都被启用" "codex" "$out"
assert_true "omp 链接建立" test -L "$HOME/.agents/skills/skill-two"
assert_true "codex 链接建立" test -L "$HOME/.codex/skills/skill-two"
"$SKM" disable skill-two --agent codex >/dev/null
assert_true "omp 的链接还在" test -L "$HOME/.agents/skills/skill-two"
assert_true "只删了 codex 的链接" test ! -e "$HOME/.codex/skills/skill-two"
check "status 报出部分启用" "部分启用" "$("$SKM" status skill-two)"
"$SKM" enable skill-two --agent codex >/dev/null
check "补齐后为全部启用" "omp、codex" "$("$SKM" status skill-two | sed -n 1p)"

echo "== 同名冲突（跨分类）拒绝建链 =="
mk_skill cat-b/skill-two
out=$("$SKM" enable cat-b/skill-two); check "拒绝同名建链" "拒绝" "$out"
check "status 报告链接名冲突" "link_name_collision" "$("$SKM" status)"

echo
echo "临时目录：$T"
echo "通过 $pass，失败 $fail"
[ "$fail" -eq 0 ]
