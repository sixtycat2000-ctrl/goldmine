import { useId, type RefObject } from "react";
import type { CellTone, MechanismMode } from "./mechanism-model";

type Point = [number, number, number];
const project = ([x, y, z]: Point) => [360 + (x - y) * 0.866, 157 + (x + y) * 0.45 - z];
const polygon = (points: Point[]) => points.map((p, i) => `${i ? "L" : "M"}${project(p).join(",")}`).join(" ") + "Z";
const segment = (a: Point, b: Point) => `M${project(a).join(",")}L${project(b).join(",")}`;
const ring = (x: number, y: number, z: number, radius: number) => polygon(
  Array.from({ length: 40 }, (_, i): Point => [x + Math.cos(i * Math.PI / 20) * radius, y + Math.sin(i * Math.PI / 20) * radius, z]),
);

function Block({ x, y, z = 0, w, d, h, tone = "quiet", dark = false }: {
  x: number; y: number; z?: number; w: number; d: number; h: number; tone?: CellTone; dark?: boolean;
}) {
  const a: Point = [x, y, z + h], b: Point = [x + w, y, z + h];
  const c: Point = [x + w, y + d, z + h], e: Point = [x, y + d, z + h];
  return <g className={`mi-block mi-${tone}${dark ? " mi-dark" : ""}`}>
    <path className="mi-side-a" d={polygon([e, c, [x + w, y + d, z], [x, y + d, z]])} />
    <path className="mi-side-b" d={polygon([b, c, [x + w, y + d, z], [x + w, y, z]])} />
    <path className="mi-top" d={polygon([a, b, c, e])} />
    <path className="mi-bevel" d={segment([x + 1, y + d - 1, z + h], [x + w - 1, y + d - 1, z + h])} />
  </g>;
}

const FEET = [[-193, -82], [168, -82], [-193, 63], [168, 63]];
const SCREWS = [[-196, -87], [191, -87], [-196, 86], [191, 86]];
const CELL_POSITIONS = Array.from({ length: 26 }, (_, i) => [-178 + (i % 13) * 24, -66 + Math.floor(i / 13) * 39]);

/** One assembled instrument, about sixty purposeful parts; only its carriage moves per frame. */
export function MechanismDrawing({ mode, cells, needle, energized, carriageRef, lang }: {
  mode: MechanismMode; cells: CellTone[]; needle: number; energized: boolean;
  carriageRef: RefObject<SVGGElement | null>; lang: "zh" | "en";
}) {
  const id = useId().replace(/:/g, "");
  const titleId = `mechanism-title-${id}`;
  const dial = Math.min(1, Math.max(0, needle));
  const theta = (-150 + dial * 300) * Math.PI / 180;
  const center: Point = [173, -39, 48];
  const tip: Point = [center[0] + Math.sin(theta) * 19, center[1] - Math.cos(theta) * 19, center[2] + 0.3];
  return (
    <svg className="mechanism-instrument" viewBox="0 0 720 360" role="img" aria-labelledby={titleId} data-instrument={mode}>
      <title id={titleId}>{lang === "zh" ? "检索实验台：向量匣、检测滑台与同一模型驱动的读数指针" : "Retrieval workbench: vector cassettes, an inspection carriage and a model-driven dial"}</title>
      <defs>
        <linearGradient id={`deck-${id}`} x1="0" x2="1" y1="0" y2="1">
          <stop stopColor="#30323b" /><stop offset="0.5" stopColor="#191c24" /><stop offset="1" stopColor="#11141b" />
        </linearGradient>
        <radialGradient id={`shadow-${id}`}><stop stopColor="#000" stopOpacity="0.75" /><stop offset="1" stopColor="#000" stopOpacity="0" /></radialGradient>
      </defs>
      <ellipse cx="360" cy="290" rx="302" ry="55" fill={`url(#shadow-${id})`} />
      {FEET.map(([x, y]) => <Block key={`${x}-${y}`} x={x} y={y} z={-25} w={25} d={25} h={12} dark />)}
      <Block x={-213} y={-103} z={-15} w={426} d={206} h={15} dark />
      <path d={polygon([[-211, -101, 0], [211, -101, 0], [211, 101, 0], [-211, 101, 0]])} fill={`url(#deck-${id})`} stroke="#4a4d58" strokeWidth="0.8" />
      <path d={polygon([[-204, -94, 0.2], [204, -94, 0.2], [204, 94, 0.2], [-204, 94, 0.2]])} fill="none" stroke="#555968" strokeOpacity="0.3" strokeWidth="0.7" />
      {SCREWS.map(([x, y]) => <g key={`${x}-${y}`}>
        <path d={ring(x, y, 0.7, 2.3)} fill="#0d0f15" stroke="#727786" strokeWidth="0.7" />
        <path d={segment([x - 1.2, y, 0.9], [x + 1.2, y, 0.9])} stroke="#9297a4" strokeWidth="0.5" />
      </g>)}

      {/* The cassette has a real rim, hinges and seated cards. */}
      <Block x={-188} y={-77} z={1} w={332} d={82} h={5} dark />
      <Block x={-188} y={-77} z={6} w={332} d={4} h={8} />
      <Block x={-188} y={-73} z={6} w={4} d={75} h={8} />
      {[-151, 77].map((x) => <Block key={x} x={x} y={-81} z={1} w={15} d={7} h={8} />)}
      {CELL_POSITIONS.map(([x, y], i) => (
        <g key={i} data-cell={i} data-tone={cells[i] ?? "quiet"}>
          <Block x={x} y={y} z={6} w={18} d={29} h={9} tone={cells[i] ?? "quiet"} />
          <path d={segment([x + 4, y + 7, 15.1], [x + 14, y + 7, 15.1])} className="mi-etch" />
          <path d={segment([x + 4, y + 12, 15.1], [x + 11, y + 12, 15.1])} className="mi-etch" />
        </g>
      ))}
      <Block x={-188} y={2} z={1} w={332} d={4} h={13} />
      <Block x={140} y={-73} z={1} w={4} d={75} h={13} />
      <Block x={-39} y={4} z={1} w={22} d={7} h={7} />

      {/* Twin guide rails, their four stops and the carriage's four bushings. */}
      {[38, 74].map((y) => <g key={y}>
        <Block x={-188} y={y} z={2} w={374} d={5} h={6} />
        <path d={segment([-181, y + 1, 8.2], [179, y + 1, 8.2])} stroke="#b7bbc3" strokeOpacity="0.48" strokeWidth="0.8" />
        {[-193, 184].map((x) => <Block key={x} x={x} y={y - 4} z={0} w={9} d={13} h={15} />)}
      </g>)}
      <g ref={carriageRef} data-carriage="" transform="translate(0 0)">
        {[-10, 9].flatMap((x) => [35, 71].map((y) => <Block key={`${x}-${y}`} x={x} y={y} z={7} w={8} d={11} h={7} dark />))}
        <Block x={-15} y={30} z={13} w={40} d={59} h={13} tone={energized ? "keep" : "quiet"} />
        <Block x={-9} y={22} z={23} w={27} d={12} h={4} dark />
        {[0, 7, 14].map((x) => <path key={x} d={segment([x - 5, 24, 27.1], [x - 5, 33, 27.1])} stroke="#c6c9d0" strokeWidth="0.6" />)}
        <path d={polygon([[-9, 43, 26.2], [17, 43, 26.2], [17, 69, 26.2], [-9, 69, 26.2]])} fill="#0d1116" stroke="#818996" strokeWidth="0.7" />
        <path d={segment([-5, 55, 26.4], [13, 55, 26.4])} stroke={energized ? "var(--color-gold)" : "#68717d"} strokeWidth="2" />
      </g>

      {/* Dial on a short post: its needle is derived from the same result as HTML. */}
      <Block x={164} y={-48} z={1} w={18} d={18} h={33} dark />
      <path d={ring(173, -39, 34, 28)} fill="#090c11" stroke="#5e6572" strokeWidth="1" />
      <path d={ring(173, -39, 48, 28)} fill="#202530" stroke="#949ba6" strokeWidth="1.2" />
      <path d={ring(173, -39, 48.2, 24)} fill="#0c1118" stroke="#454d59" strokeWidth="0.6" />
      {Array.from({ length: 11 }, (_, i) => {
        const a = (-150 + i * 30) * Math.PI / 180;
        return <path key={i} d={segment([173 + Math.sin(a) * 19, -39 - Math.cos(a) * 19, 48.5], [173 + Math.sin(a) * 22, -39 - Math.cos(a) * 22, 48.5])} stroke={i === 0 || i === 10 ? "#9ca4ae" : "#65707e"} strokeWidth="0.7" />;
      })}
      <path d={segment(center, tip)} stroke="var(--color-gold)" strokeWidth="2" strokeLinecap="round" data-needle="" />
      <path d={ring(173, -39, 48.7, 2)} fill="#f5b041" />

      {/* The front ruler, control knob and return socket finish the instrument. */}
      <Block x={-137} y={89} z={1} w={255} d={7} h={1.2} dark />
      {Array.from({ length: 43 }, (_, i) => <path key={i} d={segment([-133 + i * 5.8, 89.2, 2.3], [-133 + i * 5.8, i % 5 ? 92 : 95, 2.3])} stroke={i % 5 ? "#5e6673" : "#a7afb9"} strokeWidth={i % 5 ? "0.5" : "0.7"} />)}
      <Block x={-184} y={73} z={1} w={28} d={24} h={13} />
      <path d={ring(-170, 85, 15, 10)} fill="#343b47" stroke="#a3aab4" strokeWidth="0.8" />
      {Array.from({ length: 12 }, (_, i) => {
        const a = i * Math.PI / 6;
        return <path key={i} d={segment([-170 + Math.cos(a) * 7, 85 + Math.sin(a) * 7, 15.2], [-170 + Math.cos(a) * 9, 85 + Math.sin(a) * 9, 15.2])} stroke="#89929d" strokeWidth="0.7" />;
      })}
      <Block x={164} y={64} z={1} w={26} d={30} h={13} dark />
      <path d={ring(177, 79, 14.2, 8)} fill={energized ? "#49391f" : "#090d12"} stroke={energized ? "var(--color-gold)" : "#727d8c"} strokeWidth="1.4" />
      <path d={segment([174, 76, 14.5], [180, 82, 14.5])} stroke={energized ? "var(--color-gold)" : "#727d8c"} strokeWidth="1.2" />
    </svg>
  );
}
