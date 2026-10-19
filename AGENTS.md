# 项目约定

## 简历同步（每次改简历都必须执行）

简历有**三处副本，内容必须始终一致**：

| 副本 | 用途 |
|---|---|
| `简历-孙浩然-实习版.md` | 主源（Markdown，改内容从这里改） |
| `E:\桌面\简历-孙浩然-实习版.docx` | 投递用，桌面入口 |
| `D:\agent\CodePilot\简历-孙浩然-实习版.docx` | 项目内副本（与桌面必须字节一致） |
| `D:\agent\CodePilot\简历-孙浩然-实习版.pdf` | 由 docx 转出，投递用 |

### 硬性规则

1. **不允许只改一处。** 任何简历内容变更后，docx（桌面 + 项目）与 PDF 必须同步。
2. **简历保持 1 页。** 新增内容后必须验证页数，超页就压缩文案，不许放任变 2 页。
3. **改 docx 时保留原排版**：只替换目标段落的 run 文本，不要重建整个文档。
4. **数字必须可复现**：简历里的实测数字（份数、行数、行长、标记数）都要能对上真实文件，不得虚报样本量。
5. **PDF 从 docx 转出**，不要手工排版。

### 同步步骤

```powershell
# 1) 桌面 docx -> 项目 docx
Copy-Item "E:\桌面\简历-孙浩然-实习版.docx" "D:\agent\CodePilot\简历-孙浩然-实习版.docx" -Force

# 2) docx -> PDF（用内置 LibreOffice Kit）
$node = "C:\Users\haora\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
$cli  = "D:\dpsk\resources\app.asar.unpacked\dsh\node_modules\@deepseek-ai\libreoffice-kit\lib\cli.js"
& $node $cli convert --input "D:\agent\CodePilot\简历-孙浩然-实习版.docx" `
                      --output "D:\agent\CodePilot\_out\简历-孙浩然-实习版.pdf"
Move-Item "D:\agent\CodePilot\_out\简历-孙浩然-实习版.pdf" "D:\agent\CodePilot\简历-孙浩然-实习版.pdf" -Force

# 3) 校验一致性 + 页数
(Get-FileHash "E:\桌面\简历-孙浩然-实习版.docx" -Algorithm MD5).Hash -eq `
(Get-FileHash "D:\agent\CodePilot\简历-孙浩然-实习版.docx" -Algorithm MD5).Hash
& $node $cli render --input "D:\agent\CodePilot\简历-孙浩然-实习版.pdf" `
                    --output-dir "D:\agent\CodePilot\_chk" --pages 1 --dpi 100
# 看输出里的 pageCount，必须是 1
```

### 注意

- 改 docx 前先确认 Word 没打开它（否则写入会 `PermissionError`；存成新文件名属于违规操作）。
- 渲染器在本机有缩放问题（文字被放大并切边），**不能靠渲染图判断排版**；页数可信，视觉请以 Word 实际打开为准。

## 简历历史

- 旧版与改前备份放在 `D:\agent\_backups\简历历史\`，不要放回桌面。
- 桌面上只保留最新版一份简历。
