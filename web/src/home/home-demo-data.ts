/** Authored product illustrations: no live index, timings or relevance scores. */
export type Scene = 0 | 1 | 2;
type Words = { zh: string; en: string };
export type HomeDemoScene = {
  id: "semantic" | "identifier" | "screenshot";
  label: Words;
  query: Words;
  file: Words;
  location: Words;
  excerpt: Words;
  evidence: Words;
  lessonId: string;
};

export const HOME_COPY = {
  zh: {
    title: "按内容找本机文件",
    description: "文档里的一段话、录音里的一句话、截图上的报错，都可以用来查找。自己选文件夹，在这台 Mac 上建索引；从结果查看命中的文件和片段。",
  },
  en: {
    title: "Find local files by content.",
    description: "Find a passage from a document, a sentence in a recording, or an error in a screenshot. Choose your folders, index them on this Mac, and see the matching files and excerpts.",
  },
} as const;

export const HOME_DEMO_SCENES = [
  {
    id: "semantic",
    label: { zh: "描述内容", en: "Describe it" },
    query: { zh: "退款最晚什么时候申请？", en: "When is the refund deadline?" },
    file: { zh: "资料/售后政策.md", en: "Documents/Returns policy.md" },
    location: { zh: "第 18–20 行", en: "Lines 18–20" },
    excerpt: {
      zh: "收到商品后四十五天内可申请退货。请保留订单号与付款凭证。",
      en: "Returns can be requested within forty-five days of receiving the item. Keep the order number and proof of payment.",
    },
    evidence: { zh: "内容匹配", en: "Content match" },
    lessonId: "z-score",
  },
  {
    id: "identifier",
    label: { zh: "精确编号", en: "Exact ID" },
    query: { zh: "ZT-84", en: "ZT-84" },
    file: { zh: "项目/部署记录.md", en: "Projects/Deployment notes.md" },
    location: { zh: "第 42–44 行", en: "Lines 42–44" },
    excerpt: {
      zh: "工单 ZT-84：连接中断后先检查代理设置，再重试连接。",
      en: "Ticket ZT-84: check the proxy settings after a dropped connection, then try connecting again.",
    },
    evidence: { zh: "编号匹配", en: "Identifier match" },
    lessonId: "exact-promote",
  },
  {
    id: "screenshot",
    label: { zh: "截图报错", en: "Screenshot" },
    query: { zh: "连接被重置", en: "Connection reset" },
    file: { zh: "截图/连接错误.png", en: "Screenshots/Connection error.png" },
    location: { zh: "整张截图", en: "Whole screenshot" },
    excerpt: {
      zh: "连接被重置。请检查代理与网络设置后重试。",
      en: "Connection reset. Check the proxy and network settings, then try again.",
    },
    evidence: { zh: "截图文字（OCR）", en: "Screenshot text (OCR)" },
    lessonId: "vision-ocr",
  },
] as const satisfies readonly HomeDemoScene[];
