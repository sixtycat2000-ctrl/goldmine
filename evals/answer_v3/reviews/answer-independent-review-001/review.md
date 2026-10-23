# Independent source/semantic review

All 80 tasks were audited against the permitted frozen sources before answerer/judge jobs. The current draft needs revision. This is an independent AI review, not human professional certification.

Per-question dispositions: 49 answerable source labels acceptable; 9 answerable tasks need semantic/evidence revision; 2 cross tasks need a strict two-file-necessity decision;20 traps have no supported answer in the supplied corpus.

The review found9required correction findings and2cross-file design findings. Exact source text, original asset/view SHA pins, line locations, and zero-based half-open character offsets are in corrections.json. Upstream data and this draft were not edited.

| Finding | Affected tasks | Required change |
| --- | --- | --- |
| IR-001 | av3-multi-012 | Fact: Set count=True on click.option; each repetition increments the integer verbosity level. Extend p1_e0 through the actual decorator at line237, for example the supplied lines230-239. |
| IR-002 | av3-single-002, av3-multi-002 | Retain the fact and replace both reused spans with lines437-452, which include value extraction and if stop: break. |
| IR-003 | av3-multi-004 | Fact: v-show 在 DOM 中保留元素，只切换元素的 CSS display 属性。 |
| IR-004 | av3-multi-005 | Fact: 父组件没有提供值时，父值仍可为 undefined，而子组件使用 defineModel 的 default（示例为1），所以父子值不同步。 |
| IR-005 | av3-multi-005 | Fact: v-model:title 对应子组件的 title prop 和 update:title 事件。 |
| IR-006 | av3-multi-009 | Fact: 在 v-model 后添加 .trim 修饰符，可自动去除用户输入两端的空格。 |
| IR-007 | av3-multi-014 | Retain the fact; extend p2_e0 to lines127-143, showing the function meaning, same-host prerequisite, HTTP80/None to HTTPS443/None conditions, and return False. |
| IR-008 | av3-single-020 | Retain the fact and whole-utterance time range; extend the text span to the complete utterance (chars0-129) so it proves the causal relation. Do not invent internal word timestamps. |
| IR-009 | av3-cross-008 | Question first half: How can all Poetry virtual environments associated with the current project be removed? Retain the second half. Fact: poetry env remove --all removes all virtual environments associated with the current project. The source context and --all instruction are supplied here. |
| IR-010 | av3-cross-011 | Before claiming that every cross task needs both files, replace/revise this task under an explicit pre-run data revision, or explicitly narrow the class definition to two annotated source facts rather than two-file necessity. This review does not choose new facts or alter selection. |
| IR-011 | av3-cross-007 | Before claiming that every cross task needs both files, replace/revise this task under an explicit pre-run data revision, or narrow the class definition as described in IR-010. Preserve correct current facts if the task is retained as annotated cross-file aggregation. |

Every frozen view hash, every code/doc asset hash, all 100 positive spans, and all recorded locations matched. All selected digital-PDF quotes were found in the PDF text. Bell patent page3, Lincoln patent page2, and the HTTPX screenshot were inspected visually. Header-only EXIF reads confirmed the 3 selected timestamps and found no timezone offset tags. Frozen publisher transcripts establish the 2 selected audio facts; no fresh ASR or listening audit was done.

The20 multi composites retain both original requests and genuinely distinct source facts, with disjoint marked intervals in shared views. All 20cross tasks have2annotated source paths and2facts, but the stronger necessity of both files is weakened for cross-007 and cross-011. Cross tasks mostly aggregate two questions; this is not evidence of relational synthesis difficulty.

The 80 tasks cover 29 answerable source groups;20 cross tasks concentrate in 6 groups.100 original-question uses cover 88 IDs, with 12 IDs repeated.100 positive fact occurrences cover 88 original fact IDs. These are 80 tasks, not 80 independent samples.

All 20 traps were checked against the full122-source inventory, all 175-view discriminator search, relevant source context, and all20publisher transcripts. Literal absence alone was not treated as proof. Only 1 of the 20 had an earlier independent fixed-sample audit; the new per-trap review is in trap-independent-audit.json.

Full local answerer/judge model/settings preregistration remains MISSING. No model scores, generated answers, quality/pass results, capacity databases, retrieval results, or holdout content were read or produced. Resolve the findings in a newly pinned explicit revision and request closure review before any answerer/judge run.

Artifacts: review.json, question-verdicts.json (80 rows), corrections.json, evidence-readback.json (100 rows), source-pins.json (122 sources / 175 views), trap-independent-audit.json (20 rows), transformation-check.json. The selected binary-source checks and 2 scan renders are in source-checks/.
