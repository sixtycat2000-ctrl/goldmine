import { useId } from "react";
import { IconSearch } from "../components/Icons";
import type { Lang } from "../lib/lang";
import { SceneTabs } from "./SceneTabs";
import { HOME_DEMO_SCENES, type HomeDemoScene, type Scene } from "./home-demo-data";

function FileGlyph({ kind }: { kind: HomeDemoScene["id"] }) {
  return (
    <svg viewBox="0 0 32 36" width="32" height="36" fill="none" aria-hidden="true">
      {kind === "screenshot" ? <>
        <rect x="2.5" y="6.5" width="27" height="23" rx="3" />
        <path d="m5.5 25 7-7 5 5 4-4 5 5" />
        <circle cx="22" cy="13" r="2" />
      </> : <>
        <path d="M7 2.5h12l7 7v22a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-27a2 2 0 0 1 2-2Z" />
        <path d="M19 3v7h7" />
        {kind === "identifier" ? <path d="m13 17-3 4 3 4m6-8 3 4-3 4m-2-10-2 12" /> : <path d="M10 17h11m-11 5h11m-11 5h7" />}
      </>}
    </svg>
  );
}

/** Highlight text that is present in the illustration, without inventing a score. */
function EvidenceExcerpt({ item, lang }: { item: HomeDemoScene; lang: Lang }) {
  const text = item.excerpt[lang];
  const phrase = item.id === "semantic" ? (lang === "zh" ? "四十五天" : "forty-five days") : item.query[lang];
  const at = text.indexOf(phrase);
  if (at < 0) return <>{text}</>;
  return <>{text.slice(0, at)}<mark>{phrase}</mark>{text.slice(at + phrase.length)}</>;
}

export function HomeSearchDemo({ scene, onScene, lang }: { scene: Scene; onScene: (scene: Scene) => void; lang: Lang }) {
  const uid = useId().replace(/:/g, "");
  const panelId = `home-search-${uid}`;
  const item = HOME_DEMO_SCENES[scene];
  const file = item.file[lang];
  const basename = file.slice(file.lastIndexOf("/") + 1);

  return (
    <div className="home-search-demo" data-home-demo="" data-home-demo-kind={item.id}>
      <div className="home-demo-topline">
        <span className="home-demo-caption"><IconSearch size={15} />{lang === "zh" ? "搜索结果" : "Search result"}</span>
        <span className="home-demo-example">{lang === "zh" ? "示例" : "Example"}</span>
      </div>
      <SceneTabs scene={scene} onScene={onScene} panelId={panelId} />
      <section id={panelId} className="home-demo-panel" role="tabpanel" aria-labelledby={`${panelId}-tab-${item.id}`} tabIndex={0} data-home-scene={item.id}>
        <div className="home-demo-query">
          <IconSearch size={19} />
          <p><span className="sr-only">{lang === "zh" ? "查找：" : "Query: "}</span><span data-home-query="">{item.query[lang]}</span></p>
        </div>
        <div key={item.id} className="home-demo-result">
          <div className="home-demo-file-row">
            <span className="home-demo-file-icon"><FileGlyph kind={item.id} /></span>
            <div className="home-demo-file-info">
              <h2 className="home-demo-filename" data-home-filename="">{basename}</h2>
              <p className="home-demo-path" data-home-file="">{file}</p>
            </div>
          </div>
          <blockquote className="home-demo-excerpt" data-home-excerpt=""><EvidenceExcerpt item={item} lang={lang} /></blockquote>
          <div className="home-demo-source">
            <p className="home-demo-location" data-home-location="">
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 3.5h10M3 8h10M3 12.5h6" /></svg>
              <span>{item.location[lang]}</span>
            </p>
            <span className="home-demo-evidence">{item.evidence[lang]}</span>
          </div>
        </div>
      </section>
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{`${item.query[lang]} — ${basename}, ${item.location[lang]}`}</p>
    </div>
  );
}
