# driver-cert-patcher

Windows 图形化工具，为显示驱动模块注入自定义证书，支持备份与一键回滚。

## 功能

1. **驱动模块多选 + 智能默认勾选**: 自动扫描系统驱动存储目录下所有目标模块,
   列表显示 `inf名 [创建时间]` + 完整路径, 复选框勾选要处理的版本。
   启动时通过 WMI 查询当前实际加载的显卡驱动, 与扫描结果比对,
   匹配当前加载驱动的版本**默认勾选**。

2. **根证书**: 两种方式——
   - 点"选择证书文件"选 `.pem` 文件, 内容自动读入文本框
   - 直接在文本框粘贴证书内容
   - 打补丁前**先校验**证书 (必须含 BEGIN/END 且 base64 合法),
     通过后自动规范化并写入临时文件。

3. **自动执行**: 对每个勾选的模块依次执行
   备份原文件 → 复制临时副本 → 调用补丁核心打补丁
   → 停相关服务 → 强杀仍占用该模块的进程
   → 获取所有权/授权 → 覆盖 → 重启服务。

   > 驱动正被显卡占用时直接覆盖会失败, 故停服务后用 psutil 遍历进程
   > 精确找出仍加载该模块的进程并强杀, 再覆盖。

4. **完整备份 + 一键恢复**: 每次打补丁前自动备份, 覆盖失败/服务异常可回滚。
   - 备份位置: 程序同目录 `backup/<unix时间戳>/`, 保留驱动存储相对路径结构。
   - 每次会话写 `manifest.json` 记录 "原始路径 <-> 备份文件" 映射。
   - 底部【备份】下拉框列出所有历史备份会话, 选中后点【恢复选中备份】即可。
   - 恢复前会校验备份文件完整性 (缺失则拒绝), 并二次确认。

5. **非阻塞执行**: 打补丁/恢复的耗时操作全部在**子线程**执行,
   界面保持响应, 进度与日志实时更新。

## 目录结构

```
driver-cert-patcher/
├── patcher_gui.py               # 主程序 (Tkinter GUI)
├── gridd-unlock-patcher.exe     # 补丁核心 (编译产物, 不入库)
├── gridd-unlock-patcher-mini.c  # 补丁核心源码 (纯 C, 零第三方依赖)
├── build.bat                    # 打包主程序脚本 (PyInstaller)
├── compile_patcher.bat          # 编译补丁核心脚本 (MinGW64)
├── README.md                    # 本文件
├── LICENSE                      # MIT
├── .gitignore                   # 忽略运行时产物/本地文档
├── backup/                      # (运行时自动创建) 每次打补丁的备份会话
│   └── <unix时间戳>/
│       ├── manifest.json
│       └── <相对目录结构>/nvxdapix.dll
└── release/                     # (打包后生成) 最终发版产物
    ├── NVIDIA_GRID_Driver_Patcher.exe
    └── gridd-unlock-patcher.exe
```

> 主程序已内嵌 requireAdministrator 权限 manifest, 双击运行即自动触发 UAC 提权。

## 编译 (开发人员)

本工具有**两个独立的可编译产物**, 需要**两套不同的编译环境**:

| 产物 | 源码 | 语言 | 编译工具 | 产物大小 |
|---|---|---|---|---|
| 补丁核心 | `gridd-unlock-patcher-mini.c` | 纯 C | MinGW64 GCC (交叉编译) | ~284 KB |
| 主程序 (GUI) | `patcher_gui.py` | Python 3 | PyInstaller | ~11.8 MB |

> 主程序会把补丁核心通过 `--add-data` 打进单文件, 所以**必须先编译补丁核心, 再打包主程序**。

---

### 1. 编译补丁核心 (C → MinGW64)

**编译什么**: `gridd-unlock-patcher-mini.c` (约 300 行纯 C, 零第三方依赖, 仅用标准库
`stdio.h` / `stdlib.h` / `string.h` / `stdint.h`)
**产物**: `gridd-unlock-patcher.exe` (静态链接单文件, 仅依赖 Windows 系统 DLL)

#### 环境搭建

需要 **MinGW64 交叉编译器** (`x86_64-w64-mingw32-gcc`)。两种获取方式:

**方式 A: 通过 Cygwin 安装 (推荐, 脚本默认查找路径)**
1. 安装 [Cygwin](https://www.cygwin.com/) (默认装到 `C:\cygwin64`, 或自定义路径)
2. 在 Cygwin Setup 里勾选 `mingw64-x86_64-gcc` 包
3. 安装后编译器位于 `<Cygwin路径>\bin\bin\x86_64-w64-mingw32-gcc.exe`
   - 注意是**双层 bin**: Cygwin 自身一层 + MinGW 一层
4. `compile_patcher.bat` 默认查找 `D:\Cygwin\bin\bin\x86_64-w64-mingw32-gcc.exe`,
   如果你的 Cygwin 装在别处, 改脚本顶部的路径, 或把编译器加到系统 PATH

**方式 B: 独立 MinGW64 (不依赖 Cygwin)**
1. 下载 [WinLibs MinGW64](https://winlibs.com/) 或 [msys2](https://www.msys2.org/)
2. 安装后把 `\bin` 目录加到系统 PATH
3. 验证: 在 cmd 里跑 `x86_64-w64-mingw32-gcc --version` 能输出版本号
4. `compile_patcher.bat` 的 `where` 命令会自动从 PATH 找到

#### 编译命令

```bat
cd /d 工程根目录
compile_patcher.bat
```

等价手动命令:
```bat
x86_64-w64-mingw32-gcc -O2 -Wall -o gridd-unlock-patcher.exe gridd-unlock-patcher-mini.c
```

**验证**: 编译后 `gridd-unlock-patcher.exe` 约 284 KB, 运行 `gridd-unlock-patcher.exe -h`
应输出帮助信息。

---

### 2. 打包主程序 (Python → PyInstaller)

**编译什么**: `patcher_gui.py` (Tkinter GUI, 约 800 行)
**产物**: `release\NVIDIA_GRID_Driver_Patcher.exe` (单文件, 含 UAC manifest + 内嵌补丁核心)

#### 环境搭建

需要 **Python 3.x** (3.8+) 且带 **tkinter**。

1. 安装 [Python 3.x](https://www.python.org/downloads/)
   - 安装时**勾选 "Add Python to PATH"**
   - tkinter: 官方 Windows 安装包自带, 无需额外安装
   - 验证: `python -c "import tkinter; print('ok')"`
2. 安装 **PyInstaller** (build.bat 会自动装, 也可手动):
   ```bat
   python -m pip install --upgrade pyinstaller
   ```
   - 国内网络慢可走清华镜像:
   ```bat
   python -m pip install --upgrade pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn
   ```

> **注意**: 如果机器上同时有 Python 2.7 和 3.x, 确保 `python` 命令指向 3.x。
> 或在运行 build.bat 前 `set PYTHON=C:\path\to\python3xx.exe` 指定。

#### 打包命令

```bat
cd /d 工程根目录
build.bat
```

build.bat 自动完成:
1. 检测 Python 3.x (找不到则报错提示)
2. 检查 tkinter / PyInstaller (缺 PyInstaller 自动从清华镜像安装)
3. 检查 `gridd-unlock-patcher.exe` 是否存在 (不存在则报错, 提示先跑 `compile_patcher.bat`)
4. 清理旧中间文件 (build/ dist/ *.spec __pycache__)
5. PyInstaller 打包:
   ```
   --onefile       单文件
   --windowed      无控制台窗口 (GUI 程序)
   --uac-admin     内嵌 requireAdministrator manifest (双击自动 UAC 提权)
   --add-data      把 gridd-unlock-patcher.exe 打进产物
   ```
6. 产物 + 补丁核心 复制到 `release/`
7. 清理所有中间文件

**验证**: 打包后 `release\NVIDIA_GRID_Driver_Patcher.exe` 约 11.8 MB,
双击运行会弹 UAC 提权框。

---

### 3. 完整编译流程 (从零到发版)

```
第一步: 编译补丁核心
  x86_64-w64-mingw32-gcc -O2 -Wall -o gridd-unlock-patcher.exe gridd-unlock-patcher-mini.c
  → 产出 gridd-unlock-patcher.exe (~284 KB)

第二步: 打包主程序
  python -m PyInstaller --onefile --windowed --uac-admin ^
      --name "NVIDIA_GRID_Driver_Patcher" ^
      --add-data "gridd-unlock-patcher.exe;." ^
      --distpath "dist" --workpath "build" --specpath "." ^
      patcher_gui.py
  → 产出 dist\NVIDIA_GRID_Driver_Patcher.exe (~11.8 MB)

第三步: 整理发版
  move dist\NVIDIA_GRID_Driver_Patcher.exe release\
  copy gridd-unlock-patcher.exe release\
  rmdir /s /q build dist __pycache__ & del *.spec
  → release/ 下两个 exe 即为最终发版产物
```

> 全程使用相对路径 (`cd /d %~dp0` 切到工程根), 工程目录可放在含中文的路径下。


### 4. GitHub Actions 自动构建 (推荐)

仓库已配置 [GitHub Actions](.github/workflows/build.yml) 工作流,
**打 tag 即可自动编译 + 发布 Release**, 无需本地搭建任何环境。

#### 触发方式

- **自动**: 推送以 `v` 开头的 tag (如 `v1.0`, `v1.1`, `v2.0`)
- **手动**: 在 GitHub 仓库 Actions 页面点 "Run workflow"

#### 工作流步骤

```
build job (windows-latest):
  1. Checkout 源码
  2. MinGW64 编译补丁核心 (x86_64-w64-mingw32-gcc)
  3. 验证 -h 输出
  4. Setup Python 3.12
  5. 安装 PyInstaller
  6. PyInstaller 打包主程序 (--onefile --windowed --uac-admin)
  7. 整理 release artifacts
  8. 上传 artifact (补丁核心 + 主程序)

release job (ubuntu-latest):
  1. 下载两个 artifact
  2. 验证文件完整性
  3. 创建 GitHub Release + 上传附件
```

#### 发布流程

```bash
# 1. 修改代码
# 2. 提交 + 推 main
git add -A && git commit -m "feat: xxx" && git push origin main

# 3. 打 tag (触发自动构建)
git tag -a v1.1 -m "v1.1: xxx"
git push origin v1.1

# 4. 等待 ~1-2 分钟, 去 Releases 页面下载
```

> GitHub runner (`windows-latest`) 自带 MinGW64 和 Python, 无需额外安装。
> 构建产物: `NVIDIA_GRID_Driver_Patcher.exe` (~11 MB) + `gridd-unlock-patcher.exe` (~60 KB)。

## 使用 (最终用户)



1. 把 `release\NVIDIA_GRID_Driver_Patcher.exe` 拷贝到目标 Windows 虚拟机。
2. 双击运行 (自动弹出 UAC 提权框, 点"是")。
3. 在列表勾选要打补丁的模块版本。
4. 提供根证书: 选文件, 或直接粘贴证书全文。点"校验证书"确认变绿。
5. 点"开始打补丁", 等待日志跑完。
6. **重启虚拟机**使补丁生效。

## 依赖

- **运行**: 仅 Windows 自带 (tkinter / PyInstaller 已打入单文件), 无需额外安装。
- **编译补丁核心**: MinGW64 GCC (`x86_64-w64-mingw32-gcc`), 见上方"编译"章节。
- **编译主程序**: Python 3.8+ (带 tkinter) + PyInstaller, 见上方"编译"章节。
- **补丁核心**: 纯 C 最小 PE 解析, MinGW64 编译, 零第三方依赖, 仅依赖 Windows 系统 DLL。

## 安全说明

- 工具会**备份**原始模块后再覆盖, 可一键还原。
- 仅操作驱动存储目录下的目标模块及相关服务。
- 打补丁前的证书校验可避免把错误内容写入驱动。

## 免责声明

本工具用于**自有环境**的驱动模块证书定制, 修改显卡驱动可能导致系统不稳定或显卡功能异常。
使用者应充分了解风险并自行备份。作者不对任何数据丢失或系统故障负责。
请确保你有权修改目标系统, 且使用本工具不违反当地法律法规及服务条款。

## License

[MIT](LICENSE)
