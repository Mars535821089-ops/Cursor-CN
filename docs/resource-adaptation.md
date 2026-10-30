# Cursor 3.27.22 资源适配：验证完成，未开放安装

## 官方样本与信任证据

2026-10-30，从 [Cursor 官方下载页](https://cursor.com/download) 的 3.27 Latest / Mac ARM64 链接取得重定向，下载到临时目录，只读挂载后检查，未执行应用：

| 项目 | 实际值 |
| --- | --- |
| 官方样本 URL | `https://downloads.cursor.com/production/b1aa50cb6db84fd3e8c808259cb172ea73425c1f/darwin/arm64/Cursor-darwin-arm64.dmg` |
| DMG 大小 | 315,918,342 字节 |
| DMG SHA-256 | `fd9288d67f17d8510d19b9aa3daf8204c636377f3748273bc0fbdd38d6919580` |
| 应用精确版本 | 3.27.22 / macOS ARM64 / stable |
| product.json commit | `b1aa50cb6db84fd3e8c808259cb172ea73425c10` |
| product.json date | `2026-10-27T00:33:02.045Z` |
| Developer ID | Hilary Stout / Team ID `VDXQ22DGB9`，链至 Apple Root CA |
| 原始签名 | `codesign --verify --deep --strict` 通过 |
| 装订公证票据 | `xcrun stapler validate` 通过 |
| Gatekeeper 限制 | 本机既有状态为 `assessments disabled`；`spctl` 显示 accepted，但带 `override=security disabled`，不能单独作为完整安全评估证据 |

没有修改 Gatekeeper/SIP/授权状态、重签名、绕过票据验证或启动样本。下载路径标识与 `product.json` commit 的末尾不同，因此逐项记录原始值，不猜测它们相等。文件哈希只锁定本次样本，不等于信任任意同版本包。

## 实现与验证

- `scripts/audit_ui.cjs --profile` 可以针对显式构建 profile 扫描；本轮解析 69 个资源：46 个打包 `out/` JS 和 23 个 Cursor 自有扩展 JSON，盘点全部 116 个内置扩展清单。
- `scripts/adapt_resources.py build` 要求样本挂载于指定 DMG 且为只读，并校验深度签名、预期签名者和装订票据。新 profile 必须使用独立版本文件名，不能覆盖旧安装 profile。
- `patches/reviewed-ui-3.27.json` 单独保存新增审核词条，涵盖登录失效、系统后台更新权限、自动化筛选与 Webhook、SpaceX Airlock 容器错误、受管理部署、附件失败和堆快照隐私说明。合并既有审核词典时，冲突和占位符多重集变化均报错；旧 3.24.18 词典及支持范围不扩大。
- `patches/resources-3.27.22-arm64.json.gz` 包含 **28 个资源、1,119 处**完整字符串字面量差异。仅改 AST 识别的界面位置；每个输入/输出均锁定大小及 SHA-256，不修改命令 ID、配置键、控制流或字符串以外的 token。
- NLS 先使用原有已锁定 SHA-256 的官方简体中文语言包（版本 `1.128.2026071013`）重建 **11,432 条**译文，再补充 **1,624 条**：1,068 条命中审核词典，556 条仅在原文 SHA-256 完全相同的情况下迁移旧审核结果。**不复用旧索引**，也不把旧运行时整包注入新版。22 个官方词条的占位符不匹配，明确拒绝并计数；之后仍可由审核词典或精确原文迁移安全补充。
- `patches/nls-3.27.22.json` 只保存官方语言包之外的差异及最终 NLS 哈希，不复制完整官方语言包。`profile-3.27.22.json` 记录全部扫描输入指纹、来源、审核差异及剩余清单的 artifact 哈希。
- `scripts/adapt_resources.py preview` 逐个检查输入资源后，仅在内存重建源码和 NLS。`scripts/verify_ui_review.cjs` 再次进行 AST 重解析，28 个目标的非字符串 token 和字符串节点数量不变，已审核源文不再残留。
- 已提交的 [真实资源验证报告](resource-verification-3.27.22.json) 仅含版本、相对路径、修正计数及前后指纹，不含应用全文或私人路径。
- 52 项单元测试覆盖官方译文优先、占位符重复次数、旧索引不能迁移、冲突拒绝、只读挂载、签名/公证失败、关闭 Gatekeeper 的显式记录、未知指纹拒绝、预览不写入，以及原有安装/恢复回归。发布检查校验新 profile 的全部 artifact。

## 重现方法

维护者自行从上述官方地址下载 DMG，验证 SHA-256 后只读挂载；语言包使用旧 profile 中锁定的 VSIX。不要把应用、DMG、VSIX 或完整源码候选报告提交到仓库。

```bash
node --max-old-space-size=12000 scripts/audit_ui.cjs \
  --app "/path/to/Read-only Volume/Cursor.app" \
  --profile patches/profile-3.27.22.json --output "/path/to/new-audit.json"

python3 scripts/adapt_resources.py build \
  --app "/path/to/Read-only Volume/Cursor.app" \
  --profile "$PWD/patches/profile-3.27.22.json" \
  --audit "/path/to/new-audit.json" --vsix "/path/to/Pinned Language Pack.zip" \
  --dmg "/path/to/Cursor-darwin-arm64.dmg" \
  --source-url "https://downloads.cursor.com/production/b1aa50cb6db84fd3e8c808259cb172ea73425c1f/darwin/arm64/Cursor-darwin-arm64.dmg"

python3 scripts/adapt_resources.py preview \
  --app "/path/to/Read-only Volume/Cursor.app" \
  --profile patches/profile-3.27.22.json --vsix "/path/to/Pinned Language Pack.zip"

node --max-old-space-size=12000 scripts/verify_ui_review.cjs \
  --app "/path/to/Read-only Volume/Cursor.app" \
  --profile patches/profile-3.27.22.json --output "/path/to/new-verification.json"

python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
```

审核候选报告包含程序文案，只在本地保留。预览仅输出位置、修改计数及前后指纹，不输出整包源码。若传入旧 3.24 应用或样本有任意指纹变化，立即报错且不修改任何文件。

## 剩余工作和不可推出的结论

`patches/remaining-3.27.22.json` 是带哈希的剩余候选清单，不将候选计数冒充漏译数量：

| 类别 | 剩余范围 |
| --- | --- |
| 静态英文候选 | 14,264 个 AST 位置，按文件列计数；含品牌、诊断、协议和未审核显示文案 |
| 动态界面模板 | 1,023 个，记录文件、UTF-16 位置、片段哈希和插值数量；没有擅自替换插值逻辑 |
| 英文 NLS 候选 | 516 条，记录原始索引和源文哈希，包含品牌/快捷键及尚待审核文本 |
| 语言包占位符拒绝 | 22 条，单列索引和原文哈希，不默许格式不兼容的译文 |

这不是“所有 UI 已汉化”认证，也不是可安装版本。远端内容、第三方扩展、HTML 混合语言、旧注入运行时的动态行为及所有 GUI 分支未完成实机验收。已迁移的是**明确审核的资源范围**，不是旧补丁所有运行时行为。

`profile-3.27.22.json` 明确为 `verified_resource_preview_only` / `installation_supported: false`。本机仍为旧补丁 3.24.18，未升级、未应用新增翻译；3.24 安装器继续拒绝覆盖旧补丁状态和 3.27 样本。未来开放部署仍需设计合规的安装路径、处理官方语言包注册及缓存、完整安装/恢复和 GUI 回归；禁止修改应用签名时，不能直接使用资源打补丁方式部署。不要把此预览文件改名为 `profile.json` 或手动关闭门控。
