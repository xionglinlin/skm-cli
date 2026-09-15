# skm — 本地 Skill 仓库管理器

管理本地 skill 仓库，控制每个 skill 对各个 AI agent（OMP / Codex / Claude Code …）
是**启用**还是**停用**。

```
「启用」= 在 agent 的 skills 目录里建一条指向仓库真身的符号链接
「停用」= 删掉那条链接
```

真身（含分类目录）始终留在自己的仓库里，不会被移动或复制。

```text
  仓库（真身）                     agent 目录（链接）
  ~/.skill-manager/skills/         ~/.agents/skills/
    pdf/            ──链接──►        pdf -> …/skills/pdf
    qt-skills/                       qt-qml -> …/skills/qt-skills/qt-qml
      qt-qml/
```

## 它解决什么问题

你可能同时用 OMP、Codex、Claude Code，而它们各自有一套 skill 目录。
skill 越攒越多时会出现三个麻烦：

- **全开**：每个 agent 的上下文里都塞满用不上的 skill 元信息；
- **全关**：想临时用某个 skill 时要去手动 `ln -s` / `rm`，还容易敲错路径；
- **散乱**：同一份 skill 在多个 agent 目录下各存一份真身，改一处不生效、清理时又怕删错。

skm 的取舍是：**真身只有一份，放在你自己的仓库里；对 agent 的可见性完全由符号链接表达。**

## 设计要点

**文件系统就是唯一真相。** skm 不保存任何「启用状态」。因此不存在
「配置说要启用、实际却没有」的分裂 —— 别的工具动了目录，下次查询立刻如实反映。

**分类就是目录，不引入元数据。** 深度固定两层，不发明配置文件来表达分类：

```text
<仓库>/<skill>/SKILL.md             → 未分类
<仓库>/<分类>/<skill>/SKILL.md       → 归入该分类
```

**绝不覆盖不属于自己的东西。** 目标位置被真目录占住、或链接指向别的 skill 时，
操作被拒绝并报告 —— 那可能是别的工具（`npx skills` 等）的数据。断链也需
显式 `--force` 才替换。停用只删除「解析后正是本 skill 真身」的链接。

**目标目录不存在就跳过**，不替 agent 创建目录。

## 安装

需要 **Python 3.11+**（用到标准库 `tomllib`）与 Linux/macOS。
无第三方依赖。

### 方式一：克隆后一键安装（推荐）

```bash
git clone https://github.com/xionglinlin/skm-cli.git
cd skm-cli
bash install.sh                 # 默认软链到 ~/.local/bin
```

```bash
# 其它模式
bash install.sh --binary        # 自包含安装，之后可删掉克隆目录
bash install.sh --copy          # 生成包装脚本（不依赖 PATH 里的软链）
bash install.sh --dir ~/bin     # 装到别的目录
bash install.sh --repo ~/my-skills   # 换个仓库位置并生成配置
bash install.sh --uninstall     # 卸载（默认不动数据目录）
```

`--link` 模式装的是符号链接，所以升级只需：

```bash
cd skm-cli && git pull
```

### 方式二：pipx（隔离的独立包）

```bash
pipx install git+https://github.com/xionglinlin/skm-cli.git
```

> 注意：如果克隆目录路径里含 `@`（例如用户名带 `@`），`pipx install .`
> 会把它误解析成 URL。这种情况请用 `git+https://` 形式，或用 `bash install.sh`。

### 方式三：不安装，直接跑

```bash
git clone https://github.com/xionglinlin/skm-cli.git
./skm-cli/bin/skm list
```

## 快速开始

```bash
# 1. 首次生成配置文件（可选；不生成也能用内置默认值）
skm config init

# 2. 把 skill 放进仓库
mkdir -p ~/.skill-manager/skills/pdf
cp -r /path/to/pdf-skill/* ~/.skill-manager/skills/pdf/     # 需含 SKILL.md

# 分类就是中间加一层目录
mkdir -p ~/.skill-manager/skills/qt-skills/qt-qml

# 3. 看看仓库里有什么、现在什么状态
skm list

# 4. 启用
skm enable pdf
skm enable --category qt-skills

# 5. 重启 agent 生效（skill 在启动时发现）
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `skm list` | 树形列出全部 skill 及启用状态 |
| `skm status` | 查看详细状态（每个目标目录里那块位置到底是什么） |
| `skm enable` / `on` | 启用（建链接） |
| `skm disable` / `off` | 停用（删链接，真身保留） |
| `skm agents` | 列出配置里的 agent 及其加载目录 |
| `skm config` | 查看 / 生成配置 |
| `skm issues` / `doctor` | 报告残留链接与冲突 |

标识可以是**目录名**、**分类/目录名**（`qt-skills/qt-qml`）或 **glob**（`qt-*`）。

### 查询

```bash
skm list                            # 树形列出全部
skm list --enabled                  # 只看已启用的
skm list --disabled 'qt-*'          # 只看「qt- 开头」里未启用的
skm list --category security-skills # 只看某个分类
skm status pdf qt-qml               # 批量查看详细状态
skm status --json | jq '.skills[] | select(.enabled)'   # 机器可读
```

`list` 输出形如（`✓` 已启用、`~` 部分启用、`·` 未启用）：

```text
仓库：/home/me/.skill-manager/skills
├── · find-skills              未启用
├── ✓ pdf                      omp
└── qt-skills/  12 个 · 已启用 1
    ├── · qt-cmake-project     未启用
    └── ✓ qt-qml               omp
```

### 启用 / 停用

```bash
skm enable pdf                      # 单个
skm enable pdf docx qt-qml          # 多个
skm enable --category qt-skills     # 整个分类
skm enable 'dbus-*'                 # glob
skm enable --all                    # 仓库内全部
skm enable --all --disabled         # 只把还没启用的启用（= 全部启用）
skm disable --all                   # 全部停用（真身不动）
skm disable pdf --agent codex       # 只对某个 agent 停用
skm enable pdf -n                   # 预演，不实际改（--dry-run）
```

### 排查

```bash
skm issues          # 看残留链接与冲突
skm issues --fix    # 只清理「能证明属于本工具的」残留链接
```

退出码：`0` 成功 · `1` 有操作被拒绝或失败 · `2` 用法错误或查询无结果。

## 接入新的 agent

改配置即可，不用改代码。`skm config init` 生成的
`~/.skill-manager/config.toml`：

```toml
version = 1
base_dir = "/home/me/.skill-manager"

[[agents]]
id = "omp"
name = "Oh My Pi (OMP)"
dir = "~/.agents/skills"    # 该 agent 读取 skill 的目录
shared = true               # 与别的 agent 共用同一目录
enabled = true

# 取消注释即可接入（目录不存在时会被安全跳过）
# [[agents]]
# id = "codex"
# dir = "~/.codex/skills"
```

- `dir` 支持 `~` 与 `$VAR` 展开；
- `shared = true` 表示多个 agent 共用同一目录 —— 动一次影响全部，
  skm 只写一次但会为每个 agent 报告结果；
- `enabled = false` 只是不再向它铺链接，**不会**删除已有链接；
- 两个 agent 配了同一 `dir` 时会被识别为共用，并在输出中提示。

确认目标目录确实存在后再启用；`skm agents` 会标出目录是否存在：

```bash
$ skm agents
  omp      Oh My Pi (OMP)  /home/me/.agents/skills
           目录存在  (共享)
  codex    Codex CLI       /home/me/.codex/skills
           目录存在
```

> 各 agent 的实际加载路径以其自身版本为准，接入前建议先确认目录确实被它读取。

## 环境变量

| 变量 | 作用 |
|---|---|
| `SKM_BASE_DIR` | 仓库与配置的根目录（默认 `~/.skill-manager`） |
| `SKM_CONFIG` | 直接指定配置文件路径 |
| `SKM_PYTHON` | 指定 python3 解释器 |

## 支持的状态

`skm status` 会如实报出每个目标位置的实际状态，而不是简单的是/否：

| 状态 | 含义 | 能否自动处理 |
|---|---|---|
| `linked` | 是指向本 skill 的链接 | — |
| `absent` | 空位（正常未启用） | — |
| `broken` | 断链 | `enable --force` 可重指 |
| `elsewhere` | 链接指向别的 skill | **否**，只报告 |
| `occupied` | 位置被真目录/真文件占住 | **否**，只报告 |

## 开发

```bash
bash tests/e2e.sh      # 56 项端到端验证，全部在临时 HOME 里跑，不碰真实数据
```

源码结构：

```text
src/skm/
  model.py     skill 模型、SKILL.md frontmatter 解析、链接状态判定
  scan.py      只读扫描：仓库 + 各 agent 目录 → 一份事实快照
  actions.py   写操作：建链 / 删链（含全部安全判定）
  config.py    配置读写
  cli.py       命令行
```

## 许可

GPL-3.0-or-later，见 [LICENSE](LICENSE)。
