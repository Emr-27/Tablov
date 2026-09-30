# Harmonica Transcriber 开发交接文档 V2

版本：2.0 · 修订日期：2026-09-29 · 状态：技术规格，尚未实现或通过真实音频验收。

来源：桌面原稿 `harmonica_transcriber_codex_handoff.md`。本版整合技术审查并替代原稿的实施约定；原稿保留。本文中的命令和阶段任务均为后续开发规格，本次文档修订不代表程序已可运行。

## 1. 目标、范围与交付边界

将完整歌曲中可以辨认的主唱旋律，以及前奏、间奏、Solo、尾奏中的主要器乐旋律，整理为单声部旋律，再输出 C 调 12 孔 Solo Tuning 半音阶口琴数字简谱和优化孔位谱（TAB）。

保留原方案的核心设计：本地优先；音频模型与确定性音乐处理分离；音高与指法分离；可配置乐器音表；保留全部等价指法；按乐句进行动态规划；允许人工修正中间数据。

“覆盖整首歌”指完整保存时间轴，不要求每一时刻都有音符。鼓段、静音和只有和弦铺底的区段可为休止；无法确定旋律的区段必须标为待核对，不能伪造音符或假装已确认休止。

首版不做通用 DAW、多声部总谱、伴奏生成、完整装饰音复刻或 GUI。PDF/SVG 属于后续视觉输出。音频模型的准确率及实际吹奏体验必须单独验证。

| 阶段 | 输入 | 明确交付 |
|---|---|---|
| Phase 0 | 人工 JSON、指定单旋律轨的 MIDI | 数据校验、简谱、合法 TAB、精确时值、指法 DP、MIDI 导出与报告 |
| Phase 1 | 清晰单声部人声或乐器音频 | 音符分段、受保护的清洗、节拍映射、量化，接入 Phase 0 |
| Phase 2 | 完整歌曲 | 分轨后的人声主旋律；无人声段标明尚未分析器乐 |
| Phase 3 | 完整歌曲中的器乐区段 | 经独立验证的候选生成与主旋律选择、拒选和不确定区段 |
| Phase 4 | 已确定的主旋律 | 全局移调搜索、乐句八度编配、适度简化与可吹奏性评估 |
| Phase 5 | 结构化乐谱 | SVG/PDF/MusicXML 等视觉输出 |

## 2. 必须固定的约定

| 项目 | V2 约定 |
|---|---|
| 音高 | 12 平均律 MIDI 整数，C4=60；连续 F0/音分证据另存，不提前丢弃 |
| 乐谱时间 | `q` 表示一个四分音符时值；JSON 使用约分后的有理数字符串，如 `"1/2"` |
| 音频时间 | 原始音频解码后的绝对秒，所有区间为半开区间 `[start_sec,end_sec)` |
| 速度 | `bpm_q` 始终指每分钟四分音符数，与 6/8 的附点四分音符感知拍分开 |
| 原始与编配 | 原始转录、量化乐谱、口琴编配分别保存；每次修改有来源和原因 |
| 休止与未知 | `rest` 表示确认没有目标旋律；`unknown` 表示证据不足或该阶段尚未处理 |
| 小调 | Phase 0 固定采用 la-based minor；A 小调使用 `1=C`、主音 `6=A` |
| 八度 | `do_midi` 指无点数字 1 的绝对 MIDI 音高；不按科学音高八度号直接加点 |
| 指法最优 | 仅指已声明候选集合及版本化成本函数下的最优；不等于真实演奏难度已验证 |
| 模型分数 | 记录分数含义、来源、版本；未校准分数不称为“正确概率” |
| 首版越界 | 保留原音高并报告不可演奏；不自动逐音折回音域 |

## 3. 总体处理流程

```text
JSON / 单旋律 MIDI ────────────────────────┐
                                           ↓
音频 → 解码与全局时间契约 → 可选分轨 → 模型原始证据
                   ↓                       ↓
             节拍/拍号/调性估计        候选音符与旋律轨迹
                   ↓                       ↓
             可人工覆盖           主旋律选择 + rest/unknown
                   └──────────────┬────────┘
                                  ↓
                     有起音保护的清洗、节奏量化
                                  ↓
                       原始乐谱 original_score
                                  ↓
                     显式移调 / 可选乐句八度编配
                                  ↓
                   合法指法候选 → 统一成本下的 DP
                                  ↓
                 共用事件序列的简谱、TAB、JSON、MIDI
                                  ↓
                      report + 人工修改后重新导出
```

节拍证据在节奏清洗和量化前取得；不再采用“先量化，后获取 BPM”的隐含顺序。音频调性检测不能成为强制改正音高的理由，借用和弦、经过音及离调音均可合法存在。

## 4. 数据契约与可追溯性

### 4.1 四层数据对象

1. `ModelEvidence`：帧级 F0、音分、起音激活、有声标志、模型分数、采样率、hop、时间戳规则与分块偏移；大数组可放 NPZ，JSON 保存引用与哈希。
2. `ObservedMelody`：原始绝对秒上的候选／选中音符及区段，不覆盖模型原始输出。清洗形成新修订，并记录输入事件 ID。
3. `Score`：权威的有理数乐谱位置、时值、调性和时间映射；保存原始观测区间以供回听，但观测秒数不必与量化后的演奏秒数相等。
4. `Arrangement`：引用某个 Score 的确定版本，保存演奏音高、指法、全局移调、乐句八度偏移和删减记录。原始 Score 不被覆盖。

### 4.2 最低字段要求

| 对象 | 必须包含的字段或约束 |
|---|---|
| 文档根 | `schema_version`、`revision_id`、`source`、`time_map`、`meter_map`、`key_map`、`events`、`changes` |
| 有理数 | 统一为 `n` 或 `n/d` 字符串，分母正数，约分；禁止 NaN、Infinity 和以 float 作为权威乐谱值 |
| 音符事件 | 唯一 `id`、`kind=note`、`pitch_midi`、`start_q`、`duration_q`、`phrase_id`、可空 `spelling_hint` |
| 休止/未知 | `kind=rest/unknown`、位置、时值、原因；不得携带虚构音高 |
| 观测来源 | `observed` 可空，存在时含绝对秒区间、模型/轨道/候选 ID；人工 JSON 可不带观测 |
| 分数 | 分开的 `activity`、`pitch`、`selection`，各自保存 `value`、`semantics`、`producer`；缺失值为 null |
| 记谱属性 | 可空 `tuplet_group_id`、`tie_group_id`、原始起音标志；显示分片另存 `render_parent_id` |
| 调性项 | 起点、`tonic_pc`、`tonic_spelling`、`mode`、`do_midi`、`minor_system`、来源 `user/midi/estimated/assumed`；拼写须与 pitch class 一致 |
| 修改记录 | 动作、前后事件 ID、前后值、原因、规则版本；人工编辑标记 `producer=user` |
| 编配 | `source_revision_id`、`global_transpose`、每句 `octave_shift`、每音 `played_pitch_midi`、可空指法与失败原因 |

事件按 `start_q` 排序，时值必须大于 0，note 音高在 0–127 内。最终单声部 Score 的事件互不重叠，并覆盖其声明的乐谱区间；休止和未知必须显式填充。末尾停顿的范围来自输入的明确结束位置，不能无限补齐。

候选数据允许复音；只有选中旋律进入单声部校验。真实重叠需要在选择阶段有记录地解决，不能在导出器里无声地丢掉其中一个音。

音频自动分句的首个基线为人工边界或已确认休止≥0.5 秒；短休止不自动断句。unknown 强制拆成独立的已解决子段，各段内生成稳定 phrase_id，原始区段 ID 与分句规则版本保留。Phase 4 仅在这些已确定乐句上搜索八度；人工可修改分句后重新编配。

### 4.3 最小可编辑 Score 样例

以下为完整的人工四音例，120 BPM、4/4、G 大调，无点 1=G4。`source.kind=manual` 时不用伪造音频时长。

```json
{
  "schema_version": "2.0",
  "revision_id": "example-g-major-r1",
  "source": {"kind": "manual", "name": "G major four notes"},
  "score_start_q": "0",
  "score_end_q": "4",
  "time_map": {"kind": "tempo", "origin_q": "0", "origin_sec": 0.0, "tempo_events": [{"at_q": "0", "bpm_q": 120.0}]},
  "meter_map": [{"at_q": "0", "numerator": 4, "denominator": 4, "source": "user"}],
  "first_full_bar_q": "0",
  "key_map": [{"at_q": "0", "tonic_pc": 7, "tonic_spelling": "G", "mode": "major", "do_midi": 67, "minor_system": "la", "source": "user"}],
  "events": [
    {"id": "n1", "kind": "note", "pitch_midi": 74, "start_q": "0", "duration_q": "1", "phrase_id": "p1", "spelling_hint": "D5"},
    {"id": "n2", "kind": "note", "pitch_midi": 76, "start_q": "1", "duration_q": "1", "phrase_id": "p1", "spelling_hint": "E5"},
    {"id": "n3", "kind": "note", "pitch_midi": 79, "start_q": "2", "duration_q": "1", "phrase_id": "p1", "spelling_hint": "G5"},
    {"id": "n4", "kind": "note", "pitch_midi": 78, "start_q": "3", "duration_q": "1", "phrase_id": "p1", "spelling_hint": "F#5"}
  ],
  "changes": []
}
```

音频来源的 `observed.start_sec/end_sec` 保存实测起止；`performed.start_sec/end_sec` 由 Score 的 q 与 time_map 派生。人工改 q 后重新计算 performed，但保留 observed，禁止出现两套互相覆盖的权威时间。

## 5. 时间、拍号、弱起与 MIDI

### 5.1 秒与乐谱位置

恒速映射：`q(t) = origin_q + (t - origin_sec) * bpm_q / 60`。默认 `origin_q=0` 指首个完整小节第一拍；origin_sec 可大于 0。弱起音符可以有负 q，其第一完整小节仍从 q=0 开始。

前导静音可标在原音频区段表中，不强制印成多个空小节；它不会改变原音频绝对秒坐标。谱面起点 `score_start_q` 可以为负、零或正，必须显式保存。若尚未确认拍点原点，输出 `METER_ALIGNMENT_UNCERTAIN`，不声称小节已对齐。

变速有两种互斥的权威映射：

- `kind=tempo`：按递增的 `at_q` 保存分段恒定 `bpm_q`，相对 origin 分段积分得到秒；映射至少覆盖 `min(origin_q,score_start_q)` 到 `max(origin_q,score_end_q)`，最早 tempo 必须覆盖该下界，不向前隐式猜测。
- `kind=anchors`：保存严格递增的 `(q,sec)` 锚点，以分段线性插值做双向转换；锚点必须覆盖上述包括 origin 的整个范围，并与声明的 `(origin_q,origin_sec)` 一致。范围外要求补锚点或显式报错。锚点 q 表示四分音符位置，不能将不明拍单位的 tracker 输出直接当 q。

单一全曲 BPM 只用于已确认恒速的输入。6/8 每小节长度为 `6*4/8=3q`；若人工输入附点四分音符速度 60，则内部为 `bpm_q=90`，显示必须注明节拍单位。

拍号变化保存在 `meter_map`，Phase 0 只允许发生在明确的小节边界。弱起由 `first_full_bar_q` 与 `score_start_q` 表示；首尾不完整小节注明实际时值，不强行填充为满小节。

meter_map/key_map 的初始项必须在 score_start_q 已生效，初始 meter 项可位于负 q 的弱起起点，它是初始状态而非小节内变拍。后续 meter 变化才受完整小节边界限制。带 sidecar 的 MIDI 重导入恢复负 q、first_full_bar_q 和前导时间；仅导入 MIDI 时无法唯一推断弱起位置，必须使用显式参数或标为未确认。

### 5.2 MIDI 导入与导出

- Phase 0 支持 PPQ 时间制的 type 0/1；type 1 显式选择唯一音符轨，元信息从全部轨读取并统一到绝对 tick。自动选择仅限文件恰有一个有效非鼓旋律轨。
- 多音符轨未指定、真正复音、同音重叠、冲突 tempo/meter 元事件、type 2、SMPTE 时间制均明确拒绝。多声道混在一个轨时也必须选定 channel 或拒绝歧义。
- 以 ticks/PPQ 得到精确 q，不先转秒再按全曲 BPM 重建。读取音符前先配对 note-on/off；velocity=0 的 note-on 按 note-off 处理，未配对事件报错。
- Phase 0 对会影响音高或时值的 pitch bend、sustain/sostenuto、portamento 控制明确报不支持；一般 volume/pan 可记录后忽略。输入 key 缺失时采用用户指定值；否则明确标注临时 `1=C` 参考，并不声称检测出 C 大调。
- 可信重复 note-on 保留；跨小节长音是一个事件的显示分片，不产生额外 note-on。连音组合保留有理数时值。
- 音频来源 MIDI 输出默认 `--midi-origin audio`，人工/MIDI 输入默认 `score-start`。统一令 `q_base=score_start_q`，从该处的有效速度及后续变化生成元事件；音符及元事件的相对 tick 为 `PPQ*(q-q_base)`。score-start 模式 padding_ticks=0；audio 模式令 t_start=time_map(q_base)，按起始速度将 t_start 转为整数 padding_ticks，所有事件共同加此偏移，并在 tick 0 写入起始 tempo。t_start<0 的 audio 请求报错。记录 q_base、padding_ticks、实际前导秒与舍入误差；这样弱起可有负 q，也不会丢失原音频前导静音。T03 的 audio 模式首音应在 .25s，score-start 模式则在 0s。
- 从有理数 q 选择足够的 PPQ（至少可精确表示相对 q_base 的所有起止点与元事件位置，默认优先 960）；所需 PPQ 超过标准 PPQ 表示范围时显式报错，禁止舍入音符间的 q 差。anchor map 每个区间转换为等效恒速 tempo。tempo 的微秒整数精度及 audio 前导时间取整造成的秒级误差单独报告；精确 q 往返使用 sidecar 恢复 q_base/padding，单独 MIDI 只能保证平移后的 q 间隔。
- MIDI 不表达 unknown；导出时这些区间无音符，并在同名 JSON/报告和轨道文本标记中注明 `UNRESOLVED`，不得描述为完整已确认旋律。

参考：[Mido MIDI 时间、文件类型和速度](https://mido.readthedocs.io/en/stable/files/midi.html)。

## 6. 乐器配置与合法指法

用 `(hole,breath,slide) → MIDI pitch|null` 作为配置的唯一权威映射，再自动建立反向候选表。音名仅为显示，不以 `Cs4/C#4/Db4` 等字符串分别建音高键。

首版默认 `chromatic_12_c_conservative`：使用以下主体音表，12 孔按键吸未经具体琴型确认时禁用。这个保守配置不会擅自增加最高音，也不代表所有型号均一致。

| 孔 | 放键吹 | 放键吸 | 按键吹 | 按键吸 |
|---|---:|---:|---:|---:|
| 1 | C4 / 60 | D4 / 62 | C#4 / 61 | D#4 / 63 |
| 2 | E4 / 64 | F4 / 65 | F4 / 65 | F#4 / 66 |
| 3 | G4 / 67 | A4 / 69 | G#4 / 68 | A#4 / 70 |
| 4 | C5 / 72 | B4 / 71 | C#5 / 73 | C5 / 72 |
| 5 | C5 / 72 | D5 / 74 | C#5 / 73 | D#5 / 75 |
| 6 | E5 / 76 | F5 / 77 | F5 / 77 | F#5 / 78 |
| 7 | G5 / 79 | A5 / 81 | G#5 / 80 | A#5 / 82 |
| 8 | C6 / 84 | B5 / 83 | C#6 / 85 | C6 / 84 |
| 9 | C6 / 84 | D6 / 86 | C#6 / 85 | D#6 / 87 |
| 10 | E6 / 88 | F6 / 89 | F6 / 89 | F#6 / 90 |
| 11 | G6 / 91 | A6 / 93 | G#6 / 92 | A#6 / 94 |
| 12 | C7 / 96 | B6 / 95 | C#7 / 97 | null：按型号启用 |

配置必须有 `profile_id`、版本、孔数、调性、调律、资料来源及适用型号/版本。可选 `reference_a_hz` 描述乐器参考频率，与乐谱整数音高分开。型号未知时该字段可以 null。

```yaml
profile_id: chromatic_12_c_conservative
profile_version: 1
hole_count: 12
key: C
tuning: solo
reference_a_hz: null
top_draw_slide_verified: false
position_order: [blow_out, draw_out, blow_in, draw_in]
holes:
  1: [60, 62, 61, 63]
  2: [64, 65, 65, 66]
  3: [67, 69, 68, 70]
  4: [72, 71, 73, 72]
  5: [72, 74, 73, 75]
  6: [76, 77, 77, 78]
  7: [79, 81, 80, 82]
  8: [84, 83, 85, 84]
  9: [84, 86, 85, 87]
  10: [88, 89, 89, 90]
  11: [91, 93, 92, 94]
  12: [96, 95, 97, null]
sources:
  - https://www.hohner-cshop.de/out/media/HOHNER_CX-12_new_de.pdf
scope_note: 主体音表参考官方音表；未知具体琴型时禁用最高孔按键吸，不声明整琴型号验证。
```

命名型号配置须由对应官方音表逐项核对，不能由通用配置名称推断。官方 CX-12 新版音表区分制造版本并列出 C 调最高 D7，可作为独立型号配置来源；不据此推定所有 270 或所有 12 孔琴都相同。[HOHNER CX-12 官方音表](https://www.hohner-cshop.de/out/media/HOHNER_CX-12_new_de.pdf)

校验全部物理位置且位置不重复；禁用位置不得生成候选；同音的多个位置全部保留。C5 的候选为 `4B、4D+、5B`，F4 为 `2D、2B+`。可吹集合从配置实际键集合得出，不能仅以最低/最高音之间的闭区间代替。

## 7. 调性、唱名与异名同音

Phase 0 使用用户或 MIDI 已给定的调性；自动估计在 Phase 1。检测失败可以使用显式标注的记谱参考调，输出候选和不确定状态。整曲存在转调时用 key_map 表示；自动找转调可延后，不把一个检测出的调性强套全曲。

数字采用大调音级基准 `offsets=[0,2,4,5,7,9,11]`。自然小调默认沿用关系大调的 do：A minor 的 do 为 C，D minor 的 do 为 F；和声/旋律小调变化使用临时升降号，不删去这些音。

数字音的反解公式：`pitch = do_midi + offsets[degree-1] + accidental + 12*octave`。`octave=0` 为无点，+1 写 `'`，-1 写 `,`。因此当 `do_midi=67` 时，G4=`1`、F#5=`7`、G5=`1'`、D5=`5`。

调性与 do 的 pitch class 必须一致：major 的 do_pc=tonic_pc；minor 的 do_pc=(tonic_pc+3)%12。选用哪一组 do_midi 八度由用户/记谱配置明确给出；缺省选择对应 do 音级的科学音高第 4 组，写入谱头后固定，不能逐音变动。

`tonic_spelling` 保留 C# major 与 Db major 等调名区别；仅有 tonic_pc 不能恢复调号。Phase 0 接受传统调号 -7 至 +7 个升降号范围的大/小调，解析并验证其调号倾向；范围外理论调式明确报不支持。显式移调若没有目标调名，选择同 pitch class/mode 下调号数绝对值最少的拼写；并列时延续原调的升降倾向，原调无倾向时选升号。

拼写优先级：有效且与 MIDI 音高一致的 `spelling_hint` → 最少临时升降号 → 已声明调号的升降倾向 → 固定数字顺序打破平局。C 大调未给提示时优先升号；降号调优先降号。此规则是可复现的默认拼写，不承诺完整和声分析。支持 `#、b、##、bb`，无效 hint 报错。

例如 C4 为 do 时，MIDI 61 可按提示写 `#1` 或 `b2`；B#3 可写 `#7,`，Cb4 可写 `b1`。谱面反解必须回到同一 MIDI 音高。MIDI 整数本身不能唯一确定异名同音拼写。[music21 音高拼写说明](https://music21.org/music21docs/moduleReference/modulePitch.html#music21.pitch.Pitch.spellingIsInferred)

Phase 0 不提供 do-based minor；若输入声明该体系，显式报不支持，不能用 la-based 规则悄悄解释。

## 8. Markdown 简谱与 TAB 的完整时值语法

采用可核对的文本谱，置于 Markdown 代码块内。它是本项目的交换/展示约定，不冒充标准印刷简谱。每个音符和停顿都写明时值。

| 写法 | 含义 |
|---|---|
| `5:1` | 无点 5，四分音符，时值 1q |
| `1':1/2` | 高八度 1，八分音符 |
| `b3,:1/4` | 低八度降 3，十六分音符 |
| `6:3/2` | 6，附点四分音符 |
| `0:1/2` | 已确认休止，半 q |
| `?:1` | 一 q 待核对区段；不是确认休止 |
| `5:1/2 ~ \| 5:1/2` | 同一个音跨小节延续，不重新起音 |
| `T3:2(1:1/3 2:1/3 3:1/3)` | 八分音符三连音组，括号内已经是实际 q 时值，不能再乘 2/3 |
| `5D:1`、`6D+:1/2` | TAB 使用相同的时值语法；B=吹，D=吸，+=按键 |
| `X[B3]:1` | 音高已知但当前配置不可演奏；不能伪造孔位 |

三连音显示组 Phase 0 限三个等长、实际时值各为 1/3q 的元素，可含休止；更复杂连音可先用显式有理数时值并保留 JSON 分组信息。跨小节时按事件拆分显示并保留 group ID，不要求三连音括号跨栏。

`~` 只连接同一逻辑音符的同音分片。相邻同音事件没有 `~` 时表示重复起音；休止、unknown 不允许加 tie。跨小节 tie 的 TAB 维持同一指法。省略时值、用空格猜时值、用 `-` 猜延长拍数均不作为 V2 的机器可读格式。

排版器按 meter_map 切小节：每个完整小节的事件时值和必须精确等于 `numerator*4/denominator`。弱起、尾部残小节单独标记。简谱、TAB、小节线及不确定标记从同一渲染事件流生成。

### 8.1 已修正的 G 大调示例

```text
调性：G major；1=G；无点 1=G4；4/4；四分音符=120
简谱 | 5:1   6:1   1':1  7:1   |
TAB  | 5D:1  6B:1  7B:1  6D+:1 |
音高 | D5    E5    G5    F#5    |
```

这四音分别为 MIDI 74、76、79、78，和第 4.3 节数据完全一致。正式产物的 TAB 必须由 profile 自动计算；示例不作为手写硬编码模板。

## 9. 指法动态规划与成本函数

对已确定且在配置内的演奏音高，构造全部指法候选 F_i。将一个逻辑长音作为一个节点，谱面 tie 分片不重复优化。

```text
C(F) = sum_i unary(i,F_i) + sum_i transition(i,F_(i-1),F_i)
D_i(f) = unary(i,f) + min_g [D_(i-1)(g) + transition(i,g,f)]
```

初始化为首音 unary，回溯完整路径。纯相邻状态成本下复杂度 O(N*K²)。没有候选的音返回明确不可演奏状态，不得用巨大有限惩罚把非法指法选进结果。

初始基础边成本：`base = 1.0*孔距 + 0.25*吹吸切换 + 0.40*推键切换 + 0.80*max(0,孔距-2)`。

时间修正采用量化乐谱映射出的演奏秒数：`ioi = 下一音开始 - 当前音开始`，`gap = max(0,下一音开始 - 当前音结束)`；初始 `speed=min(4,max(1,0.25/max(ioi,0.02)))`，`relief=exp(-gap/0.25)`，`transition=base*speed*relief`。明确乐句边界或 gap≥0.5 秒时重置边成本为 0；长停顿是否可换气另行提示。

Phase 0 默认 unary=0。后续若启用持键时长、特定位置或音区偏好，必须作为版本化 unary/edge 的一部分进入同一 DP，不能优化后才追加一个会改变最优路径的评分。所有权重均为工程初值，尚未由真实吹奏标定。

gap 的减负只用于已经确认的休止；unknown 或不可演奏音强制切断优化区间，不能将这些区间当作换气机会或计入休止收益。报告只对已解决子段求成本，全曲成本标为未确定。编配的边界轮廓惩罚同样不跨 unknown。

同成本按完整指法序列的固定顺序打破平局：先孔号，再 blow/draw，再放键/按键；使用固定数值容差，保证复现。报告成本分项与成本配置哈希。

连续吹/吸时长先作为提示（阈值可配置，默认 8 秒，仅为体验提醒），不声称已建模肺活量。若未来将累计气流约束纳入目标，DP 状态必须包含相应历史，不能继续宣称当前一阶状态已求得该目标的全局最优。

## 10. 原始旋律、越界、移调与编配

`original_score` 保存清洗与量化后的原调旋律，“original”表示没有编配移调，不代表复刻了所有演唱细节。`observed/raw` 仍保留清洗前证据。

Phase 0 支持显式整数全局移调；默认 0。每个音和调性一起移动，do_midi 同步加该整数，音符之间的音程及所有时值保持不变。输出重新生成与新音高一致的 spelling_hint，原拼写留在来源记录，不携带已经失效的提示。MIDI 0–127 越界报错；乐器不可吹的音保留在原谱，并在 TAB 写 `X[...]`、报告 `playable=false`。

禁止默认逐音八度折返。例如 `B3→C4` 不能静默改成 `B4→C4`。Phase 4 的编配顺序如下：

1. 固定用户指定的全局移调 T，或 easy 模式枚举 T=-6…+6，含 0。
2. 对每个已确定乐句枚举整句八度偏移 k∈{-2,-1,0,1,2}，整句统一偏移 `12k`。超出配置的候选直接排除；默认不拆句去迁就单个音。
3. 对每个全局 T 用状态 `(乐句八度 k,指法 f)` 联合搜索。相同乐句内禁止改变 k；乐句边界才允许改变，并加入原始与编配旋律音程差的惩罚。
4. 使用同一指法成本，加八度偏移、边界轮廓改变及整体移调距离项；权重版本化。跨候选统一归一化或统一使用总成本，不混用均值和总数。
5. 无合法候选时保留失败报告，建议人工改变分句、选音或乐器配置；不能宣称已经无越界。

初始附加成本可采用 `2*Σ(duration_q*|k|) + 1*Σ边界|编配音程-原始音程| + 0.25*|T|*总音符时值_q`，与指法成本共同比较。它是待标定的基线；所有比较候选使用相同分句和简化后事件，避免通过无约束删音降低分数。

每次输出保存 `source_pitch_midi`、`global_transpose`、`phrase_octave_shift`、`played_pitch_midi` 与规则版本。整体移调测试要求所有音程不变；乐句八度编配测试要求句内音程不变、边界改变被记录。easy 简化单独留修改日志及保留音符比例，不能覆盖原版。

## 11. CLI、输出与失败语义

以下为接口规格。Phase 0 只实现 `harmonica-export`；音频命令在后续阶段提供。阶段尚未实现的功能必须明确报错，不返回假谱。

```text
harmonica-export INPUT.json --output output --profile PROFILE --transpose 0 --octave-policy reject
harmonica-export melody.mid --track 1 --channel 0 --key "G major" --do-midi 67 --output output
harmonica-transcribe song.mp3 --mode normal --transpose original --octave-policy reject --output output
harmonica-transcribe song.mp3 --mode easy --transpose easy --octave-policy phrase --output output
```

| 参数 | 契约 |
|---|---|
| `--mode original/normal/easy` | 仅控制音频清洗／简化强度；与移调、八度策略分别设置。export 不重新清洗 |
| `--transpose original/easy/INTEGER` | original=0；整数为显式全局移调；easy 为 Phase 4 搜索 |
| `--octave-policy reject/phrase` | reject 默认，保留并报告不可吹音；phrase 为 Phase 4 乐句级编配 |
| `--profile PATH` | 完整乐器配置；`--instrument chromatic-12-c` 仅为默认保守配置的别名，两者同时给定时报冲突 |
| `--bpm FLOAT` | 四分音符每分钟数；不得悄悄覆盖 MIDI tempo map，已有映射时要求专门的显式 override 选项 |
| `--meter N/D`、`--beat-zero-sec FLOAT`、`--pickup-q FRACTION` | 音频拍号与原点覆盖；保存来源 user。pickup_q=L 时 score_start_q=-L、首完整小节 q=0 |
| `--key TEXT`、`--do-midi INT` | 调性与无点 1；必须互相一致；MIDI 已有 key map 时显式说明覆盖范围 |
| `--track INT`、`--channel INT` | MIDI 从 0 计数，选择记录在报告；鼓通道不作为默认旋律 |
| `--device auto/cpu/cuda` | 音频后端须明确支持才接受；auto 回退写入报告，显式 cuda 不可用则报错 |
| `--keep-stems`、`--debug` | 保留分轨及额外模型证据；不得影响音乐结果或缓存语义 |
| `--strict` | 有 unknown 或不可演奏音时返回非零，仍可保留已完成的诊断输出 |
| `--overwrite` | 允许替换指定输出目录内同名产物；默认拒绝覆盖既有结果 |

Phase 0 默认导出清单：

```text
output/
  original_score.json       原始乐谱的规范化副本
  arrangement.json          演奏音高、指法、修改原因
  melody_original.md        原调或明确标注的输入参考调
  melody_harmonica.md       本次编配的数字简谱
  harmonica_tab.md           同一事件流的 TAB
  melody_original.mid       原始乐谱回放
  melody_harmonica.mid       本次演奏音高回放
  report.json               状态、限制、低可靠区段、版本与产物哈希
```

音频阶段增加 `source_manifest.json`、`melody_raw.json`、`melody_clean.json`、`segments.json`；`--keep-stems` 才交付 stems。Phase 4 请求 easy 时另存 easy 编配和对应谱/MIDI，不默认声称每次已经生成两个版本。

退出码：0=请求范围内成功；2=输入或参数契约错误；3=依赖/模型/运行失败；4=strict 模式质量门槛未满足。默认允许 partial 结果时退出 0，但 `report.status=partial` 且谱面显示未解决区段；调用方必须同时检查 status。不完整输出不命名为“完整验证通过”。

报告至少包含：请求阶段与已实现能力、输入哈希、配置/模型/运行版本、实际设备、调性与时间映射来源、profile 验证范围、已处理时长、unknown 时长、不可演奏音 ID、分数语义、修改记录、成本分项、缓存命中、耗时、所有 warnings/errors 和产物路径。原始 MIDI 不能表达的不确定区段在 sidecar 明示。

## 12. 后续音频实现规格

本章规定 Phase 1–3 的首个可复现实现方案。模型与参数须经固定测试集比较后决定，不预先承诺识别准确率。下列阈值、权重及窗口均为待标定初值；分数只表示模型输出或工程启发式，不视为经过校准的正确概率。

### 12.1 输入、增益与绝对时间

用 FFmpeg 解码为浮点 PCM，不删前导静音，不改变速度；把解码后原音频首样本定义为音频坐标 0 秒。这个音频零点与 time_map 中的拍点 `origin_sec` 分开命名；后者可以大于 0。记录原文件和 PCM 哈希、解码器版本、采样率、声道、样本数及编码器延迟处理。保留原始立体声分析母本；分轨按已锁定模型准备输入，各音高适配器另行重采样或混单声道，不覆盖母本。

推理时间统一回写为原音频绝对秒，采用左闭右开区间 `[start_sec, end_sec)`。帧证据必须保存采样率、hop、帧中心约定、片段绝对起点、补零及裁切信息；使用模型自身的帧到秒转换，不能统一套用某个采样率。切块保留上下文重叠，按块的有效中心区接合；跨块同一事件去重并保留来源，不能直接拼接局部时间戳。验收比较整段和切块推理的边界事件及偏移。

音乐时间使用 quarter-note 单位 `q`，由统一的 `TempoMap` 在绝对秒与 q 之间转换；不得让音频适配器用全曲平均 BPM 自行换算。时序尚未确认时仅保留秒，不伪造 q 或小节编号。

分析用 stem 必须保持共同增益。Demucs 导出可能独立缩放各 stem；应优先保存浮点推理结果，或记录并还原缩放，跨源显著度取自共同参考。禁止对各 stem 分别响度归一化后再比较音量。[Demucs 导出说明](https://github.com/facebookresearch/demucs#separating-tracks)

### 12.2 模型适配与原始证据

Phase 1 在同一调参集上比较 Basic Pitch、pYIN 和 torchcrepe；默认后端由音符、起音和误报指标决定。Basic Pitch 输出复音音符及 onset/note/contour 激活；pYIN/CREPE 输出逐帧音高，需要另做音符分段，不能把它们当作相同接口的直接替换。[Basic Pitch](https://github.com/spotify/basic-pitch)、[pYIN 输出](https://librosa.org/doc/0.11.0/generated/librosa.pyin.html)

复音事件关联成单声部轨迹是共用模块：Phase 2 在 vocals 上使用第 12.4 节的候选生成基线，处理和声/合唱重叠；Phase 3 才扩展到 other 等伴奏来源。人声活动为真仍不能证明某条轨迹就是主唱。主唱候选难以区分时输出 unknown，禁止把 Basic Pitch 的原始复音事件直接送入 Score。

适配产物至少包含原始 F0 或多音高激活、voicing/periodicity、onset 证据、音符候选、模型及权重标识、时间元数据。`source_stem=other` 不得推断为 piano/guitar；乐器类型未知时保存 `null`。Basic Pitch 的 amplitude 原样保存为模型激活摘要，不能改名为主旋律正确率。[音符解码源码](https://github.com/spotify/basic-pitch/blob/main/basic_pitch/note_creation.py)

F0 分段初值：分析步长约 10 ms，在稳定音高平台内以加权中位数估计中心音；偏移超过 70 cents 且持续 60 ms 才生成换音候选，新起音可提前断开。同音重起以 onset 峰、短能量下降及 voicing 中断为证据；先在调参集选择 onset 阈值。保留连续 F0 到完成颤音、滑音判断之后，再生成整数 MIDI 音高。

清洗只合并“同音、间隔不超过 60 ms、无可靠新起音、无明确休止”的碎片。小于 100 ms 的音先标记，结合局部节奏和 onset 决定保留；不按时长直接删除。颤音压缩要求同一持音内出现围绕中心的周期变化；滑音整理保留起点、稳定落点及原始轮廓。每次清洗记录原事件 ID、理由与前后值；真实同音重复与误碎裂必须有成对测试。

### 12.3 人声活动、可靠性与空白语义

分别保存 `scores.activity`、`scores.pitch`、`scores.selection`，附评分方法和版本；不跨模型直接比较原始数值。CREPE 的 confidence 指存在音高，静音也可能出现高值，必须另设静音门控。[CREPE](https://github.com/marl/crepe#using-crepe-from-the-command-line)、[torchcrepe 静音说明](https://github.com/maxrmorrison/torchcrepe#filtering-and-thresholding)

Phase 2 先采用可检查的启发式活动检测：在共同增益音频上计算 100 ms 窗、20 ms 步长的 vocals RMS、原混音 RMS 及有声音高帧占比。初始开启条件为 vocals 高于 -50 dBFS、相对混音高于 -15 dB、有声音高帧占比超过 0.5，连续满足 100 ms；关闭条件为能量低于相应开启阈值 3 dB，或有声占比低于 0.35，并持续 200 ms。零能量分母按噪声底保护，静音先行门控。长尾及填句交界另保留待选区，不能被保持时间强制覆盖。activity 保存规则判定及三个原始特征，未校准标量可为 null；这些条件仅生成候选活动区，无法证明是主唱：纯伴奏泄漏、和声、合唱及无固定音高演唱必须单独标注评估，无法判定主唱时标为待复核。

`rest` 表示有证据支持目标旋律此处没有音；`unknown` 表示不能可靠判定旋律。仅整曲静音、人工标注或足够可靠的目标旋律停顿可生成 rest；单个模型没检出音只能生成 unknown。伴奏仍响时也允许主旋律休止。所有 unknown 保留区间和原因，无新增可靠证据时不能转换成休止、延长邻音或填入伴奏。Phase 3 可凭新增器乐证据将 Phase 2 的 `instrumental_not_analyzed` 区段解析为 note/rest，生成有修改记录的新修订，保留上一版 unknown。

### 12.4 Phase 3 候选生成与软切换

首版先对 `other` 运行 Basic Pitch 获取复音事件，保留其 onset 与激活；该模型并不直接输出感知主旋律，且官方说明单乐器输入效果最佳。将按起音排序的事件构成有向图，同一起音的和弦音为互斥节点。只连接时间前进的节点；踏板或混响造成的时值重叠允许作为“缩短前音”的候选操作，保留原时值和操作代价，禁止直接叠成多声部输出。

以 8 秒窗口、2 秒重叠生成最多 8 条候选轨迹，初始 beam 宽度 32。节点奖励采用 onset 和音高激活各 0.5，按有效持续时间加权，避免碎音越多得分越高；相邻事件代价初值为 `0.08 × 半音距离`，跨越 12 半音另加 0.5，无证据支撑的间隔每秒加 0.3。候选还需包含停顿/unknown 路径；不能因路径必须延伸而强制选音。所有分量先限定到明确尺度，报告轨迹及分项分数，避免不可解释的总分。

首版选择分数明确为 `S = 0.40P + 0.20O + 0.20C + 0.20H`：P 为时长加权音高支持，O 为起音支持，C 为相邻音程绝对值除以 12 后取负指数的均值，H 为原混音中候选音高及其谐波邻域能量占同一分析范围总谱能量的比例。H 初值使用 46 ms Hann 窗、10 ms hop、50–5000 Hz 分析范围，计入基频及前 5 次谐波附近 ±50 cents 的频点并集，每个频点只计一次；分辨率不足的邻域至少覆盖最近一个频点，零能量帧的 H=0，最后按候选有声时长平均。P/O 的尺度变换按后端用调参集固定，H 限于 0–1；不得对每个待测片段重新做 min-max。初始 `S < 0.60`、两个以上有效候选的前两名差值小于 0.08 或来源冲突均输出 unknown；只有一个候选时仍需通过绝对阈值与活动门控。短于两个事件的候选不计算虚构的连续性，改按其余权重重归一。重复主题、音区和琶音特征留作后续可关闭扩展，先证明加入后有净收益；不硬排除低音主旋律或快速旋律。人工可指定区间及候选，不要求前奏、间奏“必须有音”。

活动门控按来源区分：人声使用第 12.3 节的人声候选活动；器乐只要求对应 stem/混音超过静音噪声底且有该候选音高的支持，不能要求 singing activity 为真。先合并在共同时间区间内音高序列、起音和时值等价的重复候选，再比较前两名；否则同一旋律的重复轨迹会造成假歧义。等价匹配初值为同 MIDI 音高、起止各相差≤30ms，合并保留所有来源证据，参数与版本写入报告。

区分 `path_score`（8 秒窗口中保留候选的累计分数）与 `scores.selection`（用于当前时刻选源的局部 S）。局部 S 在所有来源共同的 20ms 绝对时间网格上更新，用以当前时刻为中心的 200ms 区间内有效证据计算；仅离线处理，允许使用窗口后半段。无局部证据的候选不能沿用整窗高分。持续音在其有效音符区间继承该音符的起音支持，局部连续性使用包含当前音符的最近已关联音程；不存在音程时重归一其余权重。150ms 滞回作用于局部 S；活动门控的 semantics 指明检测的是 singing 还是 instrumental。

窗口重叠区先按绝对时间与音高关联同一事件，再用有效中心区组合候选；对边界悬而未决的事件保留候选，记录裁切而不重复发音。来源转换使用同一选择器版本算出的 S，不比较不同模型原始分数；可靠人声且有合法主唱候选时优先人声。用持续时间代价和滞回控制切换，不设“至少一小节”的硬限制。初始要求新候选 S 领先 0.15 并保持 150 ms；可靠人声结束或器乐新起音处允许按起音直接切换，不等待满 150 ms，使短填句可进入。输出边界吸附到可靠起音/收音，必要的截断须留记录。最终验证所有已选音符时间递增、无重叠，rest/unknown 不被穿越；人工指定来源优先于自动规则。候选用 beam 截断，因此只称启发式搜索，不宣称穷尽了所有可能主旋律。

Phase 3 先用人工标注的器乐独奏、钢琴和弦加旋律、琶音伴奏、短填句、低音主旋律及无明确旋律片段验证候选召回和最终选择。若正确旋律未进入候选集，优先修改生成器，不能靠调选择分数掩盖；若候选存在但排序错误，才调整选择器。调参片段与最终验收片段分开保存。

### 12.5 依赖与缓存

音频栈作为可选依赖，与确定性谱面核心分开。先选择实测的 Python 3.11 环境并锁定完整依赖，不声明任意 `>=3.11` 均支持。Basic Pitch 当前依赖配置在 Windows Python ≥3.11 使用受版本上限约束的 TensorFlow；ONNX 可选后端必须显式选择并做一致性比较，不能假定 pip 默认安装它。[Basic Pitch 依赖](https://github.com/spotify/basic-pitch/blob/main/pyproject.toml)

Demucs 原 Meta 仓库已归档，作者 fork 仅处理重要修复；固定仓库提交/发行版及权重校验和。Windows 短音频冒烟验收包含 FFmpeg 解码、模型下载、CPU 推理、设备回退及输出时长。[维护说明](https://github.com/facebookresearch/demucs)

按解码、分轨、原始推理、音符解码、选择/清洗、量化、编配/指法、导出分别缓存。每层键由上游产物哈希、本层代码版本、有效参数、模型权重、运行后端及依赖锁摘要构成；随机推理固定并记录 seed。编配/指法键额外包含 Score revision/hash、profile 内容哈希、成本版本与权重、移调/八度/分句策略。完成后原子写入 manifest 和产物，校验通过才标 complete；失败文件不能命中缓存。改排版只失效导出，改 profile/指法权重只失效编配及导出，改清洗只失效清洗及下游；人工修订以独立版本作为下游输入，保留其父版本，绝不被重新推理覆盖。

### 12.6 节拍、量化与调性估计

Phase 1 优先验证恒速短音频，librosa beat tracker 作为拍点基线。检测的拍点必须附其拍单位假设，检查倍速/半速候选；拍号和小节强拍不从 BPM 直接推出。未实现可靠拍号识别时默认 4/4，明确 `source=assumed`，允许人工设置 BPM、原点、弱起和拍号。变速输入只有在 anchor map 足够覆盖或有 MIDI tempo map 时才声称支持；无法建立可靠映射时输出秒级候选与待核对报告，不伪造确定的节奏谱。[librosa beat_track](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html)

量化在已建立的 time_map 上将每个观测起止映射到连续 q，再按乐句搜索候选网格 `g∈{1,1/2,1/4,1/3,1/6}q`。其中 1/4q 是十六分音符，1/3q 是八分三连音时值；配置不使用含义不明的 `max_grid=1/16`。MIDI/人工 Score 已给定有理数时值时默认不重新量化。

首个实现每句先采用一个网格，给每个起止点列出距离原位置不超过一个 g 的相邻网格点；一般音时值≥g，事件顺序不变、独立起音保留、无重叠、不越过可靠休止/unknown 边界。用动态规划选择联合起止点，而非独立四舍五入后再把零时值音删掉。目标初值为 `平均(|起点偏差_q|+|终点偏差_q|) + 0.02/g`，均以 q 为标度；更细网格付出更高复杂度代价。网格原点为 q=0。人工确认的有理数边界作为额外可用点，边界处允许小于 g 的正有理数残余并标注；音频估计边界若尚无有理数位置，先按该网格提出边界候选并记录原始秒，不能把任意浮点边界当成精确记谱事实。

所有候选均不满足约束、任一音起点移动超过 0.15q，或清晰的二分/三分混合节奏被单一网格误写时，输出 `RHYTHM_REVIEW_REQUIRED` 和未量化观测；可由用户给出有理数节奏后继续。后续再扩展逐小节混合网格，不用缩短句长或合并起音来隐瞒首版限制。起音/终点偏差分开报告，跨小节时只做渲染拆分。

调性基线可以使用按音符时长加权的 pitch-class 直方图与大/小调模板相关性，并输出前两名及差距；模板、阈值和实现版本在开发集固定。相关性是评分而非正确概率，调性不明则使用标为 `assumed` 的记谱参考调或用户值。相对大小调、借用音和转调须单列测试；自动调性不用于强制吸附音高。分段调性暂未实现时，检测到疑似转调区段应提示人工 key_map。

## 13. 测试、验收与阶段准入

本节是待实现的验收约定，不代表已有程序或实测性能。`q` 固定表示四分音符时长；有理数采用第 4 节约定的约分字符串，如 `"1/3"`，分母大于 0。原始音频秒坐标、量化后的谱面 `q`、按速度图生成的播放秒坐标分别保存，不互相覆盖。导出以编辑后的 Score 字段为权威；原始观测字段只供溯源，派生名称与播放时间必须重算。相同输入、配置及版本应得到相同语义输出。

### 13.1 确定性验收矩阵

| ID | 可重复输入/反例 | 必须得到的结果与验证方法 |
|---|---|---|
| T01 时值 | 三音起点 `0,1/3,2/3 q`，各长 `1/3 q`；另加 `3.5 q` 起、长 `1 q` 的音 | 三连音总长严格为 `1 q`；跨 `4 q` 小节线拆成两段 `1/2 q` 并 tie，播放仍为一次发音。JSON→展示→解析比较有理数，不靠浮点近似判小节是否填满。 |
| T02 三种时间 | 原始区间 `[.520,.913]s`，量化为 `[0,1/2]q`，恒速 82 QPM | 原始区间原样保留；从谱面起点播放长 `30/82s`，允许不同于原始 `.393s`。编辑谱面时值不得改写原始秒。 |
| T03 原点/弱起/变速 | `origin_sec=1.25`、`origin_q=0`；弱起长 `1q`，`score_start_q=-1`，首完整小节从 `q=0` 开始；速度 `q=-1:60`、`q=4:120` QPM | 弱起在音频 `.25s`，首完整小节强拍在 `1.25s`；`q=8` 在 `7.25s`、从谱面起点播放 `7s`。不能使用 `audio_sec×单一BPM/60`。另测无弱起、`q=4` 从 4/4 改 3/4，后续小节线为 `4,7,10 q`。 |
| T04 MIDI 时间 | PPQ=480；track 0 在 tick 0/1920 写 1000000/500000 μs/q；显式选择 track 1，音符区间 tick `[0,480]`、`[1920,2400]` | 得到谱面 `[0,1]q`、`[4,5]q`，播放 `[0,1]s`、`[4,4.5]s`。全局速度事件不能因选轨被丢弃；导出再导入保留音高、起止 q、速度和拍号图。 |
| T05 MIDI 拒绝 | 选定轨音符 `[0,480]` 与 `[240,720]` 重叠；type 2；会影响所选声部的 sustain CC64 或非零 pitch bend | 返回明确的未支持/复音错误和位置，不静默截断、吸收踏板或忽略弯音；同 tick 前音 note-off 后音 note-on 合法。跨轨同位置冲突的全局速度事件须拒绝或按显式覆盖处理。 |
| T06 唱名与八度 | G 大调、`do_midi=67`：`G4,F#5,G5`；A 小调、默认 la-based、`do_midi=60`：`A3,C4,A4,G#4` | 分别为 `1 / 7 / 1′` 与 `6, / 1 / 6 / #5`；小调标题明确 `1=C / 6=A`。无点 do 必须有绝对 MIDI 锚点。另在 C 大调、`do_midi=60` 下，MIDI 61 配 C# 或 Db 拼写提示分别出 `#1`/`b2`，TAB 仍为同一音高候选。 |
| T07 Profile/DP | 枚举 profile 所有动作及音高；固定短句 `E4,F4,G4,C5,D5`，保留 C5 的全部合法候选 | 每个动作满足孔数/吹吸/按钮约束，反查恰得指定音高；非法、冲突动作拒绝。对短句穷举所有候选路径，用独立成本求和验证 DP 成本等于枚举最小值；并列解按固定顺序决定。只能证明所定义成本的最优性。 |
| T08 原版/编配 | 最低音 C4，旋律 `B3→C4`；另一句跨度超过整个 profile 音域 | original 音高仍为 `59→60`，标出越界；编配若整句升八度则为 `71→72`，仍上行半音，不得变为 `71→60`。同句采用同一整数八度偏移；无可行方案须报告不可行，不逐音折叠伪造通过。 |
| T09 人工纠错 | 修改 `original_score.json` 中某事件音高和谱面时值后重新 export，并更新 revision_id | 稳定 event ID 和原始来源保留；简谱、TAB、MIDI 一致更新；设置模型调用计数断言为零。非法时值、拼写与音高冲突、重叠事件返回字段级错误；schema version 必填。若仅编辑秒级 `melody_clean.json`，需重新量化后进入 export，但仍不重跑模型。 |
| T10 清洗边界 | 重复 G4：`[0,.20],[.25,.45],[.50,.70]s` 且每段有新起音；碎片 G4：`[0,.18],[.19,.31],[.32,.55]s` 且后两段无新起音证据 | 前者保留三音，后者可合为一音；gap 阈值本身不能决定合并。保留合并前 ID、起音证据与操作记录。 |
| T11 空白语义 | 真静音 `[0,2]s`；未知候选 `[2,4]s`；可靠旋律 `[4,6]s` | 静音输出休止，未知输出待核对区段，可靠区段输出音符；unknown 不得伪装为确认休止，不要求每个非人声时段都有旋律。 |

G 大调展示样例统一为 `D5 E5 G5 F#5`，在 `do_midi=67` 下简谱为 `5 6 1′ 7`，合法 TAB 可用 `5D 6B 7B 6D+`。此处 TAB 是合法映射示例；只有配置成本后才可声称是最优路径。

### 13.2 Phase 0 开发任务与退出门槛

按依赖顺序交付：①版本化 schema、校验器及三种时间坐标；②origin/pickup/tempo/meter map 与有理数转换；③受限 MIDI 导入/导出及拒绝规则；④唱名、拼写、无点 do 锚点和文本节奏语法；⑤profile 校验、候选映射和枚举可验证的 DP；⑥original/编配分离；⑦JSON 人工纠错后重导出与诊断报告。每步先固定上述 fixture 及预期值，再接实现。Phase 0 退出条件为 T01–T09 的当前支持部分及 T11 的显式状态序列化全部通过；T08 只验证原音保留、显式 +12 移调及不可行报告，自动句级搜索留待 Phase 4。T10 在 Phase 0 验证导入/导出不合并两个独立事件；声学起音和碎片清洗留待 Phase 1。错误路径可复现，展示与播放语义一致。无需提前接音频模型。

### 13.3 Phase 1–3 固定评测

先冻结带哈希的 dev/holdout 清单，按歌曲/录音源分组隔离；同曲切片、stem 和增强版本不跨集合。Phase 1 覆盖清唱、重复音、颤音和快速句；Phase 2 覆盖完整混音中的人声区；Phase 3 加前奏、间奏、竞争旋律及无旋律片段。参数仅用 dev 调整，holdout 每个发布候选评一次；记录版本、配置、硬件、每曲结果及失败片段。

音符匹配采用最大一对一匹配：音高误差≤50 cents、起音误差≤50ms；分别报告不检查结束时间的 F1 和结束误差≤`max(50ms,参考时长×20%)`的 F1。`P=匹配数/预测数`，`R=匹配数/参考数`，`F1=2PR/(P+R)`；空分母单独列为 N/A，空白片段另计误发音。采用 [mir_eval transcription](https://mir-eval.readthedocs.io/stable/api/transcription.html) 的定义和固定版本。

旋律层同时报告有旋律帧召回、无旋律帧误报率、50-cent raw pitch accuracy、含静音判断的 overall accuracy；chroma accuracy 只作诊断，不能掩盖八度错误，依据 [mir_eval melody](https://mir-eval.readthedocs.io/latest/api/melody.html)。参考标注 unknown 区域排除精确正确率并报告覆盖率；预测 unknown 在已知有旋律区计漏检，不能靠弃答提高分数。分源选择准确率需使用固定来源标签单独统计，不用音高正确率冒充。

建议初始门槛：Phase 1 holdout 宏平均 note-onset F1≥0.85、无旋律帧误报率≤0.05；Phase 2/3 首轮先登记基线与分层结果，再在下一轮开发前锁定门槛。以上数字只是建议基线，未经本项目实测。所有阶段还须零非法孔位，并由口琴演奏者对可辨识度、节奏可读性和顺畅度分别评分；枚举最优不等于人工好吹。

## 14. 项目结构与开发任务

建议保留原稿分层，增加时间映射、MIDI 导入、渲染事件及修订记录模块：

```text
src/harmonica_transcriber/
  models.py                   版本化模型、校验与规范化
  cli.py                      命令与能力检查
  io/json_io.py               无损读写与版本迁移
  io/midi_import.py           选轨、事件配对、tempo/meter/key map
  theory/time_map.py          q↔sec、弱起与变速
  theory/jianpu.py            音级/八度/拼写与反解
  theory/key.py               Phase 1 调性估计
  harmonica/profile.py        正向物理音表与反向候选
  harmonica/cost.py           统一成本及版本
  harmonica/optimizer.py      DP 与路径报告
  harmonica/arrangement.py    显式移调；Phase 4 乐句八度搜索
  score/render_events.py     小节拆分、tie、休止和 unknown
  export/markdown.py
  export/midi.py
  audio/                     Phase 1/2 适配与分轨
  melody/                    证据→音符、清洗、候选与选择
profiles/
tests/fixtures/
examples/
```

Phase 0 的最小运行依赖可选 Pydantic、Typer、PyYAML、Mido；pytest 仅为开发依赖。不提前引入全部音频框架、music21 或可视化系统。Python 3.11.x 为初始验证目标，精确补丁版本和依赖版本由首次安装验证后写入锁文件；本文不伪造已验证的版本组合。

Phase 0 的开发顺序：模型/校验 → time_map/MIDI → profile → 简谱及反解 → 成本/DP → 渲染事件与导出 → CLI → 验收 fixture。每一步只消费上一层的明确契约，外部格式异常在入口报告。

### 14.1 可直接用于后续开发的 Phase 0 任务说明

> 按本 V2 文档实现 Phase 0。输入为符合 schema 的人工 JSON 或选定单旋律 MIDI 轨，不接音频模型。先实现有理数时间、tempo/meter/key map、弱起及原始/演奏数据分离；使用可配置正向物理音表生成全部合法指法候选；用统一成本 DP 选全句路径。实现 la-based 小调、明确 do_midi 的简谱及反解、完整文本时值和跨小节 tie；简谱/TAB/MIDI 共用事件。支持显式整数移调，默认越界拒绝编配并报告，不逐音折返。完成本文件 Phase 0 全部验收，输出运行方式、结果与剩余能力边界。未来功能未实现时明确报错；不要用占位分数或示例谱冒充算法结果。

Phase 1 先建立同一开发集上的模型比较和可重复 fixture，再选默认后端。Phase 2 验证分轨与活动门控。Phase 3 先验证候选生成，再验证选择；每阶段保持上一阶段验收通过。Phase 4 新增编配目标时同步扩展可验证状态与成本函数。原稿中“无人声区段必须补全”和“original 模式下逐音八度修正”的约定由本版的有证据补全及独立编配规则替代。

Skill 封装在 CLI 稳定后增加：定位输入→能力检查→运行→读取 report→返回谱与低可靠区段；用实际产物和报告答复，不让 LLM 猜音。人工编辑 Score 后直接 export，不再调用分轨和音高模型。

## 15. 主要变更与外部资料

| 原稿问题 | V2 对应处理 |
|---|---|
| 单一 BPM 直接乘秒 | 显式原点、q 单位、弱起、tempo/anchor map、MIDI tick 契约 |
| 仅 gap 合并同音 | 保护独立起音、保留帧级证据、正反两类 fixture |
| Markdown 时值不完整 | 每事件显式有理数时值、休止/未知、跨小节 tie、TAB 同步 |
| 小调/八度/拼写未定义 | la-based minor、do_midi、拼写优先级与音高反解 |
| 原始旋律与八度编配混用 | 原始与编配分离、句级调整、逐项日志与不同验收 |
| 指法与移调评分不一致 | 同一版本化成本、秒级速度和停顿、最优性边界 |
| 缺少器乐候选生成方法 | 明确研究基线、可拒选、短填句和来源切换规则 |
| 不同 confidence 混用 | 分数分层、保留原始分数语义与校准状态 |
| 依赖和缓存粗略 | 分层可选依赖、实测锁定、逐阶段缓存与输入哈希 |
| 验收只有“基本正确” | 确定性硬约束、带容差指标、固定开发/保留集及准入条件 |
| G 大调简谱/TAB 对不上 | 数据与谱例统一为 D5 E5 G5 F#5 |

外部资料用于核对工具能力与接口；本文算法阈值、成本权重及阶段目标均为项目设计，须通过实测标定。

- [Spotify Basic Pitch：能力、兼容环境、模型运行后端](https://github.com/spotify/basic-pitch)
- [Basic Pitch：音符与起音解码源码](https://github.com/spotify/basic-pitch/blob/main/basic_pitch/note_creation.py)
- [Demucs：分轨、增益与运行参数、维护状态](https://github.com/facebookresearch/demucs)
- [CREPE：单声部音高与时间戳](https://github.com/marl/crepe)
- [torchcrepe：过滤、阈值与静音限制](https://github.com/maxrmorrison/torchcrepe)
- [librosa pYIN：F0、有声输出及 center 约定](https://librosa.org/doc/0.11.0/generated/librosa.pyin.html)
- [librosa beat_track：速度与拍点接口](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html)
- [Mido：MIDI 时间、tempo、文件类型](https://mido.readthedocs.io/en/stable/files/midi.html)
- [music21：Pitch 与推断拼写](https://music21.org/music21docs/moduleReference/modulePitch.html)
- [MusicXML：时值、休止和 tie 的独立表示](https://www.w3.org/2021/06/musicxml40/tutorial/midi-compatible-part/)
- [mir_eval：音符转录匹配](https://mir-eval.readthedocs.io/en/stable/api/transcription.html)
- [mir_eval：旋律、有声与无声评价](https://mir-eval.readthedocs.io/en/stable/api/melody.html)
- [HOHNER：CX-12 新版官方音表](https://www.hohner-cshop.de/out/media/HOHNER_CX-12_new_de.pdf)
