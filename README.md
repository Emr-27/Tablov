# 扒谱洛夫（Tablov）

这是根据 [V2 技术规格](harmonica_transcriber_codex_handoff_v2.md) 与 [V3 开发路线](harmonica_transcriber_development_roadmap_v3.md) 构建的本机版本。支持人工 Score JSON、单旋律 PPQ MIDI，以及 MP3/WAV/MP4/M4A 片段 → 简谱、12 孔半音阶口琴 TAB、MIDI 与诊断报告。混合歌曲可分离主唱和伴奏，分别生成主唱草谱与伴奏单线条旋律候选。自动句级八度编配和 easy 搜索仍在后续里程碑。

## 交互界面

双击项目目录里的 **启动扒谱洛夫.cmd**，浏览器会自动打开本机工作台。也可以在 PowerShell 运行 `& .\扒谱洛夫.ps1 ui`。默认地址为 `http://127.0.0.1:8765`；端口被占用时自动换用空闲端口，实际地址保存在 `runtime/ui/server.json`。重复启动会打开已有服务。

界面支持拖入不超过 96 MB 的 MP3 / WAV / MP4 / M4A / MIDI / Score JSON、试用乐谱与 WAV 示例、整体移调、简谱与 TAB 预览、原谱对照、试听、音高校正以及下载全部产物。简谱数字下方一点表示低音，竖排两点表示倍低音；纯文本导出分别写作 `1,` 和 `1,,`。音频识别结果的“原始旋律试听”播放对应录音片段；主唱分离模式播放分离后的原唱人声。口琴版播放识别草谱的合成音。MIDI/JSON 的两版试听均由谱面合成。每次生成和校正都保存到新的 `runtime/ui/jobs/<任务编号>/output` 目录，原文件不受影响。关闭浏览器不会关闭本机服务，重启电脑后需要再次双击入口。

音频识别使用本机 DDSP Python 中的 NumPy FFT-YIN。手动选段单次最长 60 秒；勾选“自动处理整首歌曲”后，FFprobe 读取时长，程序按约 55 秒分段，在独立进程中逐段分析，再按原音频时间轴合并逐帧证据。整首模式目前支持最长 1 小时且源文件不超过 96 MB。混合歌曲模式用 MSST 分离主唱与伴奏；主唱使用 FFT-YIN，伴奏先抑制低频和高频打击乐，再从谐波显著度中提取单旋律候选。不确定的复音区段保留问号，不能视作完整伴奏编曲。请为音频填写四分音符 BPM 与调性；未填写调性时暂按 C 大调。默认拍号 4/4 和小节对齐未获确认，报告标记 `partial`。分段接缝、分离残留、和声竞争以及节奏/调性仍需人工核对。ZIP 内含两条旋律的谱面/MIDI、`audio_manifest.json`、逐帧证据和两条 MP3 试听音轨；大体积主唱 WAV 单独下载，原音频不会装进 ZIP。

原路线优先试 pYIN；本机 2.25 秒样例首次运行超过 3 分钟仍未完成，因此交互界面采用实测快速完成的 FFT-YIN 基线。它适合先生成可编辑草谱，尚未经过人工标注歌曲的音符准确率评估。

谱面中的 `*` 表示程序根据较弱但连续的候选音高，或前后音符之间不超过约 0.2 秒的短缺口自动补出的音；它们会进入简谱、TAB 和 MIDI，但必须对照原音频核对。长复音或缺少连续证据的区段仍保留 `?`，不会伪装成确定音符。音高校正可以覆盖这些推测音；修改后的音符不再标记为自动补全。

## 完整歌曲与 MP4

音频入口接受 MP3、WAV、MP4、M4A（含只有 AAC 音轨的 MP4）。超过 60 秒的音频导入后默认选择整首模式；也可取消勾选，拖动试听进度条并手动分析最长 60 秒的片段。处理时进度条按已完成的真实分段推进，并显示当前的拼接、导出或打包阶段；单段内部的模型处理时间无法预先精确估计，进度会在该段完成时更新。结果会显示分段数。

“混合歌曲 · 人声＋伴奏旋律候选”使用本机 MSST 的 `mel_band_roformer_vocals_becruily.ckpt` 和对应配置，将选段分成人声与伴奏。伴奏使用模型输出的 `other` stem，界面提供独立旋律候选谱、试听 MP3、简谱、MIDI 和 JSON。此路径沿用本机 MSST 既往成功日志的第一步参数。后续卡拉 OK 分离与去混响是可选的专门步骤，目前不自动应用。MSST 的文件和模型只读，日志及中间音轨留在本项目各任务目录。若分离失败，界面会报错，不会悄悄改为分析混音。

主唱分离仍可能留下伴奏、和声或气声；音符、BPM、调性、拍号与小节对齐均需试听核对。原音频不会收入 ZIP。

## 命令行运行

本工作区已建立 `.venv`，可以在 PowerShell 中从项目根目录直接执行：

```powershell
& .\扒谱洛夫.ps1 .\examples\g_major_four_notes.json --output .\outputs\g_major
& .\扒谱洛夫.ps1 .\examples\g_major_four_notes.json --output .\outputs\g_major_transposed --transpose 12
& .\扒谱洛夫.ps1 .\melody.mid --track 1 --channel 0 --key 'G major' --do-midi 67 --output .\outputs\melody
& .\扒谱洛夫.ps1 doctor
& .\扒谱洛夫.ps1 catalog
```

首次在此机重建核心环境时，可用已核查的本地 Python 3.11 创建只含标准库的新环境：

```powershell
py -3.11 -m venv .venv
```

确定性核心不需要 Torch、Mido 或网络下载。音频识别依赖外部 DDSP Python 与 FFmpeg；复制 [配置模板](configs/local_backends.example.json) 为 `configs/local_backends.local.json`，并填入本机实际路径。旧整合包不被修改。

`--profile` 目前接收完整 JSON 音表；默认采用 [保守 12 孔 C 调配置](profiles/chromatic_12_c_conservative.json)。该配置将 12 孔按键吸设为不可用，不推定特定型号的最高音。`--instrument chromatic-12-c` 是默认配置别名；不可与 `--profile` 同时使用。

`doctor` 读取本机配置 [local_backends.local.json](configs/local_backends.local.json) 并写出 `runtime/doctor.json`。它检查路径、文件大小、核心环境与默认音表；`dependency_checked`、`model_loaded`、`inference_checked`、`timing_checked` 为 `null` 表示未测试。修改本机路径时，参考 [配置模板](configs/local_backends.example.json)。本机配置在 `.gitignore` 中，不随代码提交。

`catalog` 只读扫描上述配置里的 `audio_asset_root`，将 WAV 的 SHA-256、头信息和文件名推得的候选源组写到 `runtime/audio_catalog.json`。它不会把同曲派生文件当成独立歌曲，也不会仅凭名称或同样本数确认来源、逐样本对齐或旋律真值。

代码中已有 `RunnerRequest/RunnerManifest 1.0` 的外部后端协议。独立 worker 以指定解释器、临时任务目录和绝对输入运行；只有 manifest、时间坐标及所有产物哈希通过校验后才进入缓存。当前通过模拟 worker 测过成功、缓存重用、超时、非零退出、缺失产物和坏 schema。现已另行接入本机 MSST 主唱分离与 FFT-YIN 音高分析；它们尚未纳入该通用 Runner 缓存协议。

## 输入与输出

人工 JSON 采用 Score schema `2.0`。`q` 是四分音符单位，以约分字符串写入；`time_map` 明确音频秒与谱面 q 的关系。所有事件连续覆盖 `score_start_q` 到 `score_end_q`；空白必须标成已确认 `rest` 或待核对 `unknown`。示例 [G 大调四音](examples/g_major_four_notes.json) 来自 V2 正式算例。

MIDI 导入支持 type 0/1 的 PPQ 时间制。存在多个音符轨或同轨多个音符通道时，指定 `--track` / `--channel`。复音、SMPTE/type 2、影响时值的踏板/滑音控制与非零弯音会返回字段明确的错误。导出时另写 `*.mid.json` 旁车文件；哈希匹配后可恢复 MIDI 本身不表达的弱起、unknown、原始观测和精确 q。缺少有效旁车文件时，导入从 tick 0 重新建谱，并注明无法推定的语义。

输出目录包含：

| 文件 | 内容 |
|---|---|
| `original_score.json` | 校验后的原谱及重新计算的演奏秒；`observed` 不变 |
| `arrangement.json` | 全局移调、实际吹奏音高、所选指法及成本分项 |
| `melody_original.md` | 原谱数字简谱 |
| `melody_harmonica.md`、`harmonica_tab.md` | 本次口琴版简谱与 TAB |
| `melody_original.mid`、`melody_harmonica.mid` | 原谱和口琴版的回放 MIDI |
| 两个 `*.mid.json` | 分别对应 MIDI 的语义旁车文件 |
| `report.json` | `complete/partial`、unknown、不可吹音、文件哈希及运行信息 |

默认不覆盖同名文件；需要替换时加 `--overwrite`。若有 unknown、不可吹音或无旁车 MIDI 的小节对齐未确认，默认保留诊断产物、`report.status=partial`、退出 0；加 `--strict` 时仍交付产物并退出 4。输入错误退出 2；运行或依赖故障退出 3。`--octave-policy phrase` 与 `--transpose easy` 会明确提示尚未实现。

## 验收

```powershell
& .\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试覆盖三连音、跨小节延音、三种时间坐标、弱起/变速、双轨 MIDI 元事件、复音与控制器拒绝、大小调及异名同音、指法穷举对照、越界与显式移调、人工编辑重导出、同音重起、rest/unknown。测试只证明当前实现符合这些确定性样例；不代表音频模型已可用或口琴真人吹奏已验证。


