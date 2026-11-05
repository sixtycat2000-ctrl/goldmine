import { Nav } from "../components/Nav";
import { Footer } from "../components/Footer";
import { Hero } from "./Hero";
import { Bento } from "./Bento";
import { Narrative } from "./Narrative";
import { MetricsBar } from "./MetricsBar";
import { HeadToHead } from "./Tables";
import { Closing } from "./Closing";
import { LearnBridge } from "./LearnBridge";
import { T } from "../lib/lang";

export function HomeApp() {
  return (
    <>
      <a
        href="#features"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:bg-bg3 focus:px-4 focus:py-2 focus:text-ink"
      >
        <T zh="跳到正文" en="Skip to content" />
      </a>
      <Nav page="home" />
      <main>
        <Hero />
        <Bento />
        <Narrative />
        <MetricsBar />
        <LearnBridge />
        <HeadToHead />
        <Closing />
      </main>
      <Footer />
    </>
  );
}
