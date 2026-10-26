import { Reveal } from "./motion/Reveal";
import { useLang, pick } from "../lib/lang";

type Bil = { zh: string; en: string };

export function SectionHead({
  no,
  kicker,
  title,
  lede,
}: {
  no: string;
  kicker: Bil;
  title: Bil;
  lede?: Bil;
}) {
  const lang = useLang();
  return (
    <div className="relative mb-10">
      <span
        className="pointer-events-none absolute -top-8 right-0 select-none font-display text-[7rem] leading-none text-ink/[0.045] sm:text-[9rem]"
        aria-hidden="true"
      >
        {no}
      </span>
      <Reveal>
        <p className="kicker">{pick(lang, kicker.zh, kicker.en)}</p>
        <h2 className="mt-3 max-w-[16em] font-display text-display leading-[1.1] text-ink">
          {pick(lang, title.zh, title.en)}
        </h2>
        {lede && (
          <p className="mt-4 max-w-[46em] text-[15px] text-ink2">{pick(lang, lede.zh, lede.en)}</p>
        )}
      </Reveal>
    </div>
  );
}
