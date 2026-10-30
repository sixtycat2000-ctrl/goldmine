import { Reveal } from "./motion/Reveal";
import { useLang, pick } from "../lib/lang";

type Bil = { zh: string; en: string };

/* taste-skill round: the eyebrow kicker and the 01-07 ghost numeral are gone —
   the heading names the section, and that is the whole job. */
export function SectionHead({ title, lede }: { title: Bil; lede?: Bil }) {
  const lang = useLang();
  return (
    <div className="relative mb-10">
      <Reveal>
        <h2 className="max-w-[16em] font-display text-display leading-[1.1] text-ink">
          {pick(lang, title.zh, title.en)}
        </h2>
        {lede && (
          <p className="mt-4 max-w-[34em] text-[15px] text-ink2">{pick(lang, lede.zh, lede.en)}</p>
        )}
      </Reveal>
    </div>
  );
}
