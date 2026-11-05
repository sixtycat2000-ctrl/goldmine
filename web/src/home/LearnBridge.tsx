import { Reveal } from "../components/motion/Reveal";
import { useLang, pick } from "../lib/lang";

// Keep this entry small: the lesson corpus belongs to /learn, not its landing link.
const LESSONS = [
  {
    lessonId: "z-score",
    mark: "z",
    title: { zh: "看懂一个检索分数", en: "Read a search score" },
    text: { zh: "换一组语料，看 z 分数怎样变化。", en: "Change the corpus and watch its z-score." },
  },
  {
    lessonId: "exact-candidate-pruning",
    mark: "∪",
    title: { zh: "少建对象，保留原结果", en: "Build fewer file records" },
    text: { zh: "向量仍照算，只减少不可能入选文件的对象创建。", en: "Score every vector; materialize only files that could qualify." },
  },
  {
    lessonId: "empty-posting-intersection",
    mark: "∅",
    title: { zh: "没有候选，少查一次", en: "No candidates, one less lookup" },
    text: { zh: "正文核验的候选为空时，跳过倒排查询。", en: "An empty candidate set can skip the posting lookup." },
  },
  {
    lessonId: "read-snapshot",
    mark: "v",
    title: { zh: "一次请求，同一快照", en: "One request, one snapshot" },
    text: { zh: "发布新版本，旧请求继续读原来的数据。", en: "Publish a version while the old request keeps its view." },
  },
  {
    lessonId: "reranker-lease",
    mark: "↔",
    title: { zh: "模型借用，有借有还", en: "Acquire a model, return its lease" },
    text: { zh: "最后一位使用者归还后，才开始计算空闲时间。", en: "Idle time starts after the last holder returns its lease." },
  },
] as const;

export function LearnBridge() {
  const lang = useLang();
  return (
    <section id="learn" className="mx-auto max-w-6xl px-5 py-24 sm:py-28" aria-labelledby="home-learn-title">
      <Reveal>
        <div className="mb-10 flex flex-wrap items-end justify-between gap-5">
          <h2 id="home-learn-title" className="font-display text-display leading-tight text-ink">
            {pick(lang, "用五个实验，看懂检索", "Understand search through five experiments")}
          </h2>
          <a className="inline-flex min-h-11 items-center gap-2 text-[14px] text-gold hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-gold"
            href="/learn" data-home-learn-all="">
            {pick(lang, "查看全部 150 个概念", "Explore all 150 concepts")} <span aria-hidden="true">→</span>
          </a>
        </div>
      </Reveal>
      <div className="grid overflow-hidden rounded-2xl border border-line bg-bg1/70 sm:grid-cols-2 lg:grid-cols-5">
        {LESSONS.map((lesson, index) => (
          <a key={lesson.lessonId} href={`/learn#c=${lesson.lessonId}`} data-home-lesson={lesson.lessonId}
            className={`group flex min-h-52 flex-col border-line p-5 transition-colors hover:bg-bg2 focus-visible:z-10 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-gold ${index ? "border-t lg:border-t-0 lg:border-l" : ""}`}>
            <span className="mb-7 flex items-center justify-between" aria-hidden="true">
              <span className="flex size-10 items-center justify-center rounded-lg border border-gold/20 bg-gold/5 font-display text-2xl text-gold">{lesson.mark}</span>
              <span className="text-sub transition-transform group-hover:translate-x-1 group-hover:text-gold motion-reduce:transform-none">↗</span>
            </span>
            <h3 className="text-[16px] font-medium leading-snug text-ink">{pick(lang, lesson.title.zh, lesson.title.en)}</h3>
            <p className="mt-3 text-[13px] leading-relaxed text-ink2">{pick(lang, lesson.text.zh, lesson.text.en)}</p>
          </a>
        ))}
      </div>
    </section>
  );
}
