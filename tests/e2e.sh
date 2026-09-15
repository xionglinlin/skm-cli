#!/usr/bin/env bash
# skm 端到端验证（隔离环境，不触碰真实数据）
#
# 用法：bash tests/e2e.sh
# 覆盖：列表/树形、单查与批查、启用/停用、幂等、冲突保护（真目录/他人链接/断链）、
#       残留链接清理、多 agent 与 --agent 定向、glob、JSON、退出码、
#       import 收编（计划/执行/幂等/拒绝/--copy/软链根/原地补链接）。
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
out=$("$SKM" status skill-two); check "显示已启用" "已启用" "$out"
check "显示 description" "test skill skill-two" "$out"
check "显示真身位置" "skills/skill-two" "$out"
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
check "issues 报告被占位（中文）" "[被占位]" "$("$SKM" issues)"
check "issues --json 保留稳定标识" '"kind": "occupied"' "$("$SKM" issues --json)"
check "status 报告被占位" "被占位" "$("$SKM" status cat-a/skill-three)"
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

echo "== 空仓库：list 给出可执行的下一步 =="
T2=$(mktemp -d /tmp/skm-e2e-empty-XXXXXX)
export SKM_BASE_DIR="$T2/repo"; export HOME="$T2/home"
mkdir -p "$HOME/.agents/skills" "$SKM_BASE_DIR/skills"
out=$("$SKM" list); check "空仓库时提示 skm import" "skm import" "$out"
out=$("$SKM" status); check "空仓库时 status 也给提示" "skm import" "$out"

echo "== import：扫 agent 目录 → 入库 + 原位置补链接 =="
mk_agent_skill() { # $1 = agent 目录下的相对路径
  local p="$HOME/.agents/skills/$1"
  mkdir -p "$p"; local n; n=$(basename "$p")
  printf -- '---\nname: %s\ndescription: agent skill %s\n---\n' "$n" "$n" >"$p/SKILL.md"
}
mk_agent_skill straight
mk_agent_skill cat-x/layered
out=$("$SKM" import); check "默认只出计划" "将被收进仓库" "$out"
check "计划说明会补链接" "补链接" "$out"
assert_true "计划阶段未搬动真身" test -f "$HOME/.agents/skills/straight/SKILL.md"
assert_true "计划阶段仓库仍空" test -z "$(ls -A "$SKM_BASE_DIR/skills")"

out=$("$SKM" import --yes); check "执行报告已搬动" "已搬动" "$out"
assert_true "真身已入库（未分类）" test -f "$SKM_BASE_DIR/skills/straight/SKILL.md"
assert_true "真身已入库（分层→分类）" test -f "$SKM_BASE_DIR/skills/cat-x/layered/SKILL.md"
assert_true "agent 侧补了链接（扁平）" test -L "$HOME/.agents/skills/straight"
assert_true "agent 侧补了链接（分层源→规范位置）" test -L "$HOME/.agents/skills/layered"
assert_true "链接指向仓库真身" test "$(readlink "$HOME/.agents/skills/layered")" = "$SKM_BASE_DIR/skills/cat-x/layered"
assert_true "源被搬走后不留空分类目录" test ! -e "$HOME/.agents/skills/cat-x"
assert_true "agent 侧内容可读（链接有效）" test -f "$HOME/.agents/skills/layered/SKILL.md"
check "收编后 list 认到并算已启用" "✓ layered" "$("$SKM" list)"
check "收编后无残留/冲突" "没有发现残留" "$("$SKM" issues)"

echo "== import 幂等：再扫一遍无事可做 =="
# 此时 agent 目录里只剩链接（不是真身）→ 没有任何候选，计划为空
out=$("$SKM" import --json); check "第二次扫描没有任何候选" '"items": []' "$out"
out=$("$SKM" import); check "第二次扫描明确说无事可做" "没有需要收编的 skill" "$out"

echo "== import：不覆盖 + 拒绝项 =="
mkdir -p "$T2/dl/dup"; printf -- '---\nname: dup\ndescription: x\n---\n' >"$T2/dl/dup/SKILL.md"
out=$("$SKM" import "$T2/dl/dup" --yes); check "指定路径可收编" "已搬动" "$out"
assert_true "源已搬走" test ! -e "$T2/dl/dup"
mkdir -p "$T2/dl2/dup"; printf -- '---\nname: dup\ndescription: y\n---\n' >"$T2/dl2/dup/SKILL.md"
"$SKM" import "$T2/dl2/dup" >/dev/null 2>&1
assert_true "仓库已有同名时退出码为 1" test $? -eq 1
assert_true "同名冲突时源未被动" test -f "$T2/dl2/dup/SKILL.md"
assert_true "同名冲突时仓库未被覆盖" grep -qF "description: x" "$SKM_BASE_DIR/skills/dup/SKILL.md"

echo "== import：--copy 保留源；agent 目录内不给复制（避免第二份真身）=="
mkdir -p "$T2/dl3/copy-me"; printf -- '---\nname: copy-me\ndescription: x\n---\n' >"$T2/dl3/copy-me/SKILL.md"
out=$("$SKM" import "$T2/dl3/copy-me" --copy --yes); check "复制模式报告已复制" "已复制" "$out"
assert_true "复制模式源保留" test -f "$T2/dl3/copy-me/SKILL.md"
assert_true "复制模式副本入库" test -f "$SKM_BASE_DIR/skills/copy-me/SKILL.md"
mk_agent_skill copy-no
out=$("$SKM" import --copy); check "拒绝复制 agent 目录内的源" "拒绝" "$out"
check "说明为什么拒绝" "第二份真身" "$out"
assert_true "拒绝后真身仍在原地" test -f "$HOME/.agents/skills/copy-no/SKILL.md"

echo "== import：链接不是真身，跳过不搬 =="
out=$("$SKM" import); refute "不搬链接" "已搬动" "$out"
assert_true "链接未被当作 skill" test -L "$HOME/.agents/skills/straight"

echo "== import：指定分类；--dry-run 压过 --yes =="
mkdir -p "$T2/dl4/boxed"; printf -- '---\nname: boxed\ndescription: x\n---\n' >"$T2/dl4/boxed/SKILL.md"
"$SKM" import "$T2/dl4/boxed" --category qt-skills --yes >/dev/null
assert_true "收进指定分类" test -f "$SKM_BASE_DIR/skills/qt-skills/boxed/SKILL.md"
mkdir -p "$T2/dl5/keepme"; printf -- '---\nname: keepme\ndescription: x\n---\n' >"$T2/dl5/keepme/SKILL.md"
"$SKM" import "$T2/dl5/keepme" --yes --dry-run >/dev/null
assert_true "--dry-run 时 --yes 不生效" test -f "$T2/dl5/keepme/SKILL.md"
assert_true "--dry-run 未入库" test ! -e "$SKM_BASE_DIR/skills/keepme"

echo "== import：显式路径不在配置的 agent 目录下 → 明确告警 =="
mkdir -p "$T2/uncovered/gamma"; printf -- '---\nname: gamma\ndescription: x\n---\n' >"$T2/uncovered/gamma/SKILL.md"
out=$("$SKM" import "$T2/uncovered" 2>&1)
check "告警：不会回填链接" "不会回填链接" "$out"

echo "== import：JSON 可解析 =="
out=$("$SKM" import --json)
assert_true "import --json 可解析" python3 -c "import json,sys; json.loads(sys.argv[1])" "$out"

echo "== import：agent 根目录是软链（dotfiles 式布局）=="
T3=$(mktemp -d /tmp/skm-e2e-link-XXXXXX)
mkdir -p "$T3/home/.agents" "$T3/repo/skills" "$T3/real/skills"
ln -s "$T3/real/skills" "$T3/home/.agents/skills"
mkdir -p "$T3/real/skills/cat/x" "$T3/real/skills/flat"
printf -- '---\nname: x\ndescription: x\n---\n' >"$T3/real/skills/cat/x/SKILL.md"
printf -- '---\nname: flat\ndescription: x\n---\n' >"$T3/real/skills/flat/SKILL.md"
out=$(SKM_BASE_DIR="$T3/repo" HOME="$T3/home" "$SKM" import --yes 2>&1)
check "软链根下也能收编" "已搬动" "$out"
assert_true "扁平源原地补链接" test -L "$T3/real/skills/flat"
assert_true "分类源在规范位置补链接" test -L "$T3/real/skills/x"
assert_true "空分类目录已收掉" test ! -e "$T3/real/skills/cat"
assert_true "没有误删 agent 根之外的上层" test -d "$T3/real"
check "收编后 list 全部已启用" "已启用 2" \
  "$(SKM_BASE_DIR="$T3/repo" HOME="$T3/home" "$SKM" list)"

echo "== import：链接位被占住 → 拒绝（搬走会让 agent 读不到）=="
T4=$(mktemp -d /tmp/skm-e2e-occ-XXXXXX)
mkdir -p "$T4/home/.agents/skills/cat/pdf" "$T4/repo/skills"
printf -- '---\nname: pdf\ndescription: x\n---\n' >"$T4/home/.agents/skills/cat/pdf/SKILL.md"
mkdir -p "$T4/home/.agents/skills/pdf"          # 规范链接位是别的真目录
printf -- '---\nname: pdf\ndescription: other\n---\n' >"$T4/home/.agents/skills/pdf/SKILL.md"
out=$(SKM_BASE_DIR="$T4/repo" HOME="$T4/home" "$SKM" import 2>&1)
check "占位时拒绝收编" "拒绝" "$out"
check "说明搬走会让 agent 读不到" "读不到这个 skill" "$out"
assert_true "被拒后源真身未动" test -f "$T4/home/.agents/skills/cat/pdf/SKILL.md"

echo "== status 显示契约（中文状态、~ 缩写、宽度、区间作用域）=="
# 这些是显示层真实易错的点：宽字符对齐、家目录缩写、超宽折行、冲突计数作用域。
T5=$(mktemp -d /tmp/skm-e2e-fmt-XXXXXX)
export SKM_BASE_DIR="$T5/repo"; export HOME="$T5/home"
mkdir -p "$HOME/.agents/skills" "$SKM_BASE_DIR/skills"
mk_skill5() { # $1 = 仓库内相对路径
  local p="$SKM_BASE_DIR/skills/$1"; mkdir -p "$p"; local n; n=$(basename "$p")
  printf -- '---\nname: %s\ndescription: 中文描述%s，用来验证宽字符列宽\n---\n' "$n" "$n" >"$p/SKILL.md"
}
mk_skill5 alpha; mk_skill5 cat-b/beta
"$SKM" enable --all >/dev/null
# 让 beta 在 omp 上被真目录占住
rm -f "$HOME/.agents/skills/beta"; mkdir "$HOME/.agents/skills/beta"

out=$("$SKM" status alpha)
check "状态用中文（不含英文枚举）" "已启用" "$out"
refute "界面不出现 linked 枚举" "linked" "$out"
check "家目录缩写为 ~" "~/.agents/skills/alpha" "$out"
refute "不再显示绝对家目录路径" "$HOME/.agents/skills/alpha" "$out"
check "显示真身位置" "skills/alpha" "$out"
check "name 与目录名一致时不重复显示 name 行" "已启用" "$out"

# 超宽输出应折行而不是溢出：所有行不超过终端宽度
long=$("$SKM" status beta)
assert_true "输出不超宽（COLUMNS=60）" bash -c "COLUMNS=60 '$SKM' status | awk 'length(\$0)>60 {exit 1}'"
check "长文本折行后仍在缩进内" "被占位" "$long"

# 冲突计数只统计本次列出的 skill
out=$("$SKM" status alpha)
refute "单查时不误报未列出 skill 的冲突" "属于本次未列出的 skill" "$out"
out=$("$SKM" status)
check "全量查询报出冲突数" "处冲突已在上面标出" "$out"

# 折行：超宽内容必须自己折行（不能让终端折，否则续行从第 0 列开始、缩进全乱）
assert_true "窄终端下没有行超宽（COLUMNS=64）" bash -c \
  "COLUMNS=64 '$SKM' status | awk 'length(\$0)>64 {exit 1}'"
check "窄终端下仍能看清状态" "被占位" "$(COLUMNS=64 "$SKM" status beta)"

echo "== 筛选滤空 vs 仓库真空（提示不同）=="
check "滤空时提示筛选" "没有符合筛选条件" "$("$SKM" status --enabled --category nope)"
check "滤空时 list 提示筛选" "没有可显示的 skill" "$("$SKM" list --category nope)"

# 恢复主场景的环境，后续用例继续用
export SKM_BASE_DIR="$T/repo"; export HOME="$T/home"

echo
echo "临时目录：$T"
echo "通过 $pass，失败 $fail"
[ "$fail" -eq 0 ]
