import { Nav } from "../components/Nav";
import { Footer } from "../components/Footer";
import { Hero } from "./Hero";
import { Bento } from "./Bento";
import { Narrative } from "./Narrative";
import { MetricsBar } from "./MetricsBar";
import { Scenarios } from "./Scenarios";
import { HeadToHead } from "./Tables";
import { Closing } from "./Closing";

export function HomeApp() {
  return (
    <>
      <a
        href="#features"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:bg-bg3 focus:px-4 focus:py-2 focus:text-ink"
      >
        Skip to content
      </a>
      <Nav page="home" />
      <main>
        <Hero />
        <Bento />
        <Narrative />
        <MetricsBar />
        <Scenarios />
        <HeadToHead />
        <Closing />
      </main>
      <Footer />
    </>
  );
}
