import { useRef, type KeyboardEvent } from "react";
import { useLang } from "../lib/lang";
import { HOME_DEMO_SCENES, type Scene } from "./home-demo-data";

export type { Scene } from "./home-demo-data";

export function SceneTabs({ scene, onScene, panelId }: {
  scene: Scene;
  onScene: (scene: Scene) => void;
  panelId: string;
}) {
  const lang = useLang();
  const tabs = useRef<Array<HTMLButtonElement | null>>([]);

  const onKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    let next: number;
    switch (event.key) {
      case "ArrowRight": next = (index + 1) % HOME_DEMO_SCENES.length; break;
      case "ArrowLeft": next = (index + HOME_DEMO_SCENES.length - 1) % HOME_DEMO_SCENES.length; break;
      case "Home": next = 0; break;
      case "End": next = HOME_DEMO_SCENES.length - 1; break;
      default: return;
    }
    event.preventDefault();
    onScene(next as Scene);
    tabs.current[next]?.focus({ preventScroll: true });
  };

  return (
    <div className="home-scene-tabs" role="tablist" aria-label={lang === "zh" ? "选择查找示例" : "Choose a search example"}>
      {HOME_DEMO_SCENES.map((item, index) => (
        <button
          key={item.id}
          ref={(element) => { tabs.current[index] = element; }}
          id={`${panelId}-tab-${item.id}`}
          type="button"
          role="tab"
          aria-selected={scene === index}
          aria-controls={panelId}
          tabIndex={scene === index ? 0 : -1}
          onClick={() => onScene(index as Scene)}
          onKeyDown={(event) => onKey(event, index)}
        >
          {item.label[lang]}
        </button>
      ))}
    </div>
  );
}
